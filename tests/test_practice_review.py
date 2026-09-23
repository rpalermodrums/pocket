"""Real loopback requests to the practice review adapter over generated comparisons."""
import http.client
import json
import threading
from pathlib import Path

import pytest
from test_practice_envelopes import envelope_fixture

from pocket_music.artifact_store import read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_compare, practice_feedback, practice_render
from pocket_music.practice_envelopes import practice_compare_processed, practice_envelope
from pocket_music.practice_review import _make_server

NOTE = "Synthetic fixture text; no listening actually performed"


def comparison(tmp_path, processed=True):
    args, _ = envelope_fixture(tmp_path)
    store, raw = args["store_root"], args["render"]
    if processed:
        joined = practice_envelope(**args)["artifacts"]["render"]
        handle = practice_compare_processed(store, "compare", raw, [joined], "Does the join dip?")
    else:
        alt = practice_render(store, "alt", read_record(raw, store)["context"], ["alternative"])["artifacts"]["render"]
        handle = practice_compare(store, "compare", raw, [alt], "Which passage?", True)
    return store, handle["artifacts"]["comparison"]


def serve(store, handle, session, reports=()):
    server = _make_server(store, handle, str(session), list(reports))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def review(tmp_path):
    store, handle = comparison(tmp_path)
    server, thread = serve(store, handle, tmp_path / "session")
    yield server, store, handle
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def call(server, path, data=None, headers=None, raw=None, method=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
    default = {"Origin": server.origin, "X-Pocket-CSRF": server.review.csrf, "Content-Type": "application/json"}
    default.update(headers or {})
    default = {k: v for k, v in default.items() if v is not None}
    body = json.dumps(data).encode() if data is not None else raw
    connection.request(method or ("POST" if body is not None else "GET"), path, body=body, headers=default)
    response = connection.getresponse()
    payload = response.read()
    result = (json.loads(payload) if payload and "application/json" in response.headers.get("Content-Type", "")
              else payload)
    status, response_headers = response.status, dict(response.headers)
    connection.close()
    return status, result, response_headers


def state(server):
    status, result, _ = call(server, "/api/review")
    assert status == 200, result
    return result


def preview(server, item="variant-1"):
    status, result, _ = call(server, "/api/review/previews",
                             {"expected_revision": state(server)["revision"], "item_id": item})
    assert status == 200, result
    return result["state"]


def report(server, item="variant-1", **changes):
    current = state(server)
    entry = next(i for i in current["items"] if i["item_id"] == item)
    body = {"expected_revision": current["revision"], "item_id": item,
            "preview_sha256": entry["preview"]["sha256"] if entry["preview"] else None,
            "interval_frames": [7990, 8010], "actor": "Synthetic reviewer", "note": NOTE, "decision": None,
            "listened": True, "client_request_id": "0" * 31 + "1", **changes}
    return call(server, "/api/review/reports", body)


def test_summary_exposes_only_selected_members_and_no_paths(review):
    server, store, _ = review
    status, page, headers = call(server, "/")
    assert status == 200 and b"practice review" in page
    assert "default-src 'self'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"
    summary = state(server)
    assert [i["item_id"] for i in summary["items"]] == ["baseline", "variant-1"]
    assert summary["question"] == "Does the join dip?"
    assert summary["alignment"]["synchronized_switching"] is True
    assert summary["items"][1]["processing"]["profile"] == "linear-loop-join-envelope/v1"
    assert all(i["preview"] is None for i in summary["items"]) and summary["report_count"] == 0
    text = json.dumps(summary)
    assert str(Path(store).resolve()) not in text and "/tmp" not in text
    for path in ("/api/review/items/variant-9", "/api/review/items/../../etc", "/api/review/audio/baseline",
                 "/api/review/nothing", "/../README.md", "/review.js/../../cli.py", "/api/review/items/%2e%2e"):
        status, _, _ = call(server, path)
        assert status == 404, path


def test_preview_audio_ranges_and_report_roundtrip(review):
    server, store, _ = review
    summary = preview(server)
    entry = summary["items"][1]
    assert entry["preview"]["profile"] == "browser-pcm16-original-rate/v1"
    assert summary["revision"] == 1
    assert preview(server)["revision"] == 1  # identical replay does not duplicate state
    record = read_record(entry["preview"]["preview"], store)
    payload = read_bytes(record["audio"], store)
    status, body, headers = call(server, "/api/review/audio/variant-1")
    assert status == 200 and body == payload and headers["Content-Type"] == "audio/wav"
    assert headers["Accept-Ranges"] == "bytes" and headers["ETag"] == f'"{record["audio"]["sha256"]}"'
    for requested, expected in (("bytes=0-43", payload[:44]), ("bytes=100-", payload[100:]),
                                ("bytes=-10", payload[-10:]), ("bytes=10-99999999", payload[10:])):
        status, body, headers = call(server, "/api/review/audio/variant-1", headers={"Range": requested})
        assert status == 206 and body == expected, requested
    for requested in ("bytes=0-1,4-5", "bytes=-", "bytes=9-2", f"bytes={len(payload)}-", "items=0-1", "bytes=a-b"):
        status, _, headers = call(server, "/api/review/audio/variant-1", headers={"Range": requested})
        assert status == 416 and headers["Content-Range"] == f"bytes */{len(payload)}", requested
    status, result, _ = report(server, decision="revise")
    assert status == 200, result
    saved = result["report"]
    assert saved["report_schema"] == "pocket.practice-feedback/v2" and saved["item_id"] == "variant-1"
    assert saved["actor_kind"] == "human" and saved["evidence_kind"] == "attributed_human_listening"
    assert saved["reviewed_audio"]["preview_sha256"] == record["audio"]["sha256"]
    assert saved["interval_frames"] == [7990, 8010] and saved["decision"] == "revise"
    again = report(server, decision="revise")
    assert again[0] == 200 and again[1]["report"]["feedback"] == saved["feedback"]
    assert state(server)["report_count"] == 1
    status, page, _ = call(server, "/api/review/reports")
    assert status == 200 and [r["feedback"] for r in page["items"]] == [saved["feedback"]]


@pytest.mark.parametrize("changes,status,match", [
    ({"listened": False}, 400, "Confirm"),
    ({"listened": "yes"}, 400, "Confirm"),
    ({"client_request_id": "short"}, 400, "draft"),
    ({"preview_sha256": "0" * 64}, 409, "current preview"),
    ({"expected_revision": 0}, 409, "refresh"),
    ({"item_id": "variant-7"}, 404, "Unknown"),
    ({"interval_frames": [10, 5]}, 409, "outside"),
    ({"interval_frames": [0, 16001]}, 409, "outside"),
    ({"actor": " "}, 409, "actor"),
    ({"note": ""}, 409, "note"),
    ({"decision": "winner"}, 409, "decision"),
    ({"actor_kind": "agent"}, 400, "Unexpected"),
    ({"render": {}}, 400, "Unexpected"),
])
def test_report_refusals_create_nothing(review, changes, status, match):
    server, _, _ = review
    preview(server)
    code, result, _ = report(server, **changes)
    assert code == status and match.lower() in result["error"].lower(), result
    assert state(server)["report_count"] == 0


def test_report_requires_a_prepared_preview(review):
    server, _, _ = review
    code, result, _ = report(server)
    assert code == 409 and "preview" in result["error"]


def test_changed_draft_under_same_identity_conflicts(review):
    server, _, _ = review
    preview(server)
    assert report(server)[0] == 200
    code, result, _ = report(server, note="Different words, same draft identity")
    assert code == 409 and "idempotency_conflict" in result["error"]
    assert state(server)["report_count"] == 1


@pytest.mark.parametrize("headers,path,body", [
    ({"Host": "localhost:1"}, "/api/review", None),
    ({"Host": "evil.example"}, "/", None),
    ({"Origin": "http://evil.example"}, "/api/review", None),
    ({"Sec-Fetch-Site": "cross-site"}, "/api/review/audio/baseline", None),
    ({"Origin": None}, "/api/review/previews", {"item_id": "baseline"}),
    ({"Origin": "http://127.0.0.1:1"}, "/api/review/previews", {"item_id": "baseline"}),
    ({"X-Pocket-CSRF": "wrong"}, "/api/review/previews", {"item_id": "baseline"}),
    ({"X-Pocket-CSRF": None}, "/api/review/reports", {"item_id": "baseline"}),
])
def test_host_origin_and_csrf_guards(review, headers, path, body):
    server, _, _ = review
    status, _, _ = call(server, path, body, headers)
    assert status == 403
    assert state(server)["revision"] == 0


def test_bounded_json_bodies(review):
    server, _, _ = review
    assert call(server, "/api/review/previews", raw=b"x" * (64 * 1024 + 1))[0] == 413
    assert call(server, "/api/review/previews", raw=b"{}", headers={"Content-Type": "text/plain"})[0] == 415
    assert call(server, "/api/review/previews", raw=b"{nope")[0] == 400
    assert call(server, "/api/review/previews", raw=b"[]")[0] == 400
    assert call(server, "/api/review/unknown", {"x": 1})[0] == 404
    assert call(server, "/api/review", method="HEAD")[0] == 405
    assert call(server, "/api/review", method="PUT", raw=b"{}")[0] == 501


def test_concurrent_preview_requests_are_bounded(review):
    server, _, _ = review
    server.review.preview_slot.acquire()
    try:
        status, result, _ = call(server, "/api/review/previews", {"expected_revision": 0, "item_id": "baseline"})
        assert status == 429 and "Another preview" in result["error"]
    finally:
        server.review.preview_slot.release()


def test_restart_reverifies_retained_session_and_refuses_second_server(tmp_path):
    store, handle = comparison(tmp_path)
    session = tmp_path / "session"
    server, thread = serve(store, handle, session)
    try:
        preview(server)
        assert report(server)[0] == 200
        with pytest.raises(PocketError, match="already served"):
            _make_server(store, handle, str(session))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    server, thread = serve(store, handle, session)
    try:
        summary = state(server)
        assert summary["report_count"] == 1 and summary["items"][1]["preview"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    (tmp_path / "other").mkdir()
    other_store, other = comparison(tmp_path / "other")
    with pytest.raises(PocketError, match="different comparison"):
        _make_server(other_store, other, str(session))


def test_tampered_store_is_refused_on_next_verified_read(tmp_path):
    store, handle = comparison(tmp_path)
    server, thread = serve(store, handle, tmp_path / "session")
    try:
        summary = preview(server)
        record = read_record(summary["items"][1]["preview"]["preview"], store)
        audio = Path(store) / record["audio"]["artifact_uri"]
        audio.chmod(0o644)
        audio.write_bytes(audio.read_bytes()[:-2] + b"\x00\x01")
        for path in ("/api/review", "/api/review/audio/variant-1", "/api/review/items/variant-1"):
            status, result, _ = call(server, path)
            assert status == 409 and "integrity" in result["error"], path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_existing_agent_reports_are_shown_but_must_belong(tmp_path):
    store, handle = comparison(tmp_path, processed=False)
    record = read_record(handle, store)
    agent = practice_feedback(store, "agent", handle, record["variants"][0], [0, 100], "Pocket agent", "agent",
                              "Technical check only", None)["artifacts"]["feedback"]
    server, thread = serve(store, handle, tmp_path / "session", [agent])
    try:
        summary = state(server)
        assert summary["report_count"] == 1 and summary["alignment"]["synchronized_switching"] is False
        status, page, _ = call(server, "/api/review/reports")
        assert status == 200 and page["items"][0]["actor_kind"] == "agent" and page["items"][0]["item_id"] == "variant-1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    (tmp_path / "x").mkdir()
    other_store, other = comparison(tmp_path / "x", processed=False)
    foreign = practice_feedback(other_store, "foreign", other, read_record(other, other_store)["baseline"], [0, 10],
                                "Pocket agent", "agent", "Other comparison", None)["artifacts"]["feedback"]
    with pytest.raises(PocketError):
        _make_server(store, handle, str(tmp_path / "session-2"), [foreign])


def test_revision_alignment_follows_explicit_identical_pairs(tmp_path):
    from test_practice_revisions import comparison_fixture

    from pocket_music.practice_comparisons import practice_compare_revisions
    _, args = comparison_fixture(tmp_path)
    handle = practice_compare_revisions(**args)["artifacts"]["comparison"]
    server, thread = serve(args["store_root"], handle, tmp_path / "session")
    try:
        summary = state(server)
        assert summary["alignment"]["synchronized_switching"] is True
        assert "identical output frames" in summary["alignment"]["basis"]
        assert summary["comparison"]["family"] == "Edited revisions with explicit correspondence"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_preview_refusals_explain_again_on_every_attempt(tmp_path):
    import numpy as np
    import soundfile as sf

    from pocket_music.assets import sha256_file
    from pocket_music.audio_regions import audio_region_capture
    from pocket_music.musical_context import context_create
    from pocket_music.time_maps import musical_time
    store, source = str(tmp_path / "store"), tmp_path / "float-take.wav"
    samples = np.full(16000, 0.2, dtype=np.float32)
    samples[5000] = 1.5  # an unrepresentable FLOAT sample in the first passage
    sf.write(source, samples, 8000, subtype="FLOAT")
    region = audio_region_capture(store_root=store, request_id="capture", source={
        "path": str(source), "expected_sha256": sha256_file(source), "start_frame": 0, "frames": 16000,
        "source_origin": "independently_acquired"})["artifacts"]["region"]
    q = lambda n: {"n": n, "d": 1}
    time_map = musical_time("create", store, request_id="time", definition={
        "source_context": {"schema": "pocket.time-context/v1", "context_id": "c", "attribution": "Synthetic"},
        "domain_qn": {"start": q(0), "end": q(4)}, "tempo": [{"at_qn": q(0), "bpm": q(120), "interpolation": "step"}],
        "host_origin": {"arrangement_qn": q(0), "host_seconds": q(0)}})["artifacts"]["time_map"]
    who = {"actor": "fixture", "actor_kind": "agent", "statement": "Synthetic", "uncertainty": ["None"]}
    context = context_create(store, "context", {
        "context_id": "refusal", "title": "Refusal fixture", "attribution": who,
        "sources": [{"clock_id": "recording", "region": region}],
        "timelines": [{"clock_id": "practice", "time_map": time_map}],
        "occurrences": [{"occurrence_id": name, "source_clock_id": "recording", "timeline_clock_id": "practice",
                         "source_span_frames": [8000 * i, 8000 * (i + 1)], "timeline_span_qn": [q(2 * i), q(2 * i + 2)]}
                        for i, name in enumerate(["a", "b"])], "anchors": [], "materials": []})["artifacts"]["context"]
    first = practice_render(store, "a", context, ["a"])["artifacts"]["render"]
    second = practice_render(store, "b", context, ["b"])["artifacts"]["render"]
    handle = practice_compare(store, "cmp", first, [second], "Synthetic refusal")["artifacts"]["comparison"]
    for session in ("session-1", "session-2"):
        server, thread = serve(store, handle, tmp_path / session)
        try:
            for _ in range(2):
                status, result, _ = call(server, "/api/review/previews",
                                         {"expected_revision": state(server)["revision"], "item_id": "baseline"})
                assert status == 409 and "does not round into PCM16" in result["error"], result
            assert preview(server, "variant-1")["items"][1]["preview"] is not None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_non_ascii_csrf_header_is_refused_not_crashed(review):
    server, _, _ = review
    status, _, _ = call(server, "/api/review/previews", {"item_id": "baseline"}, {"X-Pocket-CSRF": "\xe9"})
    assert status == 403 and state(server)["revision"] == 0


def test_errors_do_not_leak_store_paths_and_huge_ranges_are_416(review):
    server, store, _ = review
    summary = preview(server)
    record = read_record(summary["items"][1]["preview"]["preview"], store)
    status, _, headers = call(server, "/api/review/audio/variant-1", headers={"Range": "bytes=" + "9" * 5000 + "-"})
    assert status == 416 and headers["Content-Range"].startswith("bytes */")
    audio = Path(store) / record["audio"]["artifact_uri"]
    audio.rename(audio.with_suffix(".moved"))
    for path in ("/api/review", "/api/review/audio/variant-1", "/api/review/items/variant-1"):
        status, result, _ = call(server, path)
        assert status == 409, path
        assert str(Path(store).resolve()) not in result["error"] and "/tmp/" not in result["error"], result


def test_aborted_connections_are_not_reported_as_server_errors(review, capsys):
    server, _, _ = review
    try:
        raise ConnectionResetError("peer went away")
    except ConnectionResetError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "ConnectionResetError" not in capsys.readouterr().err
    try:
        raise RuntimeError("unexpected")
    except RuntimeError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "RuntimeError" in capsys.readouterr().err


def test_interrupted_preview_request_is_left_for_inspection_and_bypassed(review):
    server, store, _ = review
    render = server.review.items["variant-1"]["render"]
    stale = Path(store) / "requests" / ("review-preview-" + render["sha256"][:40]) / "lock"
    stale.mkdir(parents=True)  # what a killed process leaves behind
    summary = preview(server)
    assert summary["items"][1]["preview"] is not None
    assert stale.is_dir()  # never stolen or deleted


def test_shutdown_waits_for_in_flight_preview_and_report_work(review):
    import time
    server, _, _ = review
    held = threading.Event()

    def busy():
        with server.review.preview_slot:
            held.set()
            time.sleep(0.4)
    worker = threading.Thread(target=busy)
    worker.start()
    held.wait()
    started = time.monotonic()
    assert server.review.drain(timeout=5) is True
    assert time.monotonic() - started >= 0.3
    worker.join()
