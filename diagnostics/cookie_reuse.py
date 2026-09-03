"""Check whether Camoufox clearance can be reused by curl_cffi.

Run from the repository root with:

    python -m diagnostics.cookie_reuse
"""

from typing import Any
from urllib.parse import urlsplit

from curl_cffi import requests as cf_requests

from wsmpld.upstream import (
    CURL_CFFI_FIREFOX_MAJOR_VERSION,
    CamoufoxClearanceAcquirer,
    firefox_major_version,
)

BASE_URL = "https://www.whosampled.com"
BROWSER_ARTIST = "Structure"
BROWSERLESS_ARTIST = "Kanye-West"


def _solve_with_browser(artist: str) -> tuple[list[dict[str, Any]], str]:
    url = f"{BASE_URL}/{artist}/"
    print(f"[browser] solving challenge for {url}")
    clearance = CamoufoxClearanceAcquirer()(90.0)
    cookies: list[dict[str, Any]] = [
        {"name": name, "value": value}
        for name, value in clearance.cookies.items()
    ]
    print("[browser] acquired usable WhoSampled clearance")
    return cookies, clearance.user_agent


def _fetch_with_cookies(
    artist: str, cookies: list[dict[str, Any]], user_agent: str
) -> bool:
    url = f"{BASE_URL}/{artist}/"
    print(f"\n[curl_cffi] fetching {url} with reused clearance, no browser")
    try:
        browser_major = firefox_major_version(user_agent)
    except ValueError:
        print("[handoff] browser major: unparseable")
        return False
    impersonation_profile = f"firefox{browser_major}"
    print(f"[handoff] browser major: {browser_major}")
    print(f"[handoff] impersonation profile: {impersonation_profile}")
    print(
        "[handoff] cookie names: "
        + ", ".join(sorted(str(cookie["name"]) for cookie in cookies))
    )
    if browser_major != CURL_CFFI_FIREFOX_MAJOR_VERSION:
        print(
            "[handoff] incompatible browser major; "
            f"expected {CURL_CFFI_FIREFOX_MAJOR_VERSION}"
        )
        return False
    jar = {str(cookie["name"]): str(cookie["value"]) for cookie in cookies}
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    with cf_requests.Session(impersonate=impersonation_profile) as client:
        response = client.get(url, cookies=jar, headers=headers, timeout=20)

    resolved_host = urlsplit(str(getattr(response, "url", url))).hostname
    challenged = (
        "just a moment" in response.text.lower()
        or "cf-challenge" in response.text.lower()
    )
    print(f"[curl_cffi] resolved host: {resolved_host}")
    print(f"[curl_cffi] status: {response.status_code}")
    print(f"[curl_cffi] challenged: {challenged}")
    if challenged:
        print("Browserless fetch was challenged. Cookie reuse failed.")
        return False
    if response.status_code != 200:
        print(f"Browserless fetch returned unexpected status {response.status_code}.")
        return False
    if artist.lower() not in response.text.lower():
        print("Response was not challenged, but the artist name was absent.")
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
