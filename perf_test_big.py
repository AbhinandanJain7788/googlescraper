"""Bigger perf test: 30 leads with auto_grid (exercises parallel tiles)."""
import asyncio
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.scraper import GoogleMapsScraper

KEYWORD = "law firm"
LOCATION = "Manhattan, New York, USA"
N = 30


async def main():
    started = time.time()
    last_t = started
    rows = []

    def on_status(msg):
        t = time.time()
        print(f"[{t-started:6.2f}s] STATUS: {msg}")

    def on_result(item):
        nonlocal last_t
        t = time.time()
        gap = t - last_t
        last_t = t
        rows.append({
            "t_abs": t - started,
            "gap_s": gap,
            "title": item.get("title"),
            "website": item.get("website"),
            "email": item.get("email"),
            "emails_count": len(item.get("emails") or []),
            "phone": item.get("phone"),
        })
        em = item.get("email") or "(none)"
        site_short = (item.get("website") or "")[:60]
        print(f"[{t-started:6.2f}s] +{gap:5.2f}s LEAD #{len(rows):2d}: {item.get('title')!r}")
        print(f"           email={em}  site={site_short}")

    print(f"=== BIG PERF TEST: {KEYWORD} in {LOCATION}, N={N}, auto_grid=ON ===\n")
    async with GoogleMapsScraper(headless=True, fetch_emails=True) as s:
        await s.scrape(
            keyword=KEYWORD,
            location=LOCATION,
            max_results=N,
            on_result=on_result,
            on_status=on_status,
            auto_grid=True,
            restrict_to_location=True,
            tile_workers=3,
            card_workers=5,
        )

    total = time.time() - started
    with_email = sum(1 for r in rows if r["email"])
    with_site = sum(1 for r in rows if r["website"])
    with_phone = sum(1 for r in rows if r["phone"])

    print("\n=== SUMMARY ===")
    print(f"Total time:     {total:.1f}s for {len(rows)} leads")
    print(f"Avg per lead:   {total/max(len(rows),1):.2f}s")
    print(f"Throughput:     {len(rows)/max(total,1)*3600:.0f} leads/hour (extrapolated)")
    print(f"With website:   {with_site}/{len(rows)}")
    print(f"With email:     {with_email}/{len(rows)}")
    print(f"With phone:     {with_phone}/{len(rows)}")
    print(f"Email yield:    {with_email/max(len(rows),1)*100:.0f}% of leads")
    print(f"Email-of-sites: {with_email}/{with_site} ({with_email/max(with_site,1)*100:.0f}% of sites had extractable email)")


if __name__ == "__main__":
    asyncio.run(main())
