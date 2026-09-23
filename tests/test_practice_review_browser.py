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
    assert page.get_by_text("Not yet reviewed by a person. Playing audio never records a report.").is_visible()
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
    listed.filter(has_text="late snare more").wait_for()  # the list refreshes just after the receipt
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


def hold(page, pattern, method):
    """Intercept matching requests and keep them until the test releases them."""
    held = []
    page.route(pattern, lambda route: held.append(route) if route.request.method == method else route.continue_())
    return held


def wait_for_held(page, held):
    for _ in range(200):
        if held:
            return held[0]
        page.wait_for_timeout(25)
    raise AssertionError("request was not intercepted")


def test_form_is_inert_while_a_save_is_in_flight(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    held = hold(page, "**/api/review/reports", "POST")
    page.get_by_role("button", name="Save report").click()
    route = wait_for_held(page, held)
    for name in ("Cancel draft", "Save report"):
        assert page.get_by_role("button", name=name).is_disabled()
    assert page.get_by_label("What did you hear?").is_disabled()
    assert page.get_by_role("radio", name="Baseline").is_disabled()
    route.continue_()
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    assert report_count(server) == 1
    assert "No report was saved" not in page.locator("#status").inner_text()


def test_lost_save_response_is_explained_and_retry_returns_the_same_report(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    dropped = []

    def drop_response(route):
        if route.request.method != "POST" or dropped:
            return route.continue_()
        route.fetch()  # the save reaches the server...
        dropped.append(True)
        route.abort()  # ...but its response never reaches the page
    page.route("**/api/review/reports", drop_response)
    page.get_by_role("button", name="Save report").click()
    page.locator("#error").get_by_text("may already exist").wait_for()
    assert report_count(server) == 1
    assert page.get_by_label("What did you hear?").input_value() == "The join dips slightly"
    page.get_by_role("button", name="Save report").click()
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    assert report_count(server) == 1 and page.locator("#reports li").count() == 1


def test_late_provenance_answer_is_not_shown_under_another_item(review):
    server, page, _, _ = review
    summary = server.review.summary()
    short = {entry["item_id"]: entry["short_id"] for entry in summary["items"]}
    page.get_by_role("radio", name="Variant 1").click()
    held = hold(page, "**/api/review/items/variant-1", "GET")
    page.locator("#provenance summary").click()
    route = wait_for_held(page, held)
    page.get_by_role("radio", name="Baseline").click()
    page.locator("#provenance summary").click()
    page.locator("#provenance-list").get_by_text(short["baseline"], exact=False).wait_for()
    route.continue_()
    page.wait_for_timeout(300)
    listed = page.locator("#provenance-list").inner_text()
    assert short["baseline"] in listed and short["variant-1"] not in listed


def test_interval_playback_stops_at_its_end_and_never_cuts_ordinary_play(review):
    _, page, _, _ = review
    prepare(page)
    page.get_by_label("Start (seconds)").fill("0.9")
    page.get_by_label("End (seconds)").fill("1.1")
    page.get_by_role("button", name="Play this interval").click()
    wait_until(page, "document.querySelector('audio').currentTime > 0.95 && document.querySelector('audio').paused")
    stopped = page.evaluate("document.querySelector('audio').currentTime")
    assert 1.1 <= stopped <= 1.16, stopped  # within a few display frames, not ~250 ms late
    page.get_by_role("button", name="Play this interval").click()
    wait_until(page, "!document.querySelector('audio').paused")
    page.evaluate("document.querySelector('audio').pause()")
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press(" ")  # ordinary playback after a manual pause
    wait_until(page, "document.querySelector('audio').currentTime > 1.3")


def test_late_preview_completion_does_not_take_focus(review):
    _, page, _, _ = review
    prepare(page, "Baseline")
    page.get_by_role("radio", name="Variant 1").click()
    held = hold(page, "**/api/review/previews", "POST")
    page.get_by_role("button", name="Prepare browser preview").click()
    route = wait_for_held(page, held)
    page.get_by_role("radio", name="Baseline").click()
    page.get_by_label("What did you hear?").click()
    page.keyboard.type("snare is")
    route.continue_()
    page.get_by_text("Preview for Variant 1 ready.").wait_for()
    page.keyboard.type(" late")
    assert page.evaluate("document.activeElement.id") == "note"
    assert page.get_by_label("What did you hear?").input_value() == "snare is late"
    assert page.evaluate("document.querySelector('audio').paused") is True


def test_selection_keeps_focus_and_saved_drafts_clear_the_guard(review):
    _, page, _, _ = review
    page.get_by_role("radio", name="Variant 1").click()
    assert page.evaluate("document.activeElement.dataset.item") == "variant-1"
    page.get_by_role("radio", name="Baseline").focus()
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.dataset.item") == "baseline"
    prepare(page)
    fill_report(page)
    page.get_by_role("radio", name="Baseline").click()
    assert page.locator("#switch-guard").is_visible()
    page.get_by_role("button", name="Save report").click()
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    assert page.locator("#switch-guard").is_hidden()


def test_agent_reports_are_counted_and_labelled_separately(tmp_path, browser):
    from pocket_music.artifact_store import read_record
    from pocket_music.practice_audio import practice_feedback
    store, handle = comparison(tmp_path)
    variant = read_record(handle, store)["variants"][0]
    agent = practice_feedback(store, "agent", handle, variant, [0, 100], "Pocket agent", "agent",
                              "Technical check only", None)["artifacts"]["feedback"]
    server = _make_server(store, handle, str(tmp_path / "session"), [agent])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context()
    page = context.new_page()
    try:
        page.goto(server.origin + "/")
        page.locator("#reports li").first.wait_for()
        assert page.locator("#listening-state").inner_text().startswith(
            "Not yet reviewed by a person. 1 agent report (technical, not listening).")
        row = page.locator("#reports li").first.inner_text()
        assert "Agent report" in row and "Audio referenced:" in row and "Heard:" not in row
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_failed_initial_verification_leaves_nothing_actionable(tmp_path, browser):
    store, handle = comparison(tmp_path)
    server = _make_server(store, handle, str(tmp_path / "session"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context()
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.route("**/api/review", lambda route: route.fulfill(
            status=409, content_type="application/json", body='{"error": "Artifact integrity mismatch"}'))
        page.goto(server.origin + "/")
        page.locator("#error").get_by_text("Artifact integrity mismatch").wait_for()
        assert page.get_by_role("button", name="Save report").is_disabled()
        assert page.get_by_label("What did you hear?").is_disabled()
        assert page.locator("#make-preview").is_disabled()
        assert not errors
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_lost_save_explanation_survives_an_unreachable_server(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)

    def gone(route):
        if route.request.method == "POST":
            route.fetch()  # the save completes on the server
        route.abort()  # but nothing reaches the page any more
    page.route("**/api/review**", gone)
    page.get_by_role("button", name="Save report").click()
    page.locator("#error").get_by_text("may already exist").wait_for()
    page.wait_for_timeout(300)
    assert "may already exist" in page.locator("#error").inner_text()
    assert report_count(server) == 1


def test_failed_save_unlocks_the_form_before_the_refresh_returns(review):
    from test_practice_review import preview as prepare_over_http
    server, page, _, _ = review
    prepare(page)
    prepare_over_http(server, "baseline")  # the page's revision is now stale
    fill_report(page)
    held = hold(page, "**/api/review", "GET")
    page.get_by_role("button", name="Save report").click()
    page.locator("#error").get_by_text("Report not saved").wait_for()
    wait_for_held(page, held)
    assert not page.get_by_label("What did you hear?").is_disabled()
    assert "Saving and verifying" not in page.locator("#save-problem").inner_text()
    held[0].continue_()


def test_manual_seek_or_switch_cancels_interval_playback(review):
    _, page, _, _ = review
    prepare(page, "Baseline")
    prepare(page)
    page.get_by_label("Start (seconds)").fill("0.1")
    page.get_by_label("End (seconds)").fill("0.4")
    page.get_by_role("button", name="Play this interval").click()
    wait_until(page, "!document.querySelector('audio').paused")
    page.evaluate("document.querySelector('audio').currentTime = 0.05")  # a manual seek
    wait_until(page, "document.querySelector('audio').currentTime > 0.6")
    page.evaluate("document.querySelector('audio').pause()")
    page.get_by_role("button", name="Play this interval").click()
    wait_until(page, "!document.querySelector('audio').paused")
    page.get_by_role("radio", name="Baseline").click()  # synchronized switch pauses and cues
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press(" ")
    wait_until(page, "document.querySelector('audio').currentTime > 0.6")


def fail_first_save(page):
    """Answer the first save with a refusal; later saves reach the server (or earlier routes)."""
    failed = []

    def route(route):
        if route.request.method != "POST" or failed:
            return route.fallback()
        failed.append(True)
        route.fulfill(status=409, json={"error": "This review changed; refresh before continuing"})
    page.route("**/api/review/reports", route)


def answer_late(page, pattern):
    """Hold the next GET's answer as the server gave it then, until the test releases it."""
    held = []

    def route(route):
        if route.request.method != "GET" or held:
            return route.fallback()
        held.append((route, route.fetch()))
    page.route(pattern, route)
    return held


def test_a_failed_saves_refresh_never_unlocks_a_retry_in_flight(review):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    refresh = hold(page, "**/api/review", "GET")
    retry = hold(page, "**/api/review/reports", "POST")
    fail_first_save(page)
    save = page.get_by_role("button", name="Save report")
    save.click()
    page.locator("#error").get_by_text("Report not saved").wait_for()
    first = wait_for_held(page, refresh)
    save.click()  # the retry the explanation invites
    second = wait_for_held(page, retry)
    listed = lambda response: response.url.endswith("/api/review/reports") and response.request.method == "GET"
    with page.expect_response(listed):
        first.continue_()  # the failed save's refresh finishes while the retry is in flight
    page.wait_for_timeout(300)
    for name in ("Cancel draft", "Save report"):
        assert page.get_by_role("button", name=name).is_disabled()
    assert page.get_by_role("radio", name="Baseline").is_disabled()
    assert page.get_by_label("What did you hear?").is_disabled()
    assert "Report not saved" not in page.locator("#error").inner_text()
    second.continue_()
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    assert report_count(server) == 1
    assert "No report was saved" not in page.locator("#status").inner_text()


@pytest.mark.parametrize("late", ["**/api/review", "**/api/review/reports"], ids=["state", "reports"])
def test_a_late_answer_to_a_failed_save_never_replaces_the_retrys_result(review, late):
    server, page, _, _ = review
    prepare(page)
    fill_report(page)
    held = answer_late(page, late)
    fail_first_save(page)
    save = page.get_by_role("button", name="Save report")
    save.click()
    page.locator("#error").get_by_text("Report not saved").wait_for()
    route, response = wait_for_held(page, held)
    save.click()
    page.locator("#receipt").get_by_text("Report saved").wait_for()
    saved = page.locator("#reports li").filter(has_text="The join dips slightly")
    saved.wait_for()
    route.fulfill(response=response)  # older state or an older report list answers last
    page.wait_for_timeout(400)
    assert "Report not saved" not in page.locator("#error").inner_text()
    assert saved.count() == 1
    assert page.locator("#listening-state").inner_text().startswith("1 human listening report")
    assert report_count(server) == 1


def test_show_more_reports_loads_each_page_once(review):
    from test_practice_review import preview as prepare_over_http
    from test_practice_review import report as report_over_http
    server, page, _, _ = review
    prepare_over_http(server)
    for n in range(17):  # one more than a page
        status, result, _ = report_over_http(server, interval_frames=[7000 + n, 7050 + n],
                                             client_request_id=f"{n + 1:032x}")
        assert status == 200, result
    page.reload()
    more = page.get_by_role("button", name="Show more reports")
    more.wait_for()
    assert page.locator("#reports li").count() == 16
    held = hold(page, lambda url: "/api/review/reports?cursor=" in url, "GET")
    more.scroll_into_view_if_needed()
    box = more.bounding_box()
    page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    wait_for_held(page, held)
    page.wait_for_timeout(300)
    requested = len(held)
    for route in held:
        route.continue_()
    wait_until(page, "document.querySelectorAll('#reports li').length >= 17")
    page.wait_for_timeout(300)
    assert page.locator("#reports li").count() == 17
    assert requested == 1
    assert more.is_hidden()
