"""Check whether Camoufox clearance can be reused by curl_cffi.

Run from the repository root with:

    python -m diagnostics.cookie_reuse
"""

import time
from typing import Any

from camoufox.sync_api import Camoufox
from curl_cffi import requests as cf_requests

BASE_URL = "https://www.whosampled.com"
BROWSER_ARTIST = "Structure"
BROWSERLESS_ARTIST = "Kanye-West"


def _solve_with_browser(artist: str) -> tuple[list[dict[str, Any]], str]:
    url = f"{BASE_URL}/{artist}/"
    print(f"[browser] solving challenge for {url}")

    with Camoufox(headless=False, humanize=True, os="macos") as browser:
        page = browser.new_page()
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        stable_title: str | None = None
        stable_count = 0
        deadline = time.time() + 30
        last_title: str | None = None

        while time.time() < deadline:
            title = page.title()
            transitional = "Just a moment" in title or "Loading" in title
            stable_count = stable_count + 1 if not transitional and title == last_title else 0
            last_title = title
            if stable_count >= 2:
                stable_title = title
                break
            page.wait_for_timeout(500)

        print(f"[browser] final title: {stable_title!r}")
        if stable_title is None or "moment" in stable_title.lower():
            raise RuntimeError("Challenge never resolved within the deadline")

        cookies: list[dict[str, Any]] = page.context.cookies()
        user_agent = str(page.evaluate("navigator.userAgent"))
        clearance_cookie = next(
            (cookie for cookie in cookies if cookie["name"] == "cf_clearance"), None
        )
        if clearance_cookie is None:
            raise RuntimeError("No cf_clearance cookie found after solving")

        print(f"[browser] got cf_clearance (expires {clearance_cookie.get('expires')})")
        return cookies, user_agent


def _fetch_with_cookies(
    artist: str, cookies: list[dict[str, Any]], user_agent: str
) -> bool:
    url = f"{BASE_URL}/{artist}/"
    print(f"\n[curl_cffi] fetching {url} with reused clearance, no browser")
    jar = {str(cookie["name"]): str(cookie["value"]) for cookie in cookies}
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with cf_requests.Session(impersonate="firefox135") as client:
        response = client.get(url, cookies=jar, headers=headers, timeout=20)

    print(f"[curl_cffi] status: {response.status_code}")
    if "Just a moment" in response.text or "cf-challenge" in response.text.lower():
        print("Browserless fetch was challenged. Cookie reuse failed.")
        return False
    if response.status_code != 200:
        print(f"Browserless fetch returned unexpected status {response.status_code}.")
        return False
    if artist.lower() not in response.text.lower():
        print("Response was not challenged, but the artist name was absent.")
        print(f"First 300 characters: {response.text[:300]}")
        return False
    print(f"Got real browserless content for {artist!r}.")
    return True


def run_diagnostic() -> bool:
    cookies, user_agent = _solve_with_browser(BROWSER_ARTIST)
    return _fetch_with_cookies(BROWSERLESS_ARTIST, cookies, user_agent)


def main() -> None:
    raise SystemExit(0 if run_diagnostic() else 1)


if __name__ == "__main__":
    main()
