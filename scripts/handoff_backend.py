"""
V3.6 -- provider-neutral shared-queue backend abstraction.

    class HandoffBackend:
        export_ready(email_ready_rows, contact_form_ready_rows) -> upsert
        import_results() -> list[dict] of raw external result-event rows
        sync(email_ready_rows, contact_form_ready_rows) -> export_ready() + return status

`LocalFileBackend` always works, zero external credentials, and is what
config/handoff.yaml defaults to. `GoogleSheetsBackend` is a real,
runnable implementation gated behind a service-account file that does not
exist yet in this environment -- until it does, every call fails closed
with `SharedHandoffAuthRequired` (never raises an unhandled exception,
never fabricates success, never touches local state). See docs/AUTOMATION.md
"Google Sheets setup" for exactly what to provision.

Absolute rule, same as claude_invoke.py's for Claude subprocesses: nothing
in this module ever imports smtplib/imaplib/any Gmail API client, and
nothing here ever reuses Gmail OAuth credentials -- Sheets and Gmail are
governed by completely separate credentials, on purpose.
"""
import csv
import json
from pathlib import Path

from _lib import DATA, load_yaml, now_iso, append_jsonl


class SharedHandoffAuthRequired(Exception):
    """Remote backend credentials are unavailable/expired/unconfigured.
    Callers must fail closed: keep the local queue as the authoritative
    copy, record SHARED_HANDOFF_AUTH_REQUIRED, never delete local data,
    never pretend the upload succeeded."""


class HandoffBackend:
    """Interface every backend implements. `rows` arguments are lists of
    dicts already matching schemas/handoff_row.schema.json (built by
    scripts/handoff_lib.py) -- this layer only ever moves already-built
    rows, never computes lead-engine fields itself."""

    def export_ready(self, email_ready_rows, contact_form_ready_rows):
        raise NotImplementedError

    def all_rows(self):
        """Returns {lead_id: row} across both queues -- the 'existing state'
        scripts/sync_handoff.py merges fresh Lead-Engine fields into (see
        handoff_lib.merge_row) so external-owned fields are never lost."""
        raise NotImplementedError

    def export_candidates(self, candidate_rows):
        """V3.8.1 -- upserts the CANDIDATES tab/file by lead_id (never
        appends a duplicate for an already-synced lead_id). An ADDITIONAL
        handoff surface -- never touches EMAIL_READY/CONTACT_FORM_READY/
        RESULTS."""
        raise NotImplementedError

    def all_candidate_rows(self):
        """Returns {lead_id: row} for the CANDIDATES tab/file -- the
        'existing state' scripts/sync_handoff.py: sync_candidates() merges
        fresh rows into (see handoff_lib.merge_candidate_row)."""
        raise NotImplementedError

    def import_results(self):
        """Returns a list of raw external result-event dicts (see
        schemas/outreach_result_event.schema.json) the backend has
        available to read back -- e.g. a results tab/sheet, or (for
        LocalFileBackend) simply nothing, since the local fallback's result
        inbox is data/outreach/outreach_results.jsonl, read directly by
        scripts/import_outreach_results.py rather than through this
        interface (see that script's own docstring)."""
        raise NotImplementedError

    def sync(self, email_ready_rows, contact_form_ready_rows):
        """Convenience wrapper most callers use: export, and return a
        small status dict. Never raises SharedHandoffAuthRequired itself --
        callers should catch that from export_ready()/import_results()
        directly when they need the fail-closed distinction."""
        self.export_ready(email_ready_rows, contact_form_ready_rows)
        return {"backend": self.__class__.__name__, "synced_at": now_iso(),
                "email_ready_count": len(email_ready_rows), "contact_form_ready_count": len(contact_form_ready_rows)}


class LocalFileBackend(HandoffBackend):
    """Zero-credential backend: two JSON files (dict keyed by lead_id, so
    export_ready is a pure upsert -- re-running never duplicates a row) plus
    a CSV mirror of each for spreadsheet-style inspection. Always available;
    this is what makes the whole system work with no Google Sheets
    configuration at all."""

    def __init__(self, cfg=None):
        cfg = (cfg or load_yaml("handoff.yaml"))["local_backend"]
        # Relative to _lib.DATA (not ROOT) so LEAD_ENGINE_DATA_DIR -- the
        # same sandbox override every other V3.5/V3.6 path already respects
        # -- correctly redirects this too, e.g. during a controlled
        # validation run against synthetic fixtures.
        self.dir = DATA / Path(cfg["dir"]).name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.email_ready_path = self.dir / cfg["email_ready_file"]
        self.contact_form_ready_path = self.dir / cfg["contact_form_ready_file"]
        self.sync_log_path = self.dir / cfg["sync_log_file"]
        # V3.8.1 -- "candidates_file" defaults to "candidates.json" so an
        # on-disk config saved before this key existed keeps working
        # unchanged (same backward-compatible-default pattern as
        # GoogleSheetsBackend.import_results()'s results_tab).
        self.candidates_path = self.dir / cfg.get("candidates_file", "candidates.json")

    def _load(self, path):
        if not path.exists():
            return {}
        with open(path) as f:
            return json.load(f)

    def _write(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.write("\n")

    def _write_csv(self, json_path, columns):
        data = self._load(json_path)
        csv_path = json_path.with_suffix(".csv")
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in data.values():
                writer.writerow(row)

    def export_ready(self, email_ready_rows, contact_form_ready_rows):
        from handoff_lib import COLUMNS
        email_store = self._load(self.email_ready_path)
        for row in email_ready_rows:
            email_store[row["lead_id"]] = row
        self._write(self.email_ready_path, email_store)
        self._write_csv(self.email_ready_path, COLUMNS)

        form_store = self._load(self.contact_form_ready_path)
        for row in contact_form_ready_rows:
            form_store[row["lead_id"]] = row
        self._write(self.contact_form_ready_path, form_store)
        self._write_csv(self.contact_form_ready_path, COLUMNS)

        append_jsonl(self.sync_log_path, {
            "synced_at": now_iso(), "backend": "LocalFileBackend",
            "email_ready_count": len(email_ready_rows), "contact_form_ready_count": len(contact_form_ready_rows),
        })

    def all_rows(self):
        rows = {}
        rows.update(self._load(self.email_ready_path))
        rows.update(self._load(self.contact_form_ready_path))
        return rows

    def update_rows(self, rows_by_lead_id):
        """Writes back rows already present in EITHER queue file, in place
        -- used by scripts/import_outreach_results.py after apply_event(),
        which never changes preferred_channel/which queue a lead belongs to.
        A lead_id not found in either file is silently skipped (the caller
        is responsible for surfacing that as a per-event failure)."""
        from handoff_lib import COLUMNS
        for path in (self.email_ready_path, self.contact_form_ready_path):
            store = self._load(path)
            changed = False
            for lead_id, row in rows_by_lead_id.items():
                if lead_id in store:
                    store[lead_id] = row
                    changed = True
            if changed:
                self._write(path, store)
                self._write_csv(path, COLUMNS)

    def import_results(self):
        return []  # local fallback's result inbox is data/outreach/outreach_results.jsonl, read directly

    def export_candidates(self, candidate_rows):
        from handoff_lib import CANDIDATE_COLUMNS
        store = self._load(self.candidates_path)
        for row in candidate_rows:
            store[row["lead_id"]] = row
        self._write(self.candidates_path, store)
        self._write_csv(self.candidates_path, CANDIDATE_COLUMNS)
        append_jsonl(self.sync_log_path, {
            "synced_at": now_iso(), "backend": "LocalFileBackend", "candidates_count": len(candidate_rows),
        })

    def all_candidate_rows(self):
        return self._load(self.candidates_path)


class GoogleSheetsBackend(HandoffBackend):
    """
    Real, runnable Google Sheets adapter -- gated entirely behind
    `config/handoff.yaml: google_sheets.service_account_file` actually
    existing on disk. Until that file is provisioned, every method raises
    `SharedHandoffAuthRequired` immediately, before any network call is
    attempted -- this is the fail-closed behavior section 16 requires.

    Setup (see docs/AUTOMATION.md "Google Sheets setup" for the full walk-
    through): create a Google Cloud service account with ONLY the
    Sheets API enabled (never Drive, never Gmail), share the target private
    Sheet with that service account's email as an Editor, download its
    JSON key to a path OUTSIDE this repo (or anywhere already covered by
    .gitignore's credentials*.json/service-account*.json patterns), and set
    `service_account_file` + `spreadsheet_id` in config/handoff.yaml.
    `results_tab` (default `"RESULTS"` if unset, for backward compatibility
    with a config predating this key) names the tab ChatGPT/Gmail-side
    automation writes result events into.

    Dependencies (`google-api-python-client`, `google-auth`) are imported
    lazily inside methods, never at module load time -- this file, and
    every test that imports it, works with zero extra packages installed
    when the backend isn't actually selected/configured.
    """

    def __init__(self, cfg=None):
        self.cfg = (cfg or load_yaml("handoff.yaml"))["google_sheets"]

    def _require_configured(self):
        service_account_file = self.cfg.get("service_account_file")
        spreadsheet_id = self.cfg.get("spreadsheet_id")
        if not service_account_file or not spreadsheet_id:
            raise SharedHandoffAuthRequired(
                "Google Sheets backend selected but not configured -- "
                "config/handoff.yaml: google_sheets.service_account_file/spreadsheet_id "
                "are unset. Falling back to local queue as authoritative."
            )
        path = Path(service_account_file).expanduser()
        if not path.exists():
            raise SharedHandoffAuthRequired(
                f"Google Sheets service-account file not found at {path} -- "
                "see docs/AUTOMATION.md 'Google Sheets setup'. Falling back to local queue as authoritative."
            )
        return path, spreadsheet_id

    def _client(self):
        path, spreadsheet_id = self._require_configured()
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as e:
            raise SharedHandoffAuthRequired(
                "google-api-python-client/google-auth not installed -- "
                "run `pip install google-api-python-client google-auth` to enable the Google Sheets backend. "
                "Falling back to local queue as authoritative."
            ) from e
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=self.cfg["scopes"])
        service = build("sheets", "v4", credentials=creds)
        return service, spreadsheet_id

    def _upsert_tab(self, service, spreadsheet_id, tab_name, rows, columns=None):
        from handoff_lib import COLUMNS
        columns = columns or COLUMNS
        sheet = service.spreadsheets()
        existing = sheet.values().get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A:A").execute()
        existing_ids = [r[0] for r in existing.get("values", [])[1:]] if existing.get("values") else []
        id_to_row_num = {lead_id: i + 2 for i, lead_id in enumerate(existing_ids)}  # header is row 1

        header = [list(columns)]
        updates, appends = [], []
        for row in rows:
            values = [row.get(c) for c in columns]
            if row["lead_id"] in id_to_row_num:
                updates.append({"range": f"{tab_name}!A{id_to_row_num[row['lead_id']]}", "values": [values]})
            else:
                appends.append(values)

        if not existing_ids:
            sheet.values().update(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
                                   valueInputOption="RAW", body={"values": header}).execute()
        if updates:
            sheet.values().batchUpdate(spreadsheetId=spreadsheet_id,
                                        body={"valueInputOption": "RAW", "data": updates}).execute()
        if appends:
            sheet.values().append(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
                                   valueInputOption="RAW", body={"values": appends}).execute()

    def export_ready(self, email_ready_rows, contact_form_ready_rows):
        service, spreadsheet_id = self._client()
        self._upsert_tab(service, spreadsheet_id, self.cfg["email_ready_tab"], email_ready_rows)
        self._upsert_tab(service, spreadsheet_id, self.cfg["contact_form_ready_tab"], contact_form_ready_rows)

    def _read_tab_rows(self, service, spreadsheet_id, tab_name, columns=None):
        from handoff_lib import COLUMNS
        columns = columns or COLUMNS
        resp = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A:Z").execute()
        values = resp.get("values", [])
        if not values:
            return {}
        header, body = values[0], values[1:]
        rows = {}
        for r in body:
            row = dict(zip(header, r))
            if row.get("lead_id"):
                rows[row["lead_id"]] = {c: row.get(c) for c in columns}
        return rows

    def all_rows(self):
        service, spreadsheet_id = self._client()
        rows = {}
        rows.update(self._read_tab_rows(service, spreadsheet_id, self.cfg["email_ready_tab"]))
        rows.update(self._read_tab_rows(service, spreadsheet_id, self.cfg["contact_form_ready_tab"]))
        return rows

    def export_candidates(self, candidate_rows):
        """V3.8.1 -- upserts the CANDIDATES tab (config/handoff.yaml:
        google_sheets.candidates_tab, defaulting to "CANDIDATES" for
        backward compatibility with a config predating this key), reusing
        the exact same idempotent-by-lead_id _upsert_tab() every other tab
        already uses -- no separate/duplicated Sheets logic."""
        from handoff_lib import CANDIDATE_COLUMNS
        service, spreadsheet_id = self._client()
        tab_name = self.cfg.get("candidates_tab") or "CANDIDATES"
        self._upsert_tab(service, spreadsheet_id, tab_name, candidate_rows, columns=CANDIDATE_COLUMNS)

    def all_candidate_rows(self):
        from handoff_lib import CANDIDATE_COLUMNS
        service, spreadsheet_id = self._client()
        tab_name = self.cfg.get("candidates_tab") or "CANDIDATES"
        return self._read_tab_rows(service, spreadsheet_id, tab_name, columns=CANDIDATE_COLUMNS)

    def import_results(self):
        """Reads the results tab (`config/handoff.yaml:
        google_sheets.results_tab`, defaulting to `RESULTS` for backward
        compatibility with a config predating this key) -- ChatGPT/Gmail-
        side writes result events there -- and returns them as raw dicts
        for scripts/import_outreach_results.py to apply idempotently, with
        per-event validation/isolation (see handoff_lib.validate_event).

        V3.6.1: safely handles a completely empty tab by initializing it
        with the canonical header (handoff_lib.RESULT_EVENT_COLUMNS, taken
        from schemas/outreach_result_event.schema.json -- no second,
        conflicting schema invented here) -- there is nothing to overwrite
        in that case, since the tab is empty. A tab that already has ANY
        content is never touched here; its existing header (whatever it
        is) is used as-is to parse subsequent rows, exactly as before --
        this method never overwrites a legitimate existing row.
        """
        from handoff_lib import RESULT_EVENT_COLUMNS
        service, spreadsheet_id = self._client()
        tab_name = self.cfg.get("results_tab") or "RESULTS"
        resp = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"{tab_name}!A:Z").execute()
        rows = resp.get("values", [])
        if not rows:
            # Nothing here at all -- safe to self-heal the canonical header
            # so the next ChatGPT-written event row is actually parseable.
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=f"{tab_name}!A1",
                valueInputOption="RAW", body={"values": [list(RESULT_EVENT_COLUMNS)]},
            ).execute()
            return []
        header, body = rows[0], rows[1:]
        return [dict(zip(header, r)) for r in body]


def build_backend(cfg=None):
    cfg = cfg or load_yaml("handoff.yaml")
    name = cfg.get("backend", "local")
    if name == "local":
        return LocalFileBackend(cfg)
    if name == "google_sheets":
        return GoogleSheetsBackend(cfg)
    raise ValueError(f"Unknown handoff backend: {name!r}")
