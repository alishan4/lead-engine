#!/usr/bin/env python3
"""
V3.2 deterministic page-fact extraction. Real HTTP requests, plain-text/HTML
parsing via the standard library only -- ZERO LLM/agent cost for any fact
listed here. This is the literal "prefer code over LLMs for facts that code
can determine" toolkit: robots.txt, sitemap presence/count, status codes,
canonical presence, indexability directives, title/H1/meta extraction,
schema-type detection, HTTPS, and basic broken-link/nav extraction.

Nothing here calls WebFetch/WebSearch or any Agent tool -- this module makes
its own outbound requests via urllib, deterministically, so the same URL
produces the same facts regardless of which model is running the pipeline.
"""
import re
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

USER_AGENT = "Mozilla/5.0 (compatible; LeadEngineIntelligence/3.2; +deterministic-scan)"
TIMEOUT = 10


def fetch(url):
    """Returns {status, html, headers, error} -- never raises. error is None on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            html = body.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            return {"status": resp.status, "html": html, "headers": dict(resp.headers), "error": None, "url": url}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "html": None, "headers": None, "error": str(e), "url": url}
    except Exception as e:  # noqa: BLE001 -- deliberately broad: DNS, TLS, timeout, etc. are all "couldn't fetch"
        return {"status": None, "html": None, "headers": None, "error": f"{type(e).__name__}: {e}", "url": url}


def check_https(url):
    return urlparse(url).scheme == "https"


def check_robots_txt(domain_root):
    """domain_root e.g. https://example.com -- returns {exists, disallows_all, content}."""
    result = fetch(urljoin(domain_root, "/robots.txt"))
    if result["status"] != 200 or not result["html"]:
        return {"exists": False, "disallows_all": None, "content": None}
    content = result["html"]
    disallows_all = bool(re.search(r"Disallow:\s*/\s*$", content, re.MULTILINE))
    return {"exists": True, "disallows_all": disallows_all, "content": content[:2000]}


def check_sitemap(domain_root):
    """Tries /sitemap.xml. Returns {exists, url_count, is_index}."""
    result = fetch(urljoin(domain_root, "/sitemap.xml"))
    if result["status"] != 200 or not result["html"]:
        return {"exists": False, "url_count": None, "is_index": None}
    content = result["html"]
    url_count = len(re.findall(r"<loc>", content, re.IGNORECASE))
    is_index = "<sitemapindex" in content.lower()
    return {"exists": True, "url_count": url_count, "is_index": is_index}


def extract_title(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def extract_h1(html):
    matches = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    cleaned = [re.sub(r"<[^>]+>", "", m) for m in matches]
    cleaned = [re.sub(r"\s+", " ", m).strip() for m in cleaned]
    return [m for m in cleaned if m]


def extract_meta_description(html):
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html, re.IGNORECASE | re.DOTALL,
    )
    return m.group(1).strip() if m else None


def has_canonical(html):
    return bool(re.search(r'<link[^>]+rel=["\']canonical["\']', html, re.IGNORECASE))


def has_noindex(html):
    return bool(re.search(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\'][^"\']*noindex', html, re.IGNORECASE))


def detect_schema_types(html):
    """Returns a list of schema.org @type values found in JSON-LD blocks, deterministic string scan."""
    types = set()
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                              html, re.IGNORECASE | re.DOTALL):
        for m in re.findall(r'"@type"\s*:\s*"([^"]+)"', block):
            types.add(m)
    return sorted(types)


def extract_nav_links(html, base_url):
    """Best-effort extraction of <nav> links -- used to count distinct service/practice pages linked from nav."""
    nav_blocks = re.findall(r"<nav[^>]*>(.*?)</nav>", html, re.IGNORECASE | re.DOTALL)
    links = []
    for block in nav_blocks:
        for href, text in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, re.IGNORECASE | re.DOTALL):
            clean_text = re.sub(r"<[^>]+>", "", text)
            clean_text = re.sub(r"\s+", " ", clean_text).strip()
            links.append({"href": urljoin(base_url, href), "text": clean_text})
    return links


def extract_phone(html):
    m = re.search(r"(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})", html)
    return m.group(1) if m else None


def has_contact_form(html):
    return bool(re.search(r"<form", html, re.IGNORECASE))


def extract_facts(url):
    """
    One-call deterministic fact bundle for a single page -- the unit the
    scan budget (max_pages_initial_audit-style limit) counts against.
    """
    result = fetch(url)
    facts = {
        "url": url, "status": result["status"], "fetch_error": result["error"],
        "https": check_https(url),
        "title": None, "h1": [], "meta_description": None, "has_canonical": False,
        "has_noindex": False, "schema_types": [], "nav_links": [], "phone_found": None,
        "has_contact_form": False,
    }
    if result["html"]:
        html = result["html"]
        facts.update({
            "title": extract_title(html), "h1": extract_h1(html),
            "meta_description": extract_meta_description(html),
            "has_canonical": has_canonical(html), "has_noindex": has_noindex(html),
            "schema_types": detect_schema_types(html), "nav_links": extract_nav_links(html, url),
            "phone_found": extract_phone(html), "has_contact_form": has_contact_form(html),
        })
    return facts


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) != 2:
        print("Usage: python3 page_facts.py <url>", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(extract_facts(sys.argv[1]), indent=2))
