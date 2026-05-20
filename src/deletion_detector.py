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
REQUEST_DELAY = 1.0          # seconds between each URL check
CDX_API_URL = "https://web.archive.org/cdx/search/cdx"
OUTPUT_DIR = "saved_articles"

# ------------------------- TEXT EXTRACTION -------------------------
def extract_text_from_html(html, url=""):
    """
    Extract main article text from HTML using trafilatura.
    Returns plain text string, or None if extraction fails.
    """
    if not HAS_TRAFILATURA:
        # Fallback: return raw HTML (will be hashed as full page)
        return html
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_formatting=False,
            output_format="text"
        )
        if text and len(text.strip()) > 50:
            return text.strip()
        else:
            # Fallback to raw HTML if extracted text is too short
            return html
    except Exception:
        return html

def get_content_digest(html, content_only=True):
    """
    Return SHA-1 digest of either extracted text or full HTML.
    """
    if content_only and HAS_TRAFILATURA:
        text = extract_text_from_html(html)
        # Use the extracted text (or fallback full HTML)
        content = text.encode('utf-8')
    else:
        content = html.encode('utf-8')
    return hashlib.sha1(content).hexdigest().upper(), content

# ------------------------- NETWORK HELPERS -------------------------
def get_live_status(url, content_only=True):
    """Fetch current live page, return (status_code, content_digest)."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        if resp.status_code == 200:
            digest, content = get_content_digest(resp.text, content_only)
        else:
            digest = None
        return resp.status_code, digest, content
    except Exception:
        return None, None, None

def get_cdx_snapshots(url):
    """Fetch all snapshots for a URL from CDX API."""
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,statuscode,digest,original",
        "limit": 1000,
    }
    try:
        resp = requests.get(CDX_API_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=15)
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
    """Retrieve the HTML content from a Wayback snapshot."""
    try:
        resp = requests.get(archive_url, timeout=20, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None

def save_article_content(url, verdict, archive_url, timestamp, content, content_only):
    """Save full HTML to a file, with metadata comments."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_url = re.sub(r'[^a-zA-Z0-9]', '_', url)[:80]
    mode = "text_only" if content_only else "full_page"
    filename = f"{verdict}_{mode}_{timestamp}_{safe_url}.html"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"<!-- Original URL: {url} -->\n")
        f.write(f"<!-- Verdict: {verdict} -->\n")
        f.write(f"<!-- Comparison mode: {'content-only' if content_only else 'full-page'} -->\n")
        f.write(f"<!-- Archive snapshot: {archive_url} -->\n")
        f.write(content)
    return filepath

def analyze_and_save(url, content_only):
    """
    Check a single URL.
    Returns (verdict, metadata_dict, saved_filepath) if deleted/changed,
    otherwise returns (None, None, None).
    """
    # Get live status and content digest
    live_code, live_digest, content = get_live_status(url, content_only)
    
    # Get archive snapshots
    snapshots = get_cdx_snapshots(url)
    if not snapshots:
        return None, None, None
    
    # Find the latest successful (200) snapshot
    latest_good = None
    for snap in reversed(snapshots):
        if snap["statuscode"] == "200":
            latest_good = snap
            break
    if not latest_good:
        return None, None, None
    
    # For changed detection, we need to compute archive content digest
    # (the CDX digest is the full-page SHA-1, so we must recompute for content-only)
    archive_html = fetch_archive_content(latest_good["archive_url"])
    if not archive_html:
        return None, None, None
    
    if content_only and HAS_TRAFILATURA:
        archive_digest, archive_content = get_content_digest(archive_html, content_only=True)
        # live_digest already computed with content_only
    else:
        # Use CDX's stored digest for full-page comparison (faster)
        archive_digest, archive_content = latest_good["digest"]
        # If we are in content-only mode but trafilatura missing, fall back to full page
        if content_only and not HAS_TRAFILATURA:
            archive_digest = get_content_digest(archive_html, content_only=False)
    
    # Determine verdict
    verdict = None
    if live_code == 404:
        verdict = "deleted"
    elif live_code == 200 and live_digest != archive_digest:
        verdict = "changed"
        print(f"  [!] Content changed: live digest {live_digest} vs archive digest {archive_digest}")
    else:
        return None, None, None
    
    # Save the full archive HTML
    saved_path = save_article_content(url, verdict, latest_good["archive_url"],
                                      latest_good["timestamp"], archive_html, content_only)
    
    metadata = {
        "url": url,
        "verdict": verdict,
        "live_status_code": live_code,
        "archive_timestamp": latest_good["timestamp"],
        "archive_url": latest_good["archive_url"],
        "saved_file": saved_path,
        "comparison_mode": "content-only" if content_only else "full-page",
        "archive_digest": archive_digest,
        "live_digest": live_digest
    }
    return verdict, metadata, saved_path

# ------------------------- MAIN -------------------------
def main():
    parser = argparse.ArgumentParser(description="Find and save deleted/changed articles using content-based comparison")
    parser.add_argument("--urls-file", required=True, help="Text file with one URL per line")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of URLs to check (random sample)")
    parser.add_argument("--target", type=int, default=50, help="Stop after finding this many deleted/changed articles")
    parser.add_argument("--output-dir", default="saved_articles", help="Directory to save HTML files")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between URL checks")
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
    
    print(f"\n[Phase 2] Comparison mode: {'content-only (extracted text)' if content_only else 'full-page HTML'}")
    print(f"Target: {args.target} deleted/changed articles.\n")
    
    # --- Analysis loop ---
    found_count = 0
    results = []
    checked = 0
    
    for i, url in enumerate(all_urls, 1):
        if found_count >= args.target:
            print(f"\n✅ Reached target of {args.target} deleted/changed articles. Stopping.")
            break
        
        print(f"[{i}/{len(all_urls)}] Checking: {url[:80]}...")
        verdict, metadata, saved_path = analyze_and_save(url, content_only)
        checked += 1
        
        if verdict:
            found_count += 1
            results.append(metadata)
            print(f"  🚨 {verdict.upper()} #{found_count} -> saved to {saved_path}")
        else:
            print("  ➖ Unchanged or no archive")
        
        time.sleep(REQUEST_DELAY)
    
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