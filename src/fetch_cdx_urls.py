#!/usr/bin/env python3
"""
Fetch URLs from Wayback Machine CDX API with per‑year/month splitting and retries.
Saves each year to a separate file.
"""

import os
import requests
import time
import argparse
from datetime import date, timedelta

# CDX is overloaded a lot of the time — give it real backoff time.
RETRY_BACKOFFS = [2, 5, 15, 30, 60]  # seconds before retry N (one entry per gap)
REQUEST_TIMEOUT = 120

def _month_filename(domain, year, month, output_dir):
    return f"{output_dir}/{domain.replace('/', '_')}_{year}_{month:02d}.txt"

def fetch_urls_for_date_range(domain, from_ts, to_ts, retries=5, timeout=REQUEST_TIMEOUT):
    """Fetch URLs for a date range. Returns (urls_set, error_or_None).

    error_or_None lets the caller distinguish 'range was genuinely empty' (None)
    from 'all retries failed' (string) — important so we only fall back to
    smaller windows on real failure, not on empty months."""
    base_url = "https://web.archive.org/cdx/search/cdx"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    params = {
        "url": domain if domain.endswith('/') else domain + '/',
        "matchType": "prefix",
        "from": from_ts,
        "to": to_ts,
        "output": "json",
        "fl": "original",
        "limit": 200000,
        "filter": "statuscode:200",
    }
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(base_url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if not data or len(data) < 2:
                    return set(), None
                urls = {row[0] for row in data[1:]}
                return urls, None
            last_error = f"HTTP {resp.status_code}"
            print(f"      HTTP {resp.status_code} (attempt {attempt+1}/{retries})", flush=True)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"      {type(e).__name__}: {e} (attempt {attempt+1}/{retries})", flush=True)
        if attempt < retries - 1:
            wait = RETRY_BACKOFFS[min(attempt, len(RETRY_BACKOFFS) - 1)]
            time.sleep(wait)
    return set(), last_error or "exhausted retries"

def _fetch_urls_in_weekly_windows(domain, year, month):
    """Fallback: split month into ~7-day windows and union results. CDX often
    chokes on the largest months (e.g. Fontanka Jan 2015 ≈ 165k URLs); smaller
    windows usually succeed even when the full-month query 504s."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    all_urls = set()
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=7), end)
        from_ts = cur.strftime("%Y%m%d")
        to_ts = nxt.strftime("%Y%m%d")
        print(f"      week {from_ts}–{to_ts}...", flush=True)
        urls, err = fetch_urls_for_date_range(domain, from_ts, to_ts)
        if err is not None:
            print(f"        failed: {err}", flush=True)
        else:
            print(f"        ok: {len(urls)} URLs", flush=True)
            all_urls |= urls
        cur = nxt
    return all_urls

def fetch_urls_by_month(domain, year, month, output_dir):
    """Fetch URLs for a single month and save to file.

    Resumes by skipping months whose output file already exists. Falls back to
    weekly windows if the full-month CDX query fails on every retry."""
    filename = _month_filename(domain, year, month, output_dir)

    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        with open(filename) as f:
            count = sum(1 for _ in f)
        print(f"    Month {year}-{month:02d}... skip (exists, {count} URLs)")
        return count

    from_ts = f"{year}{month:02d}01"
    if month == 12:
        to_ts = f"{year+1}0101"
    else:
        to_ts = f"{year}{month+1:02d}01"

    print(f"    Month {year}-{month:02d}...", flush=True)
    urls, err = fetch_urls_for_date_range(domain, from_ts, to_ts)
    if err is not None:
        print(f"      month-level failed ({err}); falling back to weekly windows", flush=True)
        urls = _fetch_urls_in_weekly_windows(domain, year, month)

    if urls:
        with open(filename, "w") as f:
            for url in sorted(urls):
                f.write(url + "\n")
        print(f"      ok: {len(urls)} URLs saved to {filename}")
    else:
        print("      no URLs")
    return len(urls)

def fetch_urls_by_year(domain, start_year, end_year, output_dir, split_months=True):
    """
    Fetch URLs per year, optionally splitting into months for large years.
    Saves each year's URLs to a separate file (or month files if split_months=True).
    """
    os.makedirs(output_dir, exist_ok=True)
    total = 0
    for year in range(start_year, end_year + 1):
        print(f"\nYear {year}:")
        if split_months:
            year_total = 0
            for month in range(1, 13):
                year_total += fetch_urls_by_month(domain, year, month, output_dir)
            print(f"  Year {year} total: {year_total} URLs")
            total += year_total
        else:
            # single request for the whole year (may time out for large years)
            from_ts = f"{year}0101"
            to_ts = f"{year+1}0101"
            print(f"  Fetching {year} in one request...", flush=True)
            urls, err = fetch_urls_for_date_range(domain, from_ts, to_ts)
            if err is not None:
                print(f"    failed: {err}")
            elif urls:
                filename = f"{output_dir}/{domain.replace('/', '_')}_{year}.txt"
                with open(filename, "w") as f:
                    for url in sorted(urls):
                        f.write(url + "\n")
                print(f"    ok: {len(urls)} URLs saved to {filename}")
                total += len(urls)
            else:
                print("    no URLs")
    print(f"\n✅ Total unique URLs saved: {total}")

def main():
    parser = argparse.ArgumentParser(description="Fetch URLs from Wayback Machine CDX API with per‑year output")
    parser.add_argument("--domain", default="www.fontanka.ru", help="Domain to fetch")
    parser.add_argument("--from", dest="from_year", type=int, default=2015, help="Start year (YYYY)")
    parser.add_argument("--to", dest="to_year", type=int, default=2025, help="End year (YYYY)")
    parser.add_argument("--output-dir", "-o", default="urls_by_year", help="Directory to save output files")
    parser.add_argument("--no-split-months", action="store_true", help="Do NOT split years into months (may time out)")
    args = parser.parse_args()
    
    fetch_urls_by_year(
        domain=args.domain,
        start_year=args.from_year,
        end_year=args.to_year,
        output_dir=args.output_dir,
        split_months=not args.no_split_months
    )

if __name__ == "__main__":
    main()