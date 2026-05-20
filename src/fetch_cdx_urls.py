#!/usr/bin/env python3
"""
Fetch URLs from Wayback Machine CDX API with per‑year/month splitting and retries.
Saves each year to a separate file.
"""

import requests
import time
import argparse

def fetch_urls_for_date_range(domain, from_ts, to_ts, retries=3, delay=1.0):
    """Fetch URLs for a specific date range (e.g., one month). Returns set of URLs."""
    base_url = "https://web.archive.org/cdx/search/cdx"
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
    for attempt in range(retries):
        try:
            resp = requests.get(base_url, params=params, timeout=90)
            if resp.status_code == 200:
                data = resp.json()
                if not data or len(data) < 2:
                    return set()
                urls = set()
                for row in data[1:]:
                    urls.add(row[0])
                return urls
            else:
                print(f"  HTTP {resp.status_code} (attempt {attempt+1}/{retries})")
        except Exception as e:
            print(f"  Error: {e} (attempt {attempt+1}/{retries})")
        if attempt < retries - 1:
            time.sleep(delay * (2 ** attempt))  # exponential backoff
    return set()

def fetch_urls_by_month(domain, year, month, output_dir):
    """Fetch URLs for a single month and save to file if any."""
    from_ts = f"{year}{month:02d}01"
    # last day of month
    if month == 12:
        to_ts = f"{year+1}0101"
    else:
        to_ts = f"{year}{month+1:02d}01"
    print(f"    Month {year}-{month:02d}...", end=" ", flush=True)
    urls = fetch_urls_for_date_range(domain, from_ts, to_ts)
    if urls:
        filename = f"{output_dir}/{domain.replace('/', '_')}_{year}_{month:02d}.txt"
        with open(filename, "w") as f:
            for url in sorted(urls):
                f.write(url + "\n")
        print(f"{len(urls)} URLs saved to {filename}")
    else:
        print("no URLs")
    return len(urls)

def fetch_urls_by_year(domain, start_year, end_year, output_dir, split_months=True):
    """
    Fetch URLs per year, optionally splitting into months for large years.
    Saves each year's URLs to a separate file (or month files if split_months=True).
    """
    import os
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
            print(f"  Fetching {year} in one request...", end=" ", flush=True)
            urls = fetch_urls_for_date_range(domain, from_ts, to_ts)
            if urls:
                filename = f"{output_dir}/{domain.replace('/', '_')}_{year}.txt"
                with open(filename, "w") as f:
                    for url in sorted(urls):
                        f.write(url + "\n")
                print(f"{len(urls)} URLs saved to {filename}")
                total += len(urls)
            else:
                print("no URLs")
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