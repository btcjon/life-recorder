"""Rendered browser probe against a synthetic, private test receiver."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def bounds(page):
    return page.evaluate("""() => ({
      width: window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      reviewVisible: !!document.querySelector('#review-view:not([hidden])'),
      cardCount: document.querySelectorAll('.review-card').length,
      cardWidth: document.querySelector('.review-card')?.getBoundingClientRect().width || 0,
      playHeight: document.querySelector('.review-card .play-toggle')?.getBoundingClientRect().height || 0
    })""")


def main(url: str, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        errors = []
        desktop = browser.new_page(viewport={"width": 1280, "height": 800})
        desktop.on("pageerror", lambda error: errors.append(str(error)))
        desktop.goto(url, wait_until="domcontentloaded")
        desktop.locator("#tab-review").click()
        desktop.locator(".review-card").first.wait_for(timeout=10000)
        desktop_metrics = bounds(desktop)
        desktop.screenshot(path=str(output / "review-desktop.png"), full_page=True)
        self_choice = desktop.locator(".review-card select").first
        unselected = self_choice.input_value() == ""

        mobile_context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        mobile = mobile_context.new_page()
        mobile.on("pageerror", lambda error: errors.append(str(error)))
        mobile.goto(url, wait_until="domcontentloaded")
        mobile.locator("#tab-review").click()
        mobile.locator(".review-card").first.wait_for(timeout=10000)
        mobile_metrics = bounds(mobile)
        mobile.screenshot(path=str(output / "review-mobile.png"), full_page=True)

        self_choice.select_option(index=1)
        desktop.locator(".review-card .review-confirm").first.click()
        desktop.locator(".review-card").first.wait_for(state="detached", timeout=10000)
        desktop.locator("#tab-recordings").click()
        desktop.locator(".event-edit").first.click()
        desktop.get_by_label("Event title").fill("Synthetic meeting")
        desktop.locator(".event-form button").filter(has_text="Save").click()
        desktop.get_by_text("Synthetic meeting", exact=True).wait_for(timeout=10000)
        result = {
            "desktop": desktop_metrics,
            "mobile": mobile_metrics,
            "no_preselected_name": unselected,
            "confirmed_via_ui": True,
            "event_edited_via_ui": True,
            "page_errors": errors,
        }
        print(json.dumps(result, sort_keys=True))
        mobile_context.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
