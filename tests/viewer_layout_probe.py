import json
import sys

from playwright.sync_api import sync_playwright


def main():
    url = sys.argv[1]
    selector = sys.argv[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".row", timeout=10000)
        page.wait_for_function("() => document.getElementById('status').textContent !== 'Loading'", timeout=10000)
        page.wait_for_selector("#pane h2", timeout=10000)
        rows = page.locator(".row")
        count = rows.count()
        if count < 20:
            raise SystemExit("expected a long recording list, got %s" % count)
        page.locator(selector).click()
        page.wait_for_function(
            "(id) => document.getElementById(id) && document.getElementById(id).getAttribute('aria-current') === 'true'",
            arg=selector[1:],
            timeout=5000,
        )
        metrics = page.evaluate(
            """(id) => {
              const header = document.querySelector('header');
              const pane = document.getElementById('pane');
              const title = pane && pane.querySelector('h2');
              const footer = document.getElementById('player-bar');
              const list = document.getElementById('list');
              const selected = document.getElementById(id);
              const box = (el) => {
                if (!el) return null;
                const r = el.getBoundingClientRect();
                const visible = Math.max(0, Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0));
                return {
                  top: r.top,
                  bottom: r.bottom,
                  height: r.height,
                  width: r.width,
                  visible,
                  inView: r.bottom > 0 && r.top < window.innerHeight && r.height > 0 && r.width > 0
                };
              };
              return {
                scrollY: window.scrollY,
                scrollHeight: document.documentElement.scrollHeight,
                clientHeight: document.documentElement.clientHeight,
                bodyOverflow: getComputedStyle(document.body).overflow,
                htmlOverflow: getComputedStyle(document.documentElement).overflow,
                header: box(header),
                pane: box(pane),
                title: box(title),
                footer: box(footer),
                list: {
                  scrollHeight: list.scrollHeight,
                  clientHeight: list.clientHeight,
                  scrollTop: list.scrollTop
                },
                selected: box(selected),
                titleText: title ? title.textContent : "",
                paneChildCount: pane ? pane.childElementCount : 0,
                paneText: pane ? pane.innerText.slice(0, 400) : "",
                nowPlaying: document.getElementById('now-playing').textContent
              };
            }""",
            selector[1:],
        )
        metrics["pageErrors"] = errors
        metrics["rowCount"] = count
        print(json.dumps(metrics))
        browser.close()


if __name__ == "__main__":
    main()
