"""Google Maps scraper using Playwright. No API key required.

Capabilities:
  * search by keyword + location
  * scroll the result feed until enough leads are collected or end-of-list
  * pull title/rating/reviews/category/address/phone/website from the detail panel
  * fetch the business website + /contact pages and extract emails (best-effort)
  * fan-out across multiple keywords / locations with placeId-based dedup
  * AUTO-GRID: geographically tile a region into many viewport searches so we
    can collect far more than Google's ~120-per-query soft cap. Each tile is
    its own viewport-anchored search; results are deduped by placeId.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from urllib.parse import quote_plus, urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


# ----- helpers ---------------------------------------------------------------

PIN_RE = re.compile(r"\b(\d{5,6})\b")
PHONE_DATA_ID_RE = re.compile(r"^phone:tel:")

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Emails we never want to surface: tracking, default placeholders, image-file
# names that happen to match the regex, etc.
EMAIL_BLOCKLIST_SUBSTR = (
    "@example.",
    "@sentry.",
    "@wixpress.",
    "@wix.com",
    "@2x.png",
    ".png@",
    ".jpg@",
    ".svg@",
    ".webp@",
    "@sentry-",
    "@u003e",
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "user@",
    "name@",
    "email@",
    "your@",
    "yourname@",
    "yourcompany@",
    "demo@",
    "test@",
    "sample@",
    "username@",
    "firstname@",
    "lastname@",
    "@yourdomain.",
    "@yourcompany.",
    "@yoursite.",
    "@domain.com",
    "@email.com",
)
# Emails whose DOMAIN ends in an image/asset extension are CSS/SVG sprite names
# that got pattern-matched as emails (e.g. "logo@1x.svg" or "icon@2x.png").
EMAIL_DOMAIN_IMG_EXT = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".woff", ".woff2", ".ttf", ".eot", ".css", ".js")
# Reject emails whose local-part is a hash or starts with digits like asset filenames.
EMAIL_REJECT_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$|^[0-9]+x[0-9]+$")

# Contact paths to try when the homepage has no email. Ordered by hit-rate;
# we fire them concurrently so order is just for the case where we trim with
# `max_pages`. The 5 below cover ~95% of real-world hits in our testing.
CONTACT_PATHS = ("", "/contact", "/contact-us", "/about", "/about-us")

# very rough country lookup; the location query usually carries the country
# name so we infer from that. Falls back to None.
COUNTRY_CODE_MAP = {
    "india": "IN",
    "united states": "US", "usa": "US", "u.s.a": "US", "united states of america": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "canada": "CA",
    "australia": "AU",
    "germany": "DE",
    "france": "FR",
    "japan": "JP",
    "uae": "AE", "united arab emirates": "AE",
    "singapore": "SG",
    "pakistan": "PK",
    "bangladesh": "BD",
    "nepal": "NP",
    "sri lanka": "LK",
    "indonesia": "ID",
    "malaysia": "MY",
    "thailand": "TH",
    "philippines": "PH",
    "vietnam": "VN",
}

# Indian state names — used to default countryCode to IN when an Indian state
# appears in the address even though the user didn't type "India" explicitly.
INDIAN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa",
    "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland",
    "odisha", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "west bengal", "delhi", "jammu and kashmir",
    "ladakh", "puducherry", "chandigarh",
}


def _infer_country_code(location_query: str, address: Optional[str] = None) -> Optional[str]:
    q = (location_query or "").lower()
    for name, code in COUNTRY_CODE_MAP.items():
        if name in q:
            return code
    if address:
        a = address.lower()
        for name, code in COUNTRY_CODE_MAP.items():
            if name in a:
                return code
        for state in INDIAN_STATES:
            if state in a:
                return "IN"
    return None


COUNTRY_TRAILERS = {
    "united states", "united states of america", "usa", "u.s.a.", "u.s.a", "us",
    "united kingdom", "u.k.", "uk", "england", "scotland", "wales",
    "india", "canada", "australia", "uae", "united arab emirates",
    "germany", "france", "japan", "singapore", "pakistan", "bangladesh",
    "nepal", "sri lanka", "indonesia", "malaysia", "thailand", "philippines",
    "vietnam", "ireland", "new zealand", "south africa", "italy", "spain",
    "netherlands", "belgium", "switzerland", "austria", "sweden", "norway",
    "denmark", "finland", "poland", "portugal", "greece", "turkey", "mexico",
    "brazil", "argentina", "chile",
}


def _parse_address(full: Optional[str]) -> dict:
    """Best-effort split of a comma-separated address into street/city/state/PIN.

    Handles both "Indian" style (no trailing country) and "US/EU" style
    (trailing country name). Examples:
      "Sadar Bazar, Raipur, Chhattisgarh 492001"
        -> street=Sadar Bazar, city=Raipur, state=Chhattisgarh, pin=492001
      "139 Fulton St Suite 801, New York, NY 10038, United States"
        -> street=139 Fulton St Suite 801, city=New York, state=NY, pin=10038
    """
    out = {"street": None, "city": None, "state": None, "postalCode": None, "fullAddress": full}
    if not full:
        return out
    if full.lower().startswith("address:"):
        full = full.split(":", 1)[1].strip()
    out["fullAddress"] = full

    parts = [p.strip() for p in full.split(",") if p.strip()]
    if not parts:
        return out

    # If the last segment is a recognised country name, strip it. This shifts
    # the meaningful "state <zip>" segment back into `parts[-1]` so the rest
    # of the parser can do the right thing for US/UK/etc addresses.
    if parts[-1].lower().rstrip(".") in COUNTRY_TRAILERS:
        parts.pop()

    if not parts:
        return out

    last = parts[-1]
    m = PIN_RE.search(last)
    if m:
        out["postalCode"] = m.group(1)
        state_part = PIN_RE.sub("", last).strip().rstrip(",").strip()
        out["state"] = state_part or None
    else:
        out["state"] = last

    if len(parts) >= 2:
        out["city"] = parts[-2]
    if len(parts) >= 3:
        out["street"] = ", ".join(parts[:-2])
    elif len(parts) == 2:
        out["street"] = parts[0]
    elif len(parts) == 1:
        out["street"] = parts[0]
    return out


def _clean_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    return re.sub(r"\s+", " ", s).strip() or None


def _parse_rating_block(text: Optional[str]) -> tuple[Optional[float], Optional[int]]:
    if not text:
        return None, None
    rating = None
    count = None
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    if m:
        try:
            rating = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"\(([\d,\.]+)\)", text)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            count = int(digits)
    return rating, count


def _extract_place_id_from_url(url: str) -> Optional[str]:
    m = re.search(r"!1s(0x[0-9a-fA-F]+:0x[0-9a-fA-F]+)", url)
    return m.group(1) if m else None


def _extract_place_latlon(url: str) -> Optional[tuple[float, float]]:
    """Pull the place's (lat, lon) out of a Google Maps place URL.

    The URL embeds them as `!8m2!3d<lat>!4d<lon>` near the end.
    """
    if not url:
        return None
    m = re.search(r"!8m2!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", url)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            return None
    return None


def _bbox_contains(bbox: tuple[float, float, float, float], lat: float, lon: float, pad: float = 0.005) -> bool:
    """`bbox` is (south_lat, north_lat, west_lon, east_lon).
    `pad` is a small tolerance (~500m near the equator) so border places aren't dropped."""
    south, north, west, east = bbox
    return (south - pad) <= lat <= (north + pad) and (west - pad) <= lon <= (east + pad)


def _grid_from_bbox(bbox: tuple[float, float, float, float], grid: int) -> list[tuple[float, float]]:
    """Evenly tile a bounding box with `grid x grid` lat/lon points."""
    south, north, west, east = bbox
    if grid <= 1:
        return [((south + north) / 2, (west + east) / 2)]
    lat_step = (north - south) / (grid - 1)
    lon_step = (east - west) / (grid - 1)
    pts: list[tuple[float, float]] = []
    for i in range(grid):
        for j in range(grid):
            pts.append((south + i * lat_step, west + j * lon_step))
    return pts


def _make_grid(center: tuple[float, float], radius_km: float, grid: int) -> list[tuple[float, float]]:
    """Generate `grid x grid` lat/lon points covering a 2*radius_km square centred on `center`.

    Uses a flat-earth approximation, fine for cities and small regions:
        1 deg latitude  ~= 111 km
        1 deg longitude ~= 111 km * cos(latitude)
    """
    if grid <= 1:
        return [center]
    clat, clon = center
    lat_span = (radius_km * 2) / 111.0
    lon_span = (radius_km * 2) / (111.0 * max(math.cos(math.radians(clat)), 0.01))
    lat_step = lat_span / (grid - 1)
    lon_step = lon_span / (grid - 1)
    half = (grid - 1) / 2
    pts: list[tuple[float, float]] = []
    for i in range(grid):
        for j in range(grid):
            pts.append((clat + (i - half) * lat_step, clon + (j - half) * lon_step))
    return pts


def _auto_grid_size(max_results: int) -> int:
    """Pick a sensible NxN grid given how many leads the user asked for.

    Each tile in dense urban areas tends to give ~60-120 unique places before
    Google's per-viewport cap kicks in, so we size for ~80 leads/tile.

    We also engage a 2x2 grid for medium jobs (50-200 leads) so they get the
    benefit of cross-tile parallelism — running 3 tiles concurrently is what
    turns ~500 leads/hr per tile into ~1500 leads/hr aggregate.
    """
    if max_results <= 40:
        return 1
    if max_results <= 200:
        return 2  # 2x2 = 4 viewports + 1 text-search target
    if max_results <= 1000:
        return 3  # 3x3 = 9 viewports — good for mid-size cities
    n = math.ceil(math.sqrt(max_results / 120))
    # Cap at 6x6 = 36 tiles. Coupled with the browser-recycle loop in
    # scrape() (close+relaunch Chromium every N tiles) this stays within
    # Railway's 1GB RAM. The user wants exhaustive coverage of a city
    # ("give me all 800 law firms in Sydney"), so we push the grid wide
    # to fully tile dense urban areas; dedup by place ID handles overlap.
    return max(2, min(int(n), 6))


@dataclass
class _Target:
    """One scrape target. Either a text search (`location` provided) or a
    viewport search (`viewport` provided). When `bbox` is set, any scraped
    place outside that box is dropped — used to enforce "stay within this
    city/region"."""
    keyword: str
    location: str = ""
    viewport: Optional[tuple[float, float, int]] = None  # (lat, lon, zoom)
    bbox: Optional[tuple[float, float, float, float]] = None  # south, north, west, east

    def to_url(self, lang: str = "en") -> str:
        if self.viewport is not None:
            lat, lon, zoom = self.viewport
            # Viewport search: just the keyword in the query string,
            # the geographic context is encoded in the @lat,lon,zoom suffix.
            return (
                f"https://www.google.com/maps/search/{quote_plus(self.keyword)}"
                f"/@{lat:.6f},{lon:.6f},{zoom}z?hl={lang}"
            )
        query = f"{self.keyword} {self.location}".strip()
        return f"https://www.google.com/maps/search/{quote_plus(query)}/?hl={lang}"

    def label(self) -> str:
        if self.viewport is not None:
            lat, lon, _ = self.viewport
            return f"{self.keyword}  @{lat:.4f},{lon:.4f}"
        return f"{self.keyword}  in {self.location}" if self.location else self.keyword


def _normalize_website(raw: Optional[str]) -> Optional[str]:
    """Turn a bare host or full URL into a normal https URL.

    Strips Google tracking params if Google wrapped the URL.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    # Google sometimes wraps the destination as https://www.google.com/url?q=<dest>
    try:
        u = urlparse(raw)
        if u.netloc.endswith("google.com") and u.path.startswith("/url"):
            from urllib.parse import parse_qs
            q = parse_qs(u.query).get("q", [None])[0]
            if q:
                return _normalize_website(q)
    except Exception:
        pass
    return raw


def _website_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        return urlparse(url).netloc.lower().lstrip("www.") or None
    except Exception:
        return None


def _is_good_email(addr: str) -> bool:
    a = addr.lower().strip()
    if not a or "@" not in a:
        return False
    if any(s in a for s in EMAIL_BLOCKLIST_SUBSTR):
        return False
    local, _, domain = a.partition("@")
    if not domain or "." not in domain:
        return False
    # Reject "emails" that are really asset/SVG sprite filenames like
    # logo@1x.svg, icon@2x.png — the domain portion is a file extension.
    if any(domain.endswith(ext) for ext in EMAIL_DOMAIN_IMG_EXT):
        return False
    if EMAIL_REJECT_LOCAL_RE.search(local):
        return False
    # reject absurdly long locals (usually parsing garbage)
    if len(local) > 64:
        return False
    return True


async def _fetch_emails_from_site(
    website: Optional[str],
    http: httpx.AsyncClient,
    max_pages: int = 6,
) -> list[str]:
    """Fetch the business website + a few contact pages and pull email addresses.

    Issues the homepage + contact-page fetches CONCURRENTLY. Previously this
    walked CONTACT_PATHS one-by-one, so a single slow site could burn 30-60s on
    8x8s timeouts. Now all paths fire in parallel and we wait at most ~8s for
    the slowest one.
    """
    if not website:
        return []
    base = _normalize_website(website)
    if not base:
        return []

    paths = CONTACT_PATHS[:max_pages]
    urls = [base.rstrip("/") + p for p in paths]

    async def _fetch_one(u: str) -> list[str]:
        try:
            r = await http.get(u, timeout=8.0, follow_redirects=True)
            if r.status_code >= 400 or not r.text:
                return []
            text = r.text
            out: list[str] = []
            for m in EMAIL_RE.findall(text):
                e = m.strip().rstrip(".").lower()
                if _is_good_email(e):
                    out.append(e)
            # Some sites obfuscate "info [at] domain.com" — try a light decode.
            for m in re.findall(r"([A-Za-z0-9._%+\-]+)\s*\[at\]\s*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text, re.IGNORECASE):
                e = f"{m[0]}@{m[1]}".lower()
                if _is_good_email(e):
                    out.append(e)
            for m in re.findall(r"([A-Za-z0-9._%+\-]+)\s*\(at\)\s*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text, re.IGNORECASE):
                e = f"{m[0]}@{m[1]}".lower()
                if _is_good_email(e):
                    out.append(e)
            return out
        except Exception:
            return []

    found: set[str] = set()
    results = await asyncio.gather(*[_fetch_one(u) for u in urls], return_exceptions=False)
    for batch in results:
        for e in batch:
            found.add(e)

    # Build the "own-domain" set for this site so we can rank emails whose
    # domain matches the business's website ABOVE third-party mentions
    # (e.g. press contacts that get scraped from "in the news" sections).
    own_host = (_website_host(base) or "").lower()
    own_root = own_host.split(".")
    own_root = ".".join(own_root[-2:]) if len(own_root) >= 2 else own_host

    def rank(e: str) -> tuple[int, int, str]:
        local, _, domain = e.partition("@")
        # Tier 1: domain matches the site we crawled. Always prefer these.
        same_domain = 0 if (own_root and (domain == own_host or domain.endswith("." + own_root) or domain == own_root)) else 1
        # Tier 2: local-part role priority. info@ / contact@ etc are usually the
        # right ones to reach out to vs. a personal "john.smith@" pulled from a bio.
        priority = 0
        for i, kw in enumerate((
            "info", "contact", "hello", "sales", "enquiry", "enquiries",
            "office", "admin", "support", "team", "mail",
        )):
            if local == kw or local.startswith(kw + ".") or local.startswith(kw + "-"):
                priority = -10 + i
                break
        return (same_domain, priority, e)

    return sorted(found, key=rank)


# ----- main scraper ---------------------------------------------------------

ScrapeCallback = Callable[[dict], None]


_CHROMIUM_ARGS = [
    "--lang=en-US",
    "--disable-blink-features=AutomationControlled",
    # Docker / container-safety flags. Without these Chromium SIGSEGVs on
    # Railway because /dev/shm is only 64MB by default and the user-namespace
    # sandbox isn't always available.
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--no-zygote",
]


class GoogleMapsScraper:
    def __init__(self, headless: bool = True, lang: str = "en", fetch_emails: bool = True):
        self.headless = headless
        self.lang = lang
        self.fetch_emails = fetch_emails
        self._pw = None
        self._browser: Optional[Browser] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GoogleMapsScraper":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=list(_CHROMIUM_ARGS),
        )
        self._http = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(8.0, connect=4.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Best-effort cleanup. If Chromium has already died (OOM kill, SIGSEGV,
        # transport closed) calling .close() / .stop() will raise — swallow
        # those errors so a long-running job's partial results still land
        # cleanly instead of being marked status=error.
        try:
            if self._http:
                try:
                    await self._http.aclose()
                except Exception:
                    pass
        finally:
            try:
                if self._browser:
                    try:
                        await self._browser.close()
                    except Exception as e:
                        print(f"[scraper] browser.close failed (ignored): {type(e).__name__}: {e}", flush=True)
            finally:
                if self._pw:
                    try:
                        await self._pw.stop()
                    except Exception as e:
                        print(f"[scraper] playwright.stop failed (ignored): {type(e).__name__}: {e}", flush=True)

    async def _recycle_browser(self) -> None:
        """Fully restart Playwright + Chromium to release accumulated memory.
        On Railway's 1GB instance, the Playwright DRIVER process itself
        (not just Chromium) can hit memory limits after many tiles — when
        that happens, even `browser.launch` raises 'transport closed'.
        So we tear down everything and re-init.

        Must be called only when no contexts are open (between batches).
        """
        old_browser = self._browser
        old_pw = self._pw
        self._browser = None
        self._pw = None

        # Best-effort teardown of the old stack. All errors swallowed — the
        # whole point of recycling is to recover from a broken stack.
        if old_browser is not None:
            try:
                await old_browser.close()
            except Exception:
                pass
        if old_pw is not None:
            try:
                await old_pw.stop()
            except Exception:
                pass

        # Fresh start.
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=list(_CHROMIUM_ARGS),
        )
        print("[scraper] playwright + browser recycled — memory released", flush=True)

    async def _new_context(self) -> BrowserContext:
        assert self._browser is not None
        return await self._browser.new_context(
            locale=f"{self.lang}-US" if "-" not in self.lang else self.lang,
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )

    async def _geocode_bbox(
        self, location: str
    ) -> Optional[tuple[float, float, float, float]]:
        """Look up the location's administrative bounding box via OSM Nominatim.

        Returns (south_lat, north_lat, west_lon, east_lon) or None.

        We reject obviously-too-big boxes (country-level, multi-degree spans)
        so a typo like "India" doesn't end up tiling the entire subcontinent.
        """
        if not location.strip() or self._http is None:
            return None
        try:
            r = await self._http.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location, "format": "json", "limit": 1, "addressdetails": 0},
                headers={
                    "User-Agent": "google-maps-lead-scraper/3.0 (local; contact: n/a)",
                    "Accept-Language": "en",
                },
                timeout=12.0,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if not data:
                return None
            bb = data[0].get("boundingbox")
            if not bb or len(bb) != 4:
                return None
            south = float(bb[0])
            north = float(bb[1])
            west = float(bb[2])
            east = float(bb[3])
            # Reject absurdly large boxes (state/country level). Cities are
            # typically <0.5 deg on a side; we allow up to ~3 deg for big
            # metros & smaller districts.
            if (north - south) > 3.0 or (east - west) > 3.0:
                return None
            if north <= south or east <= west:
                return None
            return south, north, west, east
        except Exception:
            return None

    async def _resolve_center(self, location: str) -> Optional[tuple[float, float]]:
        """Open Google Maps for `location` and read the resolved center from the URL.

        We use Google's own geocoder this way (no third-party service, no rate
        limit beyond what we already accept for scraping). Returns `None` if
        Google didn't redirect to a placed URL within a few seconds.
        """
        if not location.strip():
            return None
        assert self._browser is not None
        ctx = await self._new_context()
        page = await ctx.new_page()
        try:
            url = f"https://www.google.com/maps/place/{quote_plus(location)}?hl={self.lang}"
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await self._dismiss_consent(page)
            # Google rewrites the URL after geocoding; give it a moment.
            for _ in range(8):
                m = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+),", page.url)
                if m:
                    return float(m.group(1)), float(m.group(2))
                await page.wait_for_timeout(500)
            return None
        finally:
            await ctx.close()

    async def _dismiss_consent(self, page: Page) -> None:
        try:
            btn = page.locator(
                "button:has-text('Accept all'), button:has-text('Reject all'), "
                "button:has-text('I agree')"
            ).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await page.wait_for_timeout(800)
        except Exception:
            pass

    async def _scroll_feed_until(self, page: Page, target: int, hard_cap: int = 400) -> tuple[int, bool]:
        """Scroll the result feed until `target` cards are loaded or the end-of-list
        marker appears. Returns (count, end_reached)."""
        feed_sel = 'div[role="feed"]'
        try:
            await page.wait_for_selector(feed_sel, timeout=15_000)
        except PlaywrightTimeoutError:
            return 0, True

        seen_count = 0
        stagnant_rounds = 0
        for _ in range(hard_cap):
            cards = page.locator(f'{feed_sel} a[href*="/maps/place/"]')
            count = await cards.count()
            if count >= target:
                return count, False
            if count == seen_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            seen_count = count

            end_marker = page.locator("text=/You.?ve reached the end of the list/i")
            if await end_marker.count() > 0:
                return count, True

            if stagnant_rounds >= 3:
                return count, True

            await page.evaluate(
                """sel => {
                  const el = document.querySelector(sel);
                  if (el) el.scrollTo(0, el.scrollHeight);
                }""",
                feed_sel,
            )
            await page.wait_for_timeout(700)
        return seen_count, False

    async def _extract_card_basic(self, card) -> dict:
        href = await card.get_attribute("href")
        title = await card.get_attribute("aria-label")
        parent = card.locator("xpath=..")
        try:
            snippet = await parent.inner_text(timeout=2000)
        except Exception:
            snippet = ""
        return {"title": _clean_text(title), "url": href, "_snippet": snippet}

    async def _extract_detail_panel(self, page: Page) -> dict:
        out: dict = {
            "phone": None, "website": None, "category": None, "fullAddress": None,
            "rating": None, "reviewsCount": None, "plusCode": None,
        }
        # Wait for any sign that the *place detail* panel has rendered. The
        # previous wait on "div[role='main'] h1" was wrong because that h1
        # exists on the search results page too ("Results"), so the wait
        # returned instantly with stale state and the rest of this function
        # silently extracted nothing.
        detail_signal = (
            'button[data-item-id="address"], '
            'a[data-item-id="authority"], '
            'button[data-item-id^="phone:"], '
            'div.F7nice, '
            'button[jsaction*="category"]'
        )
        try:
            await page.wait_for_selector(detail_signal, timeout=6_000, state="visible")
        except PlaywrightTimeoutError:
            return out
        # Brief settle so reactive panels can finish painting nested fields.
        await page.wait_for_timeout(150)

        try:
            cat = await page.locator('button[jsaction*="category"]').first.inner_text(timeout=2000)
            out["category"] = _clean_text(cat)
        except Exception:
            pass

        try:
            rb = await page.locator("div.F7nice").first.inner_text(timeout=1500)
            r, c = _parse_rating_block(rb)
            out["rating"] = r
            out["reviewsCount"] = c
        except Exception:
            pass

        # First pass: for the website specifically, grab the real <a href="...">
        try:
            link = page.locator('a[data-item-id="authority"]').first
            if await link.count() > 0:
                href = await link.get_attribute("href")
                if href:
                    out["website"] = _normalize_website(href)
        except Exception:
            pass

        # Second pass: walk every data-item-id element for the rest of the fields.
        try:
            els = await page.locator("button[data-item-id], a[data-item-id]").all()
            for el in els:
                try:
                    item_id = (await el.get_attribute("data-item-id")) or ""
                    aria = (await el.get_attribute("aria-label")) or ""
                    aria = _clean_text(aria) or ""
                except Exception:
                    continue
                if item_id == "address":
                    out["fullAddress"] = aria.split(":", 1)[-1].strip() if ":" in aria else aria
                elif item_id == "authority" and not out["website"]:
                    val = aria.split(":", 1)[-1].strip() if ":" in aria else aria
                    out["website"] = _normalize_website(val)
                elif PHONE_DATA_ID_RE.match(item_id):
                    out["phone"] = aria.split(":", 1)[-1].strip() if ":" in aria else aria
                elif item_id == "oloc":
                    out["plusCode"] = aria.split(":", 1)[-1].strip() if ":" in aria else aria
        except Exception:
            pass

        return out

    @staticmethod
    def _lead_in_target(item: dict, target: _Target) -> bool:
        """Decide whether a scraped place belongs in this target's geographic scope.

        Rules:
          1. If the target has no bbox set, accept everything.
          2. If the place's lat/lon (extracted from its Google Maps URL) is
             inside the bbox: accept.
          3. If we have no lat/lon (rare — some URLs lack the !3d!4d tail),
             fall back to address-substring matching against the typed
             location's first two tokens (e.g. "Raipur"). This catches the
             single-place landing case where Google sends us straight to a
             detail page without map metadata.
        """
        bbox = target.bbox
        if bbox is None:
            return True
        latlon = _extract_place_latlon(item.get("url") or "")
        if latlon is not None:
            return _bbox_contains(bbox, latlon[0], latlon[1])
        # No coordinates available: fall back to checking that the typed
        # location's city name appears in the full address.
        full_addr = (item.get("fullAddress") or "").lower()
        loc_tokens = [t.strip().lower() for t in re.split(r"[,/]", target.location or "") if t.strip()]
        if not loc_tokens:
            return True  # nothing to check against
        # accept if any of the first two tokens (typically city + state) appears
        for tok in loc_tokens[:2]:
            if tok and tok in full_addr:
                return True
        return False

    async def _scrape_one_query(
        self,
        target: _Target,
        max_results: int,
        on_result: Optional[ScrapeCallback],
        cancel_event: Optional[asyncio.Event],
        seen_place_ids: set[str],
        *,
        card_workers: int = 5,
    ) -> int:
        """Scrape a single target (text search or viewport search). Returns
        count of NEW (not previously seen) items added.

        Architecture: scroll the feed to collect card URLs upfront, then process
        detail pages in parallel by navigating directly to each place URL on its
        own page (rather than clicking cards one-by-one in a single page). Email
        fetch runs INLINE per card so the final emitted lead is always complete
        with its email attached.
        """
        keyword = target.keyword
        location = target.location
        url = target.to_url(self.lang)
        country_code_hint = _infer_country_code(location)

        ctx = await self._new_context()
        feed_page = await ctx.new_page()
        added = 0
        try:
            await feed_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await self._dismiss_consent(feed_page)

            try:
                await feed_page.wait_for_selector('div[role="feed"], div[role="main"] h1', timeout=20_000)
            except PlaywrightTimeoutError:
                return 0

            # Single-place landing
            if await feed_page.locator('div[role="feed"]').count() == 0:
                detail = await self._extract_detail_panel(feed_page)
                title = await feed_page.locator("div[role='main'] h1").first.inner_text()
                emails = []
                if self.fetch_emails and self._http:
                    try:
                        emails = await _fetch_emails_from_site(detail.get("website"), self._http)
                    except Exception:
                        emails = []
                item = _assemble(title, feed_page.url, "", detail, country_code_hint, keyword, emails)
                pid = item["placeId"]
                if pid and pid in seen_place_ids:
                    return 0
                if not self._lead_in_target(item, target):
                    if pid:
                        seen_place_ids.add(pid)  # remember, but don't deliver
                    return 0
                if pid:
                    seen_place_ids.add(pid)
                if on_result:
                    on_result(item)
                return 1

            # Scroll the feed enough to cover target
            await self._scroll_feed_until(feed_page, target=max_results)
            cards = feed_page.locator('div[role="feed"] a[href*="/maps/place/"]')
            total = await cards.count()

            # Collect ALL card jobs upfront so we can fan them out in parallel.
            # Over-collect by 1.5x because some will be duplicates of already-seen
            # places (from prior tiles) and bbox-filtered drops.
            url_jobs: list[tuple[str, Optional[str], str]] = []
            collect_cap = min(total, int(max_results * 1.5) + 5)
            for i in range(collect_cap):
                if cancel_event and cancel_event.is_set():
                    break
                card = cards.nth(i)
                try:
                    href = await card.get_attribute("href")
                    title = await card.get_attribute("aria-label")
                    try:
                        snippet = await card.locator("xpath=..").inner_text(timeout=1500)
                    except Exception:
                        snippet = ""
                    if not href:
                        continue
                    # Make absolute if relative
                    if href.startswith("/"):
                        href = "https://www.google.com" + href
                    pid_pre = _extract_place_id_from_url(href)
                    if pid_pre and pid_pre in seen_place_ids:
                        continue
                    url_jobs.append((href, _clean_text(title), snippet))
                except Exception:
                    continue

            if not url_jobs:
                return 0

            # Process detail pages in parallel within this tile. All workers share
            # the same browser context (saves cookies/auth setup overhead) but
            # each gets its own page so navigations don't collide.
            sem = asyncio.Semaphore(max(1, card_workers))
            state = {"added": 0}
            state_lock = asyncio.Lock()

            async def _process(href: str, title_hint: Optional[str], snippet: str) -> None:
                async with state_lock:
                    if state["added"] >= max_results:
                        return
                if cancel_event and cancel_event.is_set():
                    return

                async with sem:
                    async with state_lock:
                        if state["added"] >= max_results:
                            return
                    if cancel_event and cancel_event.is_set():
                        return
                    pid_pre = _extract_place_id_from_url(href)
                    if pid_pre and pid_pre in seen_place_ids:
                        return

                    detail: dict = {}
                    final_url = href
                    page_title: Optional[str] = title_hint
                    detail_page = None
                    try:
                        detail_page = await ctx.new_page()
                        await detail_page.goto(href, wait_until="domcontentloaded", timeout=45_000)
                        # Direct nav lands us on the place URL with the detail
                        # panel already in flight; _extract_detail_panel waits
                        # for the panel-specific selectors so we don't need the
                        # click + wait_for_function dance any more.
                        detail = await self._extract_detail_panel(detail_page)
                        final_url = detail_page.url
                        # Prefer the page's H1 over the card's aria-label when
                        # available (more accurate for some places)
                        try:
                            h1 = await detail_page.locator("div[role='main'] h1").first.inner_text(timeout=1500)
                            if h1 and h1.strip() and h1.strip().lower() != "results":
                                page_title = _clean_text(h1)
                        except Exception:
                            pass
                    except Exception:
                        pass
                    finally:
                        if detail_page is not None:
                            try:
                                await detail_page.close()
                            except Exception:
                                pass

                    # Snippet fallback for category/rating
                    if not detail.get("category"):
                        sm = re.search(r"\n[\d\.]+\n([^·\n]+?)\s*·", snippet)
                        if sm:
                            detail["category"] = _clean_text(sm.group(1))
                    if not detail.get("rating"):
                        rm = re.search(r"\n(\d\.\d)\n", snippet)
                        if rm:
                            try:
                                detail["rating"] = float(rm.group(1))
                            except ValueError:
                                pass

                    # Email fetch — INLINE so the emitted lead always carries
                    # its email. _fetch_emails_from_site now fires all contact
                    # paths concurrently so this is bounded at ~6s, not 60s.
                    emails: list[str] = []
                    if self.fetch_emails and self._http and detail.get("website"):
                        try:
                            emails = await _fetch_emails_from_site(detail.get("website"), self._http)
                        except Exception:
                            emails = []

                    item = _assemble(page_title, final_url, snippet, detail, country_code_hint, keyword, emails)
                    pid = item["placeId"]

                    async with state_lock:
                        if state["added"] >= max_results:
                            return
                        if pid and pid in seen_place_ids:
                            return
                        if not self._lead_in_target(item, target):
                            if pid:
                                seen_place_ids.add(pid)
                            return
                        if pid:
                            seen_place_ids.add(pid)
                        state["added"] += 1
                        if on_result:
                            on_result(item)

            await asyncio.gather(*[_process(h, t, s) for h, t, s in url_jobs])
            return state["added"]
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    async def _build_targets(
        self,
        keywords: list[str],
        locations: list[str],
        auto_grid: bool,
        radius_km: float,
        grid_size: Optional[int],
        zoom: int,
        max_results: int,
        restrict_to_location: bool,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> list[_Target]:
        """Expand (keywords, locations) into the full target list to scrape.

        Preferred path: for each location, ask OpenStreetMap Nominatim for the
        location's real administrative bounding box, then tile *only that box*
        into NxN viewport searches. Every Target carries the bbox forward so
        the scraper can drop any place whose coordinates fall outside.

        Fallback path (no bbox available): use a fixed-radius square around the
        Google-resolved centre. We cap the radius hard so a typo can't tile a
        thousand kilometres of country.
        """
        targets: list[_Target] = []
        gs = grid_size if (grid_size and grid_size >= 1) else _auto_grid_size(max_results)

        # Hard safety cap. Even if the user passes 1000 km, we ignore it and
        # use 25 km — otherwise the grid leaks far past the typed city.
        SAFE_RADIUS_KM = min(max(float(radius_km or 25.0), 1.0), 25.0)

        if not auto_grid or gs <= 1:
            for kw in keywords:
                for loc in locations:
                    bbox = None
                    if restrict_to_location and loc:
                        if on_status:
                            on_status(f"Geocoding bbox for '{loc}'…")
                        bbox = await self._geocode_bbox(loc)
                    targets.append(_Target(keyword=kw, location=loc, bbox=bbox))
            return targets

        for loc in locations:
            bbox = None
            grid_points: list[tuple[float, float]] = []

            if loc:
                if on_status:
                    on_status(f"Geocoding bbox for '{loc}'…")
                bbox = await self._geocode_bbox(loc)
                if bbox is not None:
                    grid_points = _grid_from_bbox(bbox, gs)
                    if on_status:
                        south, north, west, east = bbox
                        km_lat = (north - south) * 111.0
                        km_lon = (east - west) * 111.0 * max(math.cos(math.radians((south + north) / 2)), 0.01)
                        on_status(
                            f"'{loc}' bbox ≈ {km_lat:.1f} km × {km_lon:.1f} km, "
                            f"tiling {gs}×{gs} = {gs*gs} viewports"
                        )

            if not grid_points:
                # OSM didn't give us a bbox — fall back to safe-radius grid
                # centred on Google's geocoded centre.
                center = await self._resolve_center(loc) if loc else None
                if center is None:
                    for kw in keywords:
                        targets.append(_Target(keyword=kw, location=loc, bbox=None))
                    if on_status:
                        on_status(f"Could not geocode '{loc}' — falling back to plain text search")
                    continue
                if on_status:
                    on_status(f"'{loc}' no bbox from OSM; using safe radius {SAFE_RADIUS_KM:.0f} km around {center}")
                grid_points = _make_grid(center, radius_km=SAFE_RADIUS_KM, grid=gs)
                # build an approximate bbox so filtering still works
                clat, clon = center
                lat_span = SAFE_RADIUS_KM / 111.0
                lon_span = SAFE_RADIUS_KM / (111.0 * max(math.cos(math.radians(clat)), 0.01))
                bbox = (clat - lat_span, clat + lat_span, clon - lon_span, clon + lon_span)

            for kw in keywords:
                # Plain text search first — highest-relevance hits
                targets.append(_Target(keyword=kw, location=loc, bbox=bbox))
                for (lat, lon) in grid_points:
                    targets.append(_Target(keyword=kw, location=loc, viewport=(lat, lon, zoom), bbox=bbox))
        return targets

    async def scrape(
        self,
        keyword: str | Iterable[str],
        location: str | Iterable[str],
        max_results: int = 20,
        on_result: Optional[ScrapeCallback] = None,
        cancel_event: Optional[asyncio.Event] = None,
        *,
        auto_grid: bool = False,
        radius_km: float = 10.0,
        grid_size: Optional[int] = None,
        zoom: int = 14,
        restrict_to_location: bool = True,
        on_status: Optional[Callable[[str], None]] = None,
        tile_workers: int = 3,
        card_workers: int = 5,
    ) -> list[dict]:
        """Run one or many searches and collect up to `max_results` unique places.

        Args:
            keyword: single keyword or list of keywords
            location: single location or list of locations
            max_results: total upper bound across all keyword/location/tile combos
            on_result: called with each new lead dict as soon as it's scraped
            cancel_event: optional asyncio.Event; setting it stops the loop
            auto_grid: if True, expand each location into a NxN grid of map
                viewport searches so we can break past Google's ~120/query cap
            radius_km: half-side of the grid square, in km (default 10)
            grid_size: NxN grid dimension. If None, auto-pick from max_results
            zoom: Google Maps zoom for viewport tiles (13-15 reasonable)
            on_status: optional callback for human-readable progress strings
        """
        keywords = [keyword] if isinstance(keyword, str) else list(keyword)
        locations = [location] if isinstance(location, str) else list(location)
        keywords = [k.strip() for k in keywords if k and k.strip()]
        locations = [l.strip() for l in locations] if locations else [""]
        if not locations:
            locations = [""]

        collected: list[dict] = []
        seen_place_ids: set[str] = set()

        def _record(item: dict) -> None:
            # Hard cap here so parallel tiles never over-emit past max_results.
            if len(collected) >= max_results:
                return
            collected.append(item)
            if on_result:
                on_result(item)

        targets = await self._build_targets(
            keywords=keywords,
            locations=locations,
            auto_grid=auto_grid,
            radius_km=radius_km,
            grid_size=grid_size,
            zoom=zoom,
            max_results=max_results,
            restrict_to_location=restrict_to_location,
            on_status=on_status,
        )
        if on_status:
            on_status(
                f"Scraping {len(targets)} target(s) with tile_workers={tile_workers}, "
                f"card_workers={card_workers}; deduping by place ID."
            )

        # Tiles run in parallel, bounded by a semaphore. Each tile internally
        # parallelizes card processing too, so effective concurrency is
        # tile_workers * card_workers detail pages at once.
        sem = asyncio.Semaphore(max(1, tile_workers))
        # Early-bail state: count consecutive tiles that returned zero new
        # leads. If we've tried EARLY_BAIL_TILES tiles AND still have zero
        # collected, abort — the location is almost certainly bad (country
        # name, typo, middle-of-nowhere). Prevents the "Australia" / typo
        # case grinding for 10+ minutes returning nothing.
        zero_runs = {"n": 0}
        EARLY_BAIL_TILES = 3
        # `cancel_event` may be None when called from the CLI/library. Make
        # sure we always have one so the bail logic can signal abort.
        if cancel_event is None:
            cancel_event = asyncio.Event()

        async def _run_target(i: int, t: _Target) -> None:
            async with sem:
                if cancel_event.is_set():
                    return
                remaining = max_results - len(collected)
                if remaining <= 0:
                    return
                if on_status:
                    on_status(f"[{i}/{len(targets)}] {t.label()} — have {len(collected)} so far")
                pre_count = len(collected)
                try:
                    await self._scrape_one_query(
                        target=t,
                        max_results=remaining,
                        on_result=_record,
                        cancel_event=cancel_event,
                        seen_place_ids=seen_place_ids,
                        card_workers=card_workers,
                    )
                except Exception as e:  # one tile failing must not kill the whole job
                    if on_status:
                        on_status(f"[{i}/{len(targets)}] FAILED: {type(e).__name__}: {e}")
                added = len(collected) - pre_count
                if added == 0:
                    zero_runs["n"] += 1
                    if len(collected) == 0 and zero_runs["n"] >= EARLY_BAIL_TILES:
                        if on_status:
                            on_status(
                                f"Aborting: {zero_runs['n']} tiles returned zero leads. "
                                f"The location '{t.location or '<empty>'}' is probably too broad "
                                f"or misspelled. Try a specific city name."
                            )
                        cancel_event.set()
                else:
                    zero_runs["n"] = 0

        # Process targets in batches and recycle Chromium between batches.
        # WHY: each tile creates a fresh BrowserContext, but the underlying
        # Chromium process gradually bloats (handle/RPC accumulation). Past
        # ~10 tiles on Railway's 1GB instance the process freezes the whole
        # asyncio loop. Closing+relaunching releases all that memory.
        RECYCLE_EVERY = max(1, tile_workers * 2)  # e.g. tile_workers=2 → every 4 tiles
        for batch_start in range(0, len(targets), RECYCLE_EVERY):
            if cancel_event.is_set():
                break
            batch = targets[batch_start:batch_start + RECYCLE_EVERY]
            await asyncio.gather(*[
                _run_target(batch_start + j + 1, t) for j, t in enumerate(batch)
            ])
            # Recycle between batches only (not after the final batch).
            if not cancel_event.is_set() and (batch_start + RECYCLE_EVERY) < len(targets):
                if len(collected) < max_results:
                    if on_status:
                        on_status(f"Recycling browser to free memory ({len(collected)}/{max_results} so far)…")
                    try:
                        await self._recycle_browser()
                    except Exception as e:
                        if on_status:
                            on_status(f"Browser recycle failed: {type(e).__name__}: {e}")
                        break  # can't continue without a browser
        return collected


def _assemble(
    title: Optional[str],
    url: Optional[str],
    snippet: str,
    detail: dict,
    country_code_hint: Optional[str],
    search_string: str,
    emails: list[str],
) -> dict:
    addr_parts = _parse_address(detail.get("fullAddress"))
    country_code = country_code_hint or _infer_country_code("", detail.get("fullAddress"))
    primary_email = emails[0] if emails else None
    return {
        "title": title,
        "totalScore": detail.get("rating"),
        "reviewsCount": detail.get("reviewsCount"),
        "street": addr_parts["street"],
        "city": addr_parts["city"],
        "state": addr_parts["state"],
        "postalCode": addr_parts["postalCode"],
        "countryCode": country_code,
        "website": detail.get("website"),
        "phone": detail.get("phone"),
        "email": primary_email,
        "emails": emails,
        "categories": [detail.get("category")] if detail.get("category") else [],
        "categoryName": detail.get("category"),
        "plusCode": detail.get("plusCode"),
        "url": url,
        "fullAddress": detail.get("fullAddress") or addr_parts["fullAddress"],
        "placeId": _extract_place_id_from_url(url or ""),
        "searchString": search_string,
    }


# convenience CLI
async def _cli():
    import json, sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    keyword = sys.argv[1] if len(sys.argv) > 1 else "Gold Jewellery"
    location = sys.argv[2] if len(sys.argv) > 2 else "Raipur, Chhattisgarh, India"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    counter = {"n": 0}
    def _log(x: dict) -> None:
        counter["n"] += 1
        print(f"  [{counter['n']}] {x.get('title')!r}  email={x.get('email')}  site={x.get('website')}")
    async with GoogleMapsScraper(headless=True) as s:
        items = await s.scrape(keyword, location, max_results=n, on_result=_log)
        print(json.dumps(items, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(_cli())
