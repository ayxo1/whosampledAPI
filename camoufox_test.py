"""
Minimal test: can Camoufox get past WhoSampled's Cloudflare protection
well enough to scrape a single artist page?

Setup:
    pip install camoufox[geoip]
    python -m camoufox fetch      # downloads the patched Firefox binary

Run:
    python camoufox_test.py "Structure"
    python camoufox_test.py "2Pac"
"""

import sys
import time
from camoufox.sync_api import Camoufox

ARTIST = sys.argv[1] if len(sys.argv) > 1 else "Structure"
URL = f"https://www.whosampled.com/{ARTIST}/"


def main():
    print(f"Target: {URL}\n")

    with Camoufox(
        headless=False,      # watch it happen, headless is scored differently by CF
        humanize=True,       # human-like mouse movement, helps against behavioral checks
        os="macos",          # or "windows" / "linux"
    ) as browser:
        page = browser.new_page()

        start = time.time()
        page.goto(URL, timeout=30000, wait_until="domcontentloaded")

        try:
            heading = page.locator("h2").first.inner_text(timeout=25000)
            elapsed = time.time() - start
            print(f"Page title: {page.title()}")
            print(f"Loaded in: {elapsed:.1f}s")
            print(f"\n✅ Got past the challenge. Artist heading: {heading!r}")
        except Exception as e:
            elapsed = time.time() - start
            print(f"Page title: {page.title()}")
            print(f"Loaded in: {elapsed:.1f}s")
            content = page.content()
            if "Just a moment" in content or "cf-challenge" in content.lower():
                print("\n⚠️  Still stuck on the Cloudflare challenge page — did NOT get through.")
            else:
                print(f"\n⚠️  Not a challenge page, but no h1 found: {e}")
                print("Dumping first 500 chars of body text for inspection:")
                print(page.locator("body").inner_text()[:500])

        page.screenshot(path="whosampled_test.png")
        print("\nSaved screenshot to whosampled_test.png for visual confirmation.")


if __name__ == "__main__":
    main()