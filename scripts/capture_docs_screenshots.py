"""Capture documentation screenshots for the signup-app UI.

Run with the mock LiteLLM on :4000 and the app on :8765 already up.
Screenshots land in docs/images/.

Usage:
    uv run python scripts/capture_docs_screenshots.py
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

CHROMIUM_PATH = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
APP_BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# Reverse proxy header injection mirrors how the app runs in production;
# we use the debug user header so the test pages render the same DOM
# the deployed app would.
HEADERS = {"X-User-Email": "alice@example.com"}


def capture(page, url, selector_to_wait, out_path, full_page=True):
    page.goto(url, wait_until="networkidle")
    if selector_to_wait:
        page.wait_for_selector(selector_to_wait, state="visible")
    # A short settle delay for the time-series SVG to finish layout.
    page.wait_for_timeout(400)
    page.screenshot(path=str(out_path), full_page=full_page)
    print(f"wrote {out_path}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            extra_http_headers=HEADERS,
            device_scale_factor=2,
        )
        page = context.new_page()

        capture(
            page,
            f"{APP_BASE}/",
            "#keys-table",
            OUT / "keys-page.png",
        )

        capture(
            page,
            f"{APP_BASE}/dashboard",
            "#dash-content",
            OUT / "dashboard-overview.png",
        )

        # Expand the first project so the task drill-down is visible in
        # the project-section close-up.
        page.click(".project-row")
        page.wait_for_timeout(200)
        section = page.locator("section[aria-labelledby='projects-heading']")
        section.screenshot(path=str(OUT / "dashboard-projects.png"))
        print(f"wrote {OUT / 'dashboard-projects.png'}")

        budget = page.locator("section[aria-labelledby='budget-heading']")
        budget.screenshot(path=str(OUT / "dashboard-budget.png"))
        print(f"wrote {OUT / 'dashboard-budget.png'}")

        chart = page.locator("section[aria-labelledby='ts-heading']")
        chart.screenshot(path=str(OUT / "dashboard-timeseries.png"))
        print(f"wrote {OUT / 'dashboard-timeseries.png'}")

        browser.close()


if __name__ == "__main__":
    main()
