"""Optional browser checks for the practice review screen in headless Chromium.

Skipped unless Playwright and a Chromium build are available. These drive the real
loopback server and shared providers; they do not establish musical approval.
"""
import importlib.util
import threading

import pytest
from test_practice_review import comparison

from pocket_music.practice_review import _make_server

pytestmark = pytest.mark.skipif(importlib.util.find_spec("playwright") is None,
                                reason="Optional browser checks require Playwright")
HOSTILE = '<img src=x onerror="window.__pwned=1"> late snare'


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        except Error as error:
            pytest.skip(f"Chromium unavailable: {error}")
        yield instance
        instance.close()


@pytest.fixture
def review(tmp_path, browser):
    store, handle = comparison(tmp_path)
    server = _make_server(store, handle, str(tmp_path / "session"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context(viewport={"width": 1024, "height": 900})
    page = context.new_page()
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(server.origin + "/")
    page.get_by_role("heading", level=1, name="Does the join dip?").wait_for()
    yield server, page, requests, errors
    context.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def prepare(page, label="Variant 1"):
    page.get_by_role("radio", name=label).click()
    page.get_by_role("button", name="Prepare browser preview").click()
    page.get_by_text("Preview ready").wait_for()


def fill_report(page, note="The join dips slightly", start="0.9", end="1.1"):
    page.get_by_label("Start (seconds)").fill(start)
    page.get_by_label("End (seconds)").fill(end)
    page.get_by_label("Your name").fill("Synthetic reviewer")
    page.get_by_label("What did you hear?").fill(note)
    page.get_by_label("I listened to this interval of this preview").check()


def wait_until(page, expression, timeout=10):
    """Poll from Python: the page's CSP (correctly) refuses the eval Playwright's waiter needs."""
    import time
    deadline = time.monotonic() + timeout
    while not page.evaluate(expression):
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {expression}")
        time.sleep(0.05)


def report_count(server):
    return len(server.review.read()["reports"])


def test_initial_state_is_labelled_local_and_silent(review):
    server, page, requests, errors = review
    assert page.get_by_text("Not yet reviewed. Playing audio never records a report.").is_visible()
    radios = page.get_by_role("radio")
    assert radios.count() == 2 and radios.first.get_attribute("aria-checked") == "true"
    assert page.get_by_role("button", name="Prepare browser preview").is_visible()
    assert page.locator("audio").get_attribute("src") is None
    assert page.get_by_role("button", name="Save report").is_disabled()
    assert all(url.startswith(server.origin) for url in requests)
    assert not errors


def test_keyboard_selection_preview_and_explicit_report(review):
    server, page, _, errors = review
    page.get_by_role("radio", name="Baseline").focus()
    page.keyboard.press("ArrowRight")
    variant = page.get_by_role("radio", name="Variant 1")
    assert variant.get_attribute("aria-checked") == "true"
    assert page.evaluate("document.activeElement.dataset.item") == "variant-1"
    page.get_by_role("button", name="Prepare browser preview").click()
    page.get_by_text("Preview ready").wait_for()
    audio = page.locator("audio")
    assert audio.get_attribute("src").startswith("/api/review/audio/variant-1")
    assert page.evaluate("document.querySelector('audio').paused") is True
    assert "every sample identical" in page.locator("#preview-label").inner_text() or \
        "rounded" in page.locator("#preview-label").inner_text()
    fill_report(page, note=HOSTILE)
    # Typing a space in the note must not toggle playback.
    page.get_by_label("What did you hear?").press("End")
    page.keyboard.type(" more")
    assert page.evaluate("document.querySelector('audio').paused") is True
    assert "Frames 7200–8800 of 16000" in page.locator("#interval").inner_text()
    assert page.get_by_role("button", name="Save report").is_enabled()
    page.get_by_role("button", name="Save report").click()
    receipt = page.locator("#receipt")
    receipt.get_by_text("Report saved").wait_for()
    assert "Human listening report by Synthetic reviewer (person) about Variant 1, frames 7200–8800" \
        in receipt.inner_text()
    wait_until(page, "document.activeElement.id === 'receipt'")
    listed = page.locator("#reports li")
    assert listed.count() == 1 and HOSTILE + " more" in listed.first.inner_text()
    assert page.locator("#reports img").count() == 0 and page.evaluate("window.__pwned") is None
    assert report_count(server) == 1
    page.reload()
    page.locator("#reports li").first.wait_for()
    assert page.locator("#reports li").count() == 1
    assert not errors


def test_playback_alone_never_creates_a_report(review):
    server, page, _, _ = review
    prepare(page)
    page.evaluate("document.querySelector('audio').play()")
    wait_until(page, "document.querySelector('audio').currentTime > 0.3")
    page.evaluate("document.querySelector('audio').pause()")
    page.keyboard.press("[")
    assert report_count(server) == 0 and page.locator("#reports li").inner_text() == "No reports saved yet."


def test_drafts_do_not_retarget_and_cancel_saves_nothing(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    page.get_by_role("radio", name="Baseline").click()
    guard = page.locator("#switch-guard")
    assert guard.is_visible() and "unsaved report for Variant 1" in guard.inner_text()
    assert page.get_by_role("radio", name="Variant 1").get_attribute("aria-checked") == "true"
    page.get_by_role("button", name="Keep editing it").click()
    assert page.evaluate("document.activeElement.id") == "note"
    page.get_by_role("radio", name="Baseline").click()
    page.get_by_role("button", name="Discard that draft").click()
    assert page.get_by_role("radio", name="Baseline").get_attribute("aria-checked") == "true"
    assert page.get_by_label("What did you hear?").input_value() == ""
    page.get_by_role("radio", name="Variant 1").click()
    fill_report(page)
    page.get_by_role("button", name="Cancel draft").click()
    page.get_by_text("Draft discarded. No report was saved.").wait_for()
    assert report_count(server) == 0


def test_repeated_save_and_stale_revision(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    page.evaluate("""() => { const button = document.querySelector('#save');
        button.click(); button.click(); document.querySelector('#report').requestSubmit(); }""")
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    assert report_count(server) == 1
    # Another client changes the session; the stale page must refuse, focus and explain.
    from test_practice_review import preview
    preview(server, "baseline")
    fill_report(page, note="Second listen")
    page.get_by_role("button", name="Save report").click()
    error = page.locator("#error")
    error.wait_for()
    wait_until(page, "document.activeElement.id === 'error'")
    assert "refresh" in error.inner_text().lower()
    assert report_count(server) == 1


def test_narrow_layout_has_no_horizontal_scroll(review):
    _, page, _, _ = review
    page.set_viewport_size({"width": 400, "height": 900})
    prepare(page)
    assert page.evaluate("document.documentElement.scrollWidth") <= 400
    for name in ("Start (seconds)", "End (seconds)", "Your name", "What did you hear?"):
        assert page.get_by_label(name).is_visible()
