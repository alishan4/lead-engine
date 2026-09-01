# Fixtures

Synthetic, fully fake example records showing the real shape of every
artifact the pipeline produces, for a made-up business ("Example Roofing
Co.", `roofing-exampleville-ex-example-roofing-co`) that does not exist.
No field here is derived from or resembles any real prospect processed by
this system.

Use these to understand a file format without needing to run the pipeline
against a real business first, and as the input shape reference when
writing a new script that reads/writes one of these files.

- `example_lead/` — one full lead directory: `prospect.json`,
  `qualification_v3.json`, `primary_wedge.json`, `intelligence_dossier.json`,
  `staged_asset.json`, `contact_record.json`, `email_draft.json`,
  `send_window.json`.
- `example_discovered.jsonl` — one `discovered.jsonl`-shaped line.
- `example_ready_to_send.jsonl` — one `ready_to_send.jsonl`-shaped line
  (the ChatGPT/Gmail-side handoff contract).
- `example_daily_run_summary.json` — one `data/runtime/daily_runs/*.json`
  run summary.

Real, committed schemas for all of these live in `schemas/`; these
fixtures are illustrative examples, not the validation source of truth.
