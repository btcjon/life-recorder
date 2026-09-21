import json
import sys

from playwright.sync_api import sync_playwright


def card_metrics(page):
    return page.evaluate(
        """() => {
          const cards = [...document.querySelectorAll('#pane .group')];
          return cards.map((card) => {
            const play = card.querySelector('.play-toggle');
            const replay = card.querySelector('.replay');
            const meter = card.querySelector('progress');
            const clock = card.querySelector('.group-time');
            const pill = card.querySelector('.pill');
            return {
              key: card.getAttribute('data-group-key'),
              start: card.getAttribute('data-start'),
              end: card.getAttribute('data-end'),
              active: card.classList.contains('active'),
              text: (card.innerText || '').slice(0, 400),
              play: play ? play.textContent : null,
              playPressed: play ? play.getAttribute('aria-pressed') : null,
              playHeight: play ? play.getBoundingClientRect().height : 0,
              replay: replay ? replay.textContent : null,
              replayHeight: replay ? replay.getBoundingClientRect().height : 0,
              clock: clock ? clock.textContent : null,
              progressMax: meter ? Number(meter.max) : null,
              progressValue: meter ? Number(meter.value) : null,
              pill: pill ? pill.textContent : null,
              hasPicker: !!pill,
            };
          });
        }"""
    )


def main():
    url = sys.argv[1]
    selector = sys.argv[2]
    desktop_shot = sys.argv[3]
    mobile_shot = sys.argv[4]
    aba_selector = sys.argv[5] if len(sys.argv) > 5 else ""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".row", timeout=10000)
        page.wait_for_function("() => document.getElementById('status').textContent !== 'Loading'", timeout=10000)
        page.locator(selector).click()
        page.get_by_role("button", name="Speaker turns").click()
        page.wait_for_selector("#pane .group .play-toggle", timeout=5000)
        desktop_cards = card_metrics(page)
        page.locator("#pane .group").first.screenshot(path=desktop_shot)
        page.locator("#pane .group .play-toggle").first.click()
        page.wait_for_function(
            "() => document.querySelector('#pane .group.active .play-toggle')?.textContent === 'Pause'",
            timeout=5000,
        )
        after_first = card_metrics(page)
        page.locator("#pane .group .play-toggle").nth(1).click()
        page.wait_for_function(
            """() => {
              const cards = [...document.querySelectorAll('#pane .group')];
              return cards.length >= 2 && !cards[0].classList.contains('active')
                && cards[1].classList.contains('active')
                && cards[1].querySelector('.play-toggle')?.textContent === 'Pause';
            }""",
            timeout=5000,
        )
        after_second = card_metrics(page)
        page.locator("#pane .group .replay").nth(1).click()
        page.get_by_role("button", name="Play full recording").click()
        page.wait_for_function(
            "() => ![...document.querySelectorAll('#pane .group')].some((card) => card.classList.contains('active'))",
            timeout=5000,
        )
        after_full = card_metrics(page)
        aba_cards = []
        if aba_selector:
            page.locator(aba_selector).click()
            page.get_by_role("button", name="Speaker turns").click()
            page.wait_for_selector("#pane .group .play-toggle", timeout=5000)
            aba_cards = card_metrics(page)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        page.locator(selector).click()
        page.get_by_role("button", name="Speaker turns").click()
        page.wait_for_selector("#pane .group .play-toggle", timeout=5000)
        mobile_cards = card_metrics(page)
        page.locator("#pane .group").first.screenshot(path=mobile_shot)
        print(json.dumps({
            "pageErrors": errors,
            "desktopCards": desktop_cards,
            "afterFirstPlay": after_first,
            "afterSecondPlay": after_second,
            "afterFull": after_full,
            "abaCards": aba_cards,
            "mobileCards": mobile_cards,
        }))
        browser.close()


if __name__ == "__main__":
    main()
