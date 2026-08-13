"""
Step 1: prove that a Cloudflare clearance solved once with Camoufox can be
reused for plain HTTP requests (no browser) on OTHER pages.

Flow:
    1. Solve the challenge on ARTIST_A with Camoufox.
    2. Extract cf_clearance cookie + the exact User-Agent Camoufox used.
    3. Hit ARTIST_B with curl_cffi (Firefox TLS impersonation) using that
       cookie + UA. No browser.
    4. If step 3 returns real content -> the cookie-reuse trick works.

Setup:
    pip install camoufox[geoip] curl_cffi

Run:
    python cookie_reuse_test.py
"""

import time
from curl_cffi import requests as cf_requests
from camoufox.sync_api import Camoufox

ARTIST_A = "Structure"   # solved with the browser
ARTIST_B = "2Pac"        # fetched with plain HTTP using the reused cookie

BASE = "https://www.whosampled.com"


def solve_with_browser(artist: str):
    """Solve the CF challenge with Camoufox, return (cookies, user_agent)."""
    url = f"{BASE}/{artist}/"
    print(f"[browser] solving challenge for {url}")

    with Camoufox(headless=False, humanize=True, os="macos") as browser:
        page = browser.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")

        # Wait for the title to STABILIZE on something that isn't a
        # challenge/transition state, rather than racing for a DOM element
        # (the CF interstitial has its own h1, which caused a false
        # positive last time).
        stable_title = None
        stable_count = 0
        deadline = time.time() + 30
        last_title = None

        while time.time() < deadline:
            title = page.title()
            transitional = ("Just a moment" in title) or ("Loading" in title)

            if not transitional and title == last_title:
                stable_count += 1
            else:
                stable_count = 0

            last_title = title

            if stable_count >= 2:  # same non-transitional title twice in a row
                stable_title = title
                break

            page.wait_for_timeout(500)

        print(f"[browser] final title: {stable_title!r}")

        if stable_title is None or "moment" in (stable_title or "").lower():
            raise RuntimeError("Challenge never resolved within the deadline")

        cookies = page.context.cookies()
        user_agent = page.evaluate("navigator.userAgent")

        cf_cookie = next((c for c in cookies if c["name"] == "cf_clearance"), None)
        if not cf_cookie:
            raise RuntimeError("No cf_clearance cookie found after solving")

        print(f"[browser] got cf_clearance (expires {cf_cookie.get('expires')})")
        return cookies, user_agent


def fetch_with_cookies(artist: str, cookies, user_agent: str):
    """Fetch a DIFFERENT page with plain HTTP, reusing the solved cookie."""
    url = f"{BASE}/{artist}/"
    print(f"\n[curl_cffi] fetching {url} with reused cookie + Firefox TLS impersonation, no real browser")

    jar = {c["name"]: c["value"] for c in cookies}
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with cf_requests.Session(impersonate="firefox135") as client:
        resp = client.get(url, cookies=jar, headers=headers, timeout=20)

    print(f"[curl_cffi] status: {resp.status_code}")

    if "Just a moment" in resp.text or "cf-challenge" in resp.text.lower():
        print("⚠️  Got challenged again — cookie reuse did NOT work as-is.")
        print("    (Could mean: cookie is IP-bound and this process has a different")
        print("    egress IP than Camoufox, or headers need to match more closely.)")
        return False

    # crude proof-of-life: look for the artist name somewhere in the HTML
    if artist.lower() in resp.text.lower():
        print(f"✅ Got real content via plain HTTP. Found {artist!r} in the page.")
        return True
    else:
        print("⚠️  No challenge page, but couldn't confirm artist name in HTML.")
        print("First 300 chars of response:")
        print(resp.text[:300])
        return False


def main():
    cookies, user_agent = solve_with_browser(ARTIST_A)
    fetch_with_cookies(ARTIST_B, cookies, user_agent)


if __name__ == "__main__":
    main()