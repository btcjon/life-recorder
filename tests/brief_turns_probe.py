"""Exercise short-turn disclosure using synthetic audio and speaker data."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def check(page, url, mixed, tiny, output, size):
    page.goto(url, wait_until="domcontentloaded")
    page.locator("#day").fill("2026-09-28")
    page.locator("#refresh").click()
    page.locator("#filters").evaluate("e => e.open = true")
    page.locator("#flat-list").check()
    page.locator("#recording-" + mixed).click()
    page.get_by_role("button", name="Transcript", exact=True).click()
    details = page.locator("#pane .brief-turns")
    details.wait_for()
    assert details.count() == 1, "Use one disclosure for all brief turns"
    assert not details.evaluate("e => e.open"), "Mixed recording starts collapsed"
    ordinary = page.locator("#pane .speakers > .identity-anchor .pill")
    ordinary_count = ordinary.count()
    assert ordinary_count == 3
    assert any("51.3–52.3s" in t for t in ordinary.all_text_contents()), "Exactly one second stays ordinary"
    assert page.locator("#pane .brief-turns-pills .pill:visible").count() == 0
    identities = page.locator("#pane [data-identity-key]").evaluate_all("es => es.map(e => e.dataset.identityKey)")
    assert len(set(identities)) == 5
    page.screenshot(path=str(output / (size + "-collapsed.png")), full_page=True)
    details.locator("summary").click()
    brief = page.locator("#pane .brief-turns-pills .pill")
    brief_count = brief.count()
    assert brief_count == 2 and brief.first.is_visible()
    key = brief.first.get_attribute("data-identity-key")
    brief.first.click()
    page.locator("#pane .popover").wait_for(state="visible")
    assert page.locator("#pane .brief-turns").evaluate("e => e.open")
    assert set(identities) == set(page.locator("#pane [data-identity-key]").evaluate_all("es => es.map(e => e.dataset.identityKey)"))
    page.wait_for_function("() => { const p=document.getElementById('player'); return p.currentTime >= 20.09 && p.currentTime <= 20.6; }")
    page.wait_for_function("() => { const p=document.getElementById('player'); return p.paused && p.currentTime >= 20.1 && p.currentTime <= 20.6; }")
    playback_stops = page.evaluate("() => { const p=document.getElementById('player'); return p.paused && p.currentTime >= 20.1 && p.currentTime <= 20.6; }")
    editor_visible = page.locator("#pane .popover").is_visible()
    expanded_overflow = page.evaluate("() => document.documentElement.scrollWidth > innerWidth + 2")
    assert not expanded_overflow
    page.keyboard.press("Escape")
    page.wait_for_function("key => document.activeElement?.dataset.identityKey === key", arg=key)
    assert page.locator("#pane .brief-turns").evaluate("e => e.open")
    page.locator("#pane .brief-turns-pills .pill").first.click()
    with page.expect_response(lambda r: r.request.method == "POST" and "/label" in r.url):
        page.locator("#pane .popover .person-option", has_text="Synthetic person" if size == "desktop" else "Synthetic other").click()
    page.wait_for_function("() => !document.querySelector('#pane .popover') && document.querySelector('#pane .brief-turns')?.open")
    page.locator("#refresh").click()
    page.wait_for_function("() => document.querySelector('#status').textContent !== 'Loading'")
    assert page.locator("#pane .brief-turns").evaluate("e => e.open")
    page.get_by_role("button", name="Speaker turns", exact=True).click()
    cards = page.locator("#pane .group")
    card_count = cards.count()
    assert card_count == 5
    assert cards.locator(".play-toggle").count() == 5
    assert set(identities) == set(cards.locator("[data-identity-key]").evaluate_all("es => es.map(e => e.dataset.identityKey)"))
    page.get_by_role("button", name="Transcript", exact=True).click()
    assert page.locator("#pane .brief-turns").evaluate("e => e.open")
    if page.locator("#back-recordings").is_visible():
        page.locator("#back-recordings").click()
    page.locator("#recording-" + tiny).click()
    page.locator("#pane .brief-turns").wait_for()
    only_open = page.locator("#pane .brief-turns").evaluate("e => e.open")
    assert only_open and page.locator("#pane .brief-turns-pills .pill:visible").count() == 2
    page.screenshot(path=str(output / (size + "-only-brief.png")), full_page=True)
    overflow = page.evaluate("() => document.documentElement.scrollWidth > innerWidth + 2")
    return {"ordinary_pills": ordinary_count, "brief_pills": brief_count, "all_cards": card_count,
            "editor_visible": editor_visible, "only_brief_open": only_open,
            "overflow": overflow or expanded_overflow, "playback_stops": playback_stops}


def main(url, mixed, tiny, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        result = {"errors": []}
        for size, viewport in [("desktop", {"width": 1280, "height": 800}),
                               ("mobile", {"width": 390, "height": 844})]:
            page = browser.new_page(viewport=viewport)
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            result[size] = check(page, url, mixed, tiny, output, size)
            page.close()
        browser.close()
        print(json.dumps(result))


if __name__ == "__main__":
    main(*sys.argv[1:])
