"""Check whether visible Camoufox can acquire a real WhoSampled page.

Run from the repository root with:

    python -m diagnostics.camoufox_clearance Structure
"""

import sys
import time

from camoufox.sync_api import Camoufox


def run_diagnostic() -> bool:
    artist = sys.argv[1] if len(sys.argv) > 1 else "Structure"
    url = f"https://www.whosampled.com/{artist}/"
    print(f"Target: {url}\n")

    with Camoufox(headless=False, humanize=True, os="macos") as browser:
        page = browser.new_page()
        start = time.time()
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")

        try:
            heading = page.locator("h2").first.inner_text(timeout=25_000)
            elapsed = time.time() - start
            print(f"Page title: {page.title()}")
            print(f"Loaded in: {elapsed:.1f}s")
            print(f"\nGot past the challenge. Artist heading: {heading!r}")
            succeeded = True
        except Exception as error:
            elapsed = time.time() - start
            print(f"Page title: {page.title()}")
            print(f"Loaded in: {elapsed:.1f}s")
            content = page.content()
            if "Just a moment" in content or "cf-challenge" in content.lower():
                print("\nStill stuck on the Cloudflare challenge page.")
            else:
                print(f"\nNo artist heading was found: {error}")
                print("First 500 characters of body text:")
                print(page.locator("body").inner_text()[:500])
            succeeded = False

        page.screenshot(path="whosampled_test.png")
        print("\nSaved whosampled_test.png for visual confirmation.")
        return succeeded


def main() -> None:
    raise SystemExit(0 if run_diagnostic() else 1)


if __name__ == "__main__":
    main()
