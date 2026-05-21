#!/usr/bin/env python3
"""
Deletion and Change Detector with Content-Based Comparison
- Loads URLs from a text file (one per line)
- Compares live article text vs. archived text (ignores layout/ads)
- Saves full HTML of deleted/changed articles as evidence
- Stops after finding target number of deleted+changed articles
"""

import sys
import re
import time
import json
import hashlib
import requests
import argparse
import random
import os
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Try to import trafilatura (optional but recommended)
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("Warning: trafilatura not installed. Install with: uv add trafilatura", file=sys.stderr)

# ------------------------- CONFIGURATION -------------------------
USER_AGENT = "Mozilla/5.0 (compatible; DeletionDetector/1.0; research@example.com)"
REQUEST_DELAY = 0.0          # per-task delay; concurrency provides natural spacing
CDX_API_URL = "https://web.archive.org/cdx/search/cdx"
OUTPUT_DIR = "saved_articles"

# Shared session — connection pooling cuts TLS handshake cost across all requests.
# Adapter pool sized for the worker pool so concurrent workers don't serialize on the
# default 10-connection pool.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})
_adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

PRINT_LOCK = threading.Lock()

# ------------------------- TEXT EXTRACTION -------------------------
def extract_text_from_html(html, url=""):
    """
    Extract main article text from HTML using trafilatura, then normalize.
    Returns normalized plain text, or None if extraction fails / yields too little.
    Never falls back to raw HTML — that would defeat content-only comparison.

    This output is used for the digest comparison only. Links and formatting
    are stripped so that volatile URL parameters (tracking tokens, session
    IDs) don't trigger false 'changed' verdicts. For human-readable evidence
    files see extract_article_for_save().
    """
    if not HAS_TRAFILATURA:
        return None
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_formatting=False,
            output_format="text",
        )
    except Exception:
        return None
    if not text or len(text.strip()) < 50:
        return None
    text = unicodedata.normalize("NFKC", text)
    text = " ".join(text.split())
    return text

def extract_article_for_save(html):
    """Extract article preserving links, headings, lists, and paragraph
    structure — output is Markdown so the saved file is human-readable AND
    diffable. Used only for evidence files, not for the digest comparison.
    Returns Markdown text or None if extraction fails / yields too little.
    """
    if not HAS_TRAFILATURA:
        return None
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_formatting=True,
            include_links=True,
            output_format="markdown",
        )
    except Exception:
        return None
    if not text or len(text.strip()) < 50:
        return None
    return text.strip()

def get_content_digest(html, content_only=True):
    """
    Return (SHA-1 digest, content bytes) of extracted text or full HTML.
    Returns (None, None) if content-only extraction failed.
    """
    if content_only:
        text = extract_text_from_html(html)
        if text is None:
            return None, None
        content = text.encode("utf-8")
    else:
        content = html.encode("utf-8")
    return hashlib.sha1(content).hexdigest().upper(), content

# ------------------------- NETWORK HELPERS -------------------------
def get_live_status(url, content_only=True):
    """Fetch current live page, return (status_code, content_digest, raw_html).
    On non-200 responses the digest/raw_html are None but the status code is
    returned so the caller can still detect 404s as deletions. raw_html is the
    full HTTP body — used to save the live side of a `changed` finding."""
    try:
        resp = SESSION.get(url, timeout=15, allow_redirects=True)
    except Exception:
        return None, None, None
    if resp.status_code == 200:
        digest, _ = get_content_digest(resp.text, content_only)
        return resp.status_code, digest, resp.text
    return resp.status_code, None, None

def get_cdx_snapshots(url):
    """Fetch all snapshots for a URL from CDX API."""
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,statuscode,digest,original",
        "limit": 1000,
    }
    try:
        resp = SESSION.get(CDX_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not data or len(data) < 2:
            return []
        snapshots = []
        for row in data[1:]:
            snapshots.append({
                "timestamp": row[0],
                "statuscode": row[1],
                "digest": row[2],
                "archive_url": f"https://web.archive.org/web/{row[0]}/{row[3]}"
            })
        return snapshots
    except Exception:
        return []

def fetch_archive_content(archive_url):
    """Retrieve raw archived HTML using the `id_` modifier so the Wayback toolbar,
    banner script, and rewritten URLs are not included — otherwise comparison would
    pick up Wayback's own wrapper as 'changes'."""
    raw_url = re.sub(r"(/web/\d{14})/", r"\1id_/", archive_url, count=1)
    try:
        resp = SESSION.get(raw_url, timeout=20)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def _get_article_text(html):
    """Best-effort extraction for the saved evidence file:
    Markdown-with-links first, plain text as a fallback so we still get
    *something* readable when trafilatura can't produce structured output."""
    if html is None:
        return None
    rich = extract_article_for_save(html)
    if rich is not None:
        return rich
    return extract_text_from_html(html)

def save_finding_json(url, verdict, live_code, latest_good,
                      archive_html, live_html,
                      archive_digest, live_digest, content_only):
    """Write one JSON file per finding containing both URLs and the extracted
    article text from each side that exists. Filename:
    ``{verdict}_{archive_timestamp}_{sanitized_url}.json``.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_url = re.sub(r'[^a-zA-Z0-9]', '_', url)[:80]
    filename = f"{verdict}_{latest_good['timestamp']}_{safe_url}.json"
    filepath = os.path.join(OUTPUT_DIR, filename)

    archive_raw_url = re.sub(r"(/web/\d{14})/", r"\1id_/",
                             latest_good["archive_url"], count=1)
    finding = {
        "url": url,
        "verdict": verdict,
        "comparison_mode": "content-only" if content_only else "full-page",
        "archive": {
            "url": latest_good["archive_url"],
            "raw_url": archive_raw_url,
            "timestamp": latest_good["timestamp"],
            "digest": archive_digest,
            "text": _get_article_text(archive_html),
        },
        "live": {
            "url": url,
            "status_code": live_code,
            "fetched_at": datetime.now().isoformat(),
            "digest": live_digest,
            # Only `changed` has live article text; for `deleted` the live
            # page is 404 so there's no text to save.
            "text": _get_article_text(live_html) if verdict == "changed" else None,
        },
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(finding, f, indent=2, ensure_ascii=False)
    return filepath

def analyze_and_save(url, content_only):
    """
    Check a single URL.
    Returns (verdict, metadata_dict, saved_path) if deleted/changed,
    otherwise returns (None, None, None).

    For each finding we write a single JSON file containing both URLs
    (live + archive), the archived article text, and — for `changed` only —
    the live article text. Article text is extracted with trafilatura in
    Markdown mode so hyperlinks and paragraph structure are preserved.
    """
    live_code, live_digest, live_html = get_live_status(url, content_only)

    snapshots = get_cdx_snapshots(url)
    if not snapshots:
        return None, None, None

    latest_good = None
    for snap in reversed(snapshots):
        if snap["statuscode"] == "200":
            latest_good = snap
            break
    if not latest_good:
        return None, None, None

    archive_html = fetch_archive_content(latest_good["archive_url"])
    if not archive_html:
        return None, None, None

    archive_digest, _ = get_content_digest(archive_html, content_only=content_only)

    verdict = None
    if live_code == 404:
        verdict = "deleted"
    elif (
        live_code == 200
        and live_digest is not None
        and archive_digest is not None
        and live_digest != archive_digest
    ):
        verdict = "changed"
    else:
        return None, None, None

    saved_path = save_finding_json(
        url, verdict, live_code, latest_good,
        archive_html, live_html,
        archive_digest, live_digest, content_only,
    )

    metadata = {
        "url": url,
        "verdict": verdict,
        "live_status_code": live_code,
        "archive_timestamp": latest_good["timestamp"],
        "archive_url": latest_good["archive_url"],
        "saved_file": saved_path,
        "comparison_mode": "content-only" if content_only else "full-page",
        "archive_digest": archive_digest,
        "live_digest": live_digest,
    }
    return verdict, metadata, saved_path

# ------------------------- MAIN -------------------------
def _process_one(idx, total, url, content_only, delay):
    """Worker entry point: analyze one URL, optionally sleep, return result tuple."""
    try:
        verdict, metadata, saved_path = analyze_and_save(url, content_only)
    except Exception as e:
        return idx, url, None, None, None, e
    if delay > 0:
        time.sleep(delay)
    return idx, url, verdict, metadata, saved_path, None

def main():
    parser = argparse.ArgumentParser(description="Find and save deleted/changed articles using content-based comparison")
    parser.add_argument("--urls-file", required=True, help="Text file with one URL per line")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of URLs to check (random sample)")
    parser.add_argument("--target", type=int, default=50, help="Stop after finding this many deleted/changed articles")
    parser.add_argument("--output-dir", default="saved_articles", help="Directory to save HTML files")
    parser.add_argument("--workers", type=int, default=8, help="Concurrent worker threads (default 8). Lower this if Wayback rate-limits you.")
    parser.add_argument("--delay", type=float, default=0.0, help="Per-task delay after each URL (default 0). Bump up if rate-limited.")
    parser.add_argument("--full-page", action="store_true", help="Compare full HTML instead of extracted text (more false positives)")
    args = parser.parse_args()

    content_only = not args.full_page
    if content_only and not HAS_TRAFILATURA:
        print("Warning: trafilatura not installed. Falling back to full-page comparison.", file=sys.stderr)
        content_only = False

    global OUTPUT_DIR, REQUEST_DELAY
    OUTPUT_DIR = args.output_dir
    REQUEST_DELAY = args.delay

    # --- Load URLs ---
    print(f"\n[Phase 1] Loading URLs from {args.urls_file}")
    try:
        with open(args.urls_file, "r", encoding="utf-8") as f:
            all_urls = [line.strip() for line in f if line.strip()]
        print(f"  [+] Loaded {len(all_urls)} URLs.")
    except Exception as e:
        print(f"  [!] Error loading file: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Apply limit ---
    if args.limit and args.limit < len(all_urls):
        all_urls = random.sample(all_urls, args.limit)
        print(f"  [+] Randomly selected {len(all_urls)} URLs to check.")
    else:
        print(f"  [+] Will check all {len(all_urls)} URLs.")

    total = len(all_urls)
    print(f"\n[Phase 2] Comparison mode: {'content-only (extracted text)' if content_only else 'full-page HTML'}")
    print(f"Workers: {args.workers}, per-task delay: {args.delay}s, target: {args.target} deleted/changed articles.\n")

    # --- Analysis loop (concurrent) ---
    found_count = 0
    results = []
    checked = 0
    start_time = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_process_one, i, total, url, content_only, args.delay): (i, url)
            for i, url in enumerate(all_urls, 1)
        }
        try:
            for fut in as_completed(futures):
                idx, url, verdict, metadata, saved_path, err = fut.result()
                checked += 1
                with PRINT_LOCK:
                    if err is not None:
                        print(f"[{idx}/{total}] ⚠️  error: {url[:80]} — {err}", file=sys.stderr)
                    elif verdict:
                        found_count += 1
                        results.append(metadata)
                        print(f"[{idx}/{total}] 🚨 {verdict.upper()} #{found_count}: {url[:80]} -> {saved_path}")
                    # Skip per-URL "unchanged" prints — they drown out signal with N workers.
                if found_count >= args.target:
                    print(f"\n✅ Reached target of {args.target}. Cancelling remaining work.")
                    break
        finally:
            # Cancel queued-but-not-started futures; in-flight tasks finish on their own.
            for f in futures:
                f.cancel()

    elapsed = time.monotonic() - start_time
    rate = checked / elapsed if elapsed > 0 else 0.0
    print(f"\n⏱  Checked {checked} URLs in {elapsed:.1f}s ({rate:.1f} URLs/s).")

    # --- Save report ---
    report = {
        "source_file": args.urls_file,
        "scan_time": datetime.now().isoformat(),
        "comparison_mode": "content-only" if content_only else "full-page",
        "total_urls_loaded": len(all_urls),
        "total_checked": checked,
        "target": args.target,
        "found_count": found_count,
        "deleted_changed_articles": results
    }
    report_file = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n📄 Full report saved to {report_file}")
    print(f"📊 Found {found_count} deleted/changed articles out of {checked} checked.")
    print(f"📁 HTML files saved in '{OUTPUT_DIR}/'")
    print("\n✅ Done.")

if __name__ == "__main__":
    main()