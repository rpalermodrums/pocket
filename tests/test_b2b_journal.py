# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b journal, its offline readers and the b2b clocks. No host, device or sleep is used."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime

import pytest
from b2b_harness import HOST_EPOCH, MS

from pocket_music.artifact_store import canonical_bytes, digest
from pocket_music.b2b_clock import SystemClock, TestClock, platform_clock_names, suspend_gap_ns
from pocket_music.b2b_journal import (
    ENTRY_SCHEMA,
    MAX_LINE_BYTES,
    RESERVE_BYTES,
    Journal,
    JournalUnavailable,
    read_entries,
    read_line,
    read_request,
)
from pocket_music.errors import PocketError, valid_hint

SESSION = HOST_EPOCH + ".1"


def open_journal(tmp_path, **kwargs):
    clock = TestClock()
    path = tmp_path / "journal.ndjson"
    return Journal(path, clock=clock, host_epoch=HOST_EPOCH, **kwargs), path, clock


def started(j, request_id, channel="agent", operation="act"):
    return j.append("request_started", {"request_id": request_id, "channel": channel, "operation": operation,
                                        "input_sha256": "a" * 64}, session_epoch=SESSION)


def completed(j, request_id, receipt, channel="agent"):
    return j.append("request_completed", {"request_id": request_id, "channel": channel,
                                          "receipt_sha256": digest(receipt), "receipt": receipt},
                    session_epoch=SESSION)


def admitted(j, request_id, index=0):
    return j.append("step_admitted", {"request_id": request_id, "step": index,
                                      "resource": "synthetic/right/gain", "value": 40, "generation": 3,
                                      "grant_id": "g-1"}, session_epoch=SESSION)


def lines(path):
    return path.read_bytes().split(b"\n")[:-1]


def rewrite(path, new_lines, tail=b""):
    path.write_bytes(b"".join(line + b"\n" for line in new_lines) + tail)


def test_entries_are_canonical_hash_chained_lines_with_clock_context(tmp_path):
    j, path, clock = open_journal(tmp_path)
    first = j.append("host_started", {"profile": "synthetic-two-deck/v1"}, session_epoch=SESSION)
    clock.advance(5 * MS)
    second = j.append("session_registered", {"session_id": "s"}, session_epoch=SESSION, monotonic_ns=42)
    j.sync()
    raw = lines(path)
    entries = [json.loads(line) for line in raw]
    assert [canonical_bytes(e) for e in entries] == raw
    assert entries[0] == {"schema": ENTRY_SCHEMA, "seq": 1, "prev_sha256": None, "host_epoch": HOST_EPOCH,
                          "session_epoch": SESSION, "monotonic_ns": 1_000_000_000_000,
                          "wall_time_utc": clock.wall_utc(), "kind": "host_started",
                          "data": {"profile": "synthetic-two-deck/v1"}}
    assert entries[1]["prev_sha256"] == hashlib.sha256(raw[0]).hexdigest()
    assert entries[1]["monotonic_ns"] == 42
    assert (first, second) == ((1, 0), (2, len(raw[0]) + 1))
    assert j.status() == {"state": "ok", "bytes": path.stat().st_size, "max_bytes": j.max_bytes,
                          "lost_entries": 0, "last_seq": 2}
    with pytest.raises(ValueError):
        j.append("grant_maybe", {}, session_epoch=SESSION)
    with pytest.raises(TypeError):
        j.append("panic", ["not", "an", "object"], session_epoch=SESSION)
    assert j.seq == 2


def test_journal_refuses_an_existing_file_and_is_private(tmp_path):
    j, path, clock = open_journal(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        Journal(path, clock=clock, host_epoch=HOST_EPOCH)
    with pytest.raises(ValueError):
        Journal(tmp_path / "small.ndjson", clock=clock, host_epoch=HOST_EPOCH, max_bytes=1024)
    j.close()
    with pytest.raises(JournalUnavailable):
        j.append("panic", {}, session_epoch=SESSION)


def test_crash_after_admission_reads_back_as_outcome_unknown(tmp_path):
    j, path, _ = open_journal(tmp_path)
    started(j, "r-act")
    admitted(j, "r-act")
    j.append("step_submitted", {"request_id": "r-act", "submission_id": "s-1-1"}, session_epoch=SESSION)
    j.sync()
    assert read_request(path, "r-act")["state"] == "outcome_unknown"
    # The host dies while writing the completion: a torn last line, never a completed request.
    with path.open("ab") as stream:
        stream.write(b'{"data":{"channel":"agent","receipt":{"status":"ok"')
    status = read_request(path, "r-act")
    assert status["state"] == "outcome_unknown" and status["receipt"] is None
    assert status["steps_admitted"] == 1 and status["operation"] == "act"
    assert status["journal"]["chain"] == "truncated_at_seq" and status["journal"]["last_seq"] == 3


def test_stop_before_any_admission_reads_back_as_stopped_before_dispatch(tmp_path):
    j, path, _ = open_journal(tmp_path)
    started(j, "r-claim", operation="claim")
    started(j, "r-act")
    j.append("step_cancelled", {"request_id": "r-act", "reason": "notice_delivery_unknown"},
             session_epoch=SESSION)
    j.sync()
    assert read_request(path, "r-claim")["state"] == "stopped_before_dispatch"
    assert read_request(path, "r-act")["state"] == "stopped_before_dispatch"
    missing = read_request(path, "r-never")
    assert missing["state"] == "not_found" and missing["operation"] is None


def test_completed_request_returns_its_verified_receipt_and_an_altered_one_is_unavailable(tmp_path):
    j, path, _ = open_journal(tmp_path)
    receipt = {"schema": "pocket.operation-receipt/v1", "request_id": "r-1", "status": "ok",
               "outcome": {"kind": "verified", "evidence_refs": ["journal:3"]}}
    started(j, "r-1")
    admitted(j, "r-1")
    completed(j, "r-1", receipt)
    j.sync()
    status = read_request(path, "r-1")
    assert status["state"] == "complete" and status["receipt"] == receipt
    assert not status["receipt_unavailable"]
    assert status["journal"] == {"chain": "intact", "last_seq": 3, "bytes_read": path.stat().st_size,
                                 "read_limit_reached": False}
    # The last line has no successor to vouch for it, so the receipt digest is what catches an edit.
    *head, last = lines(path)
    rewrite(path, [*head, last.replace(b'"status":"ok"', b'"status":"no"')])
    altered = read_request(path, "r-1")
    assert altered["state"] == "complete" and altered["receipt"] is None and altered["receipt_unavailable"]


def test_page_channel_requests_never_answer_for_an_agent_request_id(tmp_path):
    j, path, _ = open_journal(tmp_path)
    started(j, "page-7", channel="page", operation="panic")
    completed(j, "page-7", {"status": "ok"}, channel="page")
    started(j, "r-2")
    completed(j, "r-2", {"status": "ok"}, channel="page")
    j.sync()
    assert read_request(path, "page-7")["state"] == "not_found"
    assert read_request(path, "page-7", channel="page")["state"] == "complete"
    assert read_request(path, "r-2")["state"] == "stopped_before_dispatch"


def _five(tmp_path):
    j, path, _ = open_journal(tmp_path)
    for index in range(5):
        j.append("human_input", {"resources": ["synthetic/right/gain"], "count": index},
                 session_epoch=SESSION)
    j.sync()
    return path, lines(path)


def test_journal_chain_reports_truncation_and_tampering(tmp_path):
    path, raw = _five(tmp_path)
    entries, chain = read_entries(path)
    assert (chain["chain"], chain["last_seq"]) == ("intact", 5)
    assert [e["seq"] for _, e in entries] == [1, 2, 3, 4, 5]
    assert [offset for offset, _ in entries] == [sum(len(x) + 1 for x in raw[:i]) for i in range(5)]

    rewrite(path, raw, tail=raw[0][:20])  # torn tail
    entries, chain = read_entries(path)
    assert (chain["chain"], chain["last_seq"], len(entries)) == ("truncated_at_seq", 5, 5)

    # Still valid JSON, but no longer what line 4 hashed.
    tampered = raw[2].replace(b'"count":2', b'"count":9')
    rewrite(path, [*raw[:2], tampered, *raw[3:]])
    entries, chain = read_entries(path)
    assert (chain["chain"], chain["last_seq"]) == ("chain_broken", 2)
    assert [e["seq"] for _, e in entries] == [1, 2]

    rewrite(path, [*raw[:2], *raw[3:]])  # a deleted line: neither neighbour can be vouched for
    entries, chain = read_entries(path)
    assert (chain["chain"], chain["last_seq"]) == ("chain_broken", 1)

    rewrite(path, [*raw[:2], b"not json", *raw[3:]])
    assert read_entries(path)[1]["chain"] == "chain_broken" and read_entries(path)[1]["last_seq"] == 2

    rewrite(path, [*raw[:2], b"x" * (MAX_LINE_BYTES + 10), *raw[3:]])
    assert read_entries(path)[1]["chain"] == "chain_broken" and read_entries(path)[1]["last_seq"] == 2

    forged_first = json.loads(raw[0])
    forged_first["prev_sha256"] = "0" * 64
    rewrite(path, [canonical_bytes(forged_first), *raw[1:]])
    assert read_entries(path)[0] == [] and read_entries(path)[1]["chain"] == "chain_broken"


def test_read_bound_is_reported_rather_than_read_as_an_intact_chain(tmp_path):
    path, raw = _five(tmp_path)
    entries, chain = read_entries(path, max_bytes=len(raw[0]) + len(raw[1]) + 2 + 5)
    assert (chain["chain"], chain["last_seq"], chain["read_limit_reached"]) == ("truncated_at_seq", 2, True)
    assert len(entries) == 2


def test_read_line_reads_one_entry_at_its_offset(tmp_path):
    j, path, _ = open_journal(tmp_path)
    started(j, "r-1")
    ref = completed(j, "r-1", {"status": "ok"})
    j.sync()
    entry = read_line(path, ref.offset)
    assert entry["seq"] == ref.seq == 2 and entry["kind"] == "request_completed"
    assert digest(entry["data"]["receipt"]) == entry["data"]["receipt_sha256"]
    assert read_line(path, ref.offset + 3) is None
    assert read_line(path, -1) is None and read_line(path, path.stat().st_size) is None
    assert read_line(tmp_path / "missing.ndjson", 0) is None


def test_oversized_entry_keeps_its_identity_and_a_digest_of_what_was_omitted(tmp_path):
    j, path, _ = open_journal(tmp_path)
    data = {"request_id": "r-big", "channel": "agent", "receipt": {"blob": "x" * (MAX_LINE_BYTES + 1)}}
    j.append("request_completed", data, session_epoch=SESSION)
    j.sync()
    (line,) = lines(path)
    assert len(line) < MAX_LINE_BYTES
    kept = json.loads(line)["data"]
    assert kept["request_id"] == "r-big" and kept["data_sha256"] == digest(data) and kept["omitted_bytes"] > 0
    assert read_request(path, "r-big")["state"] == "not_found"  # no request_started was written


def test_journal_budget_writes_one_overflow_entry_then_refuses_every_append(tmp_path):
    j, path, _ = open_journal(tmp_path, max_bytes=1024 * 1024)
    payload = {"blob": "y" * 10_000}
    with pytest.raises(JournalUnavailable) as full:
        for _ in range(200):
            j.append("human_input", payload, session_epoch=SESSION)
    assert full.value.state == "full" and j.state == "full" and j.lost_entries == 1
    j.sync()
    size = path.stat().st_size
    assert j.max_bytes - RESERVE_BYTES - 10_500 < size <= j.max_bytes
    entries, chain = read_entries(path)
    assert chain["chain"] == "intact"
    overflow = entries[-1][1]
    assert overflow["kind"] == "journal_overflow" and overflow["data"]["refused_kind"] == "human_input"
    with pytest.raises(JournalUnavailable):
        j.append("panic", {}, session_epoch=SESSION)
    assert j.lost_entries == 2 and path.stat().st_size == size


def test_writer_error_marks_the_journal_failed_and_counts_lost_entries(tmp_path):
    calls = []

    def no_space(fd, data):
        calls.append(len(data))
        raise OSError(errno.ENOSPC, "No space left on device")

    fsyncs = []
    j, path, _ = open_journal(tmp_path, writer=no_space, fsync=fsyncs.append)
    with pytest.raises(JournalUnavailable) as failed:
        started(j, "r-1")
    assert failed.value.state == "failed" and j.state == "failed" and j.lost_entries == 1
    with pytest.raises(JournalUnavailable):
        admitted(j, "r-1")
    assert len(calls) == 1 and j.lost_entries == 2 and path.read_bytes() == b""
    assert j.sync() == "failed" and fsyncs == []


def test_a_write_that_fails_midway_leaves_a_torn_tail_and_a_failed_journal(tmp_path):
    state = {"calls": 0}

    def half_then_fail(fd, data):
        state["calls"] += 1
        if state["calls"] <= 2:
            return os.write(fd, data)
        if state["calls"] == 3:
            return os.write(fd, data[: len(data) // 2])
        raise OSError(errno.EIO, "I/O error")

    j, path, _ = open_journal(tmp_path, writer=half_then_fail)
    started(j, "r-1")
    admitted(j, "r-1")
    with pytest.raises(JournalUnavailable):
        completed(j, "r-1", {"status": "ok"})
    assert j.state == "failed" and j.seq == 2
    status = read_request(path, "r-1")
    assert status["state"] == "outcome_unknown" and status["journal"]["chain"] == "truncated_at_seq"


def test_short_writes_are_completed_and_a_zero_byte_write_fails(tmp_path):
    calls = []

    def seven_bytes(fd, data):
        calls.append(len(data))
        return os.write(fd, data[:7])

    j, path, _ = open_journal(tmp_path, writer=seven_bytes)
    started(j, "r-1")
    assert len(calls) > 1 and read_entries(path)[1]["chain"] == "intact"
    (tmp_path / "stuck").mkdir()
    stuck, stuck_path, _ = open_journal(tmp_path / "stuck", writer=lambda fd, data: 0)
    with pytest.raises(JournalUnavailable):
        started(stuck, "r-1")
    assert stuck.state == "failed" and stuck_path.read_bytes() == b""


def test_append_is_one_write_and_fsync_runs_only_in_sync_without_blocking_appends(tmp_path):
    writes, fsyncs = [], []
    fsync_started, release = threading.Event(), threading.Event()

    def spy_write(fd, data):
        writes.append(len(data))
        return os.write(fd, data)

    def slow_fsync(fd):
        fsyncs.append(fd)
        fsync_started.set()
        assert release.wait(timeout=5)

    j, _, _ = open_journal(tmp_path, writer=spy_write, fsync=slow_fsync)
    started(j, "r-1")
    admitted(j, "r-1")
    assert len(writes) == 2 and fsyncs == []
    syncer = threading.Thread(target=j.sync, daemon=True)
    syncer.start()
    assert fsync_started.wait(timeout=5)
    # An fsync in progress never holds up an append, which runs under the broker lock.
    j.append("step_submitted", {"request_id": "r-1", "submission_id": "s-1-1"}, session_epoch=SESSION)
    assert len(writes) == 3
    release.set()
    syncer.join(timeout=5)
    assert not syncer.is_alive() and len(fsyncs) == 1


def test_fsync_failure_marks_the_journal_failed_for_the_next_append(tmp_path):
    def broken_fsync(fd):
        raise OSError(errno.EIO, "I/O error")

    j, path, _ = open_journal(tmp_path, fsync=broken_fsync)
    started(j, "r-1")
    assert j.sync() == "failed" and j.state == "failed"
    with pytest.raises(JournalUnavailable):
        admitted(j, "r-1")
    assert read_request(path, "r-1")["state"] == "stopped_before_dispatch"


def test_journal_never_writes_a_forbidden_secret(tmp_path):
    j, path, _ = open_journal(tmp_path)
    token = "k" * 43
    j.forbid(token)
    with pytest.raises(ValueError):
        j.forbid("short")
    j.append("host_started", {"page_link": "printed_to_terminal"}, session_epoch=SESSION)
    assert j.seq == 1
    with pytest.raises(JournalUnavailable):
        j.append("readback", {"page": f"http://127.0.0.1:1/#k={token}"}, session_epoch=SESSION)
    # Nothing was written, and the journal failed closed: no later entry is written either.
    assert j.seq == 1 and token.encode() not in path.read_bytes() and j.state == "failed"
    with pytest.raises(JournalUnavailable):
        j.append("readback", {"value": 1}, session_epoch=SESSION)


def _scripted_clocks(monkeypatch, names, deadline, uptime):
    ids = {names[0]: 9001, names[1]: 9002}
    for name, clock_id in ids.items():
        monkeypatch.setattr(time, name, clock_id, raising=False)
    readings = {9001: iter(deadline), 9002: iter(uptime)}
    monkeypatch.setattr(time, "clock_gettime_ns", lambda clock_id: next(readings[clock_id]))


@pytest.mark.parametrize("platform, names", [("darwin", ("CLOCK_MONOTONIC", "CLOCK_UPTIME_RAW")),
                                             ("linux", ("CLOCK_BOOTTIME", "CLOCK_MONOTONIC"))])
def test_clock_pair_never_runs_backwards_and_suspend_advances_only_deadline_time(
        monkeypatch, platform, names):
    assert platform_clock_names(platform) == names
    _scripted_clocks(monkeypatch, names, deadline=[100, 90, 160], uptime=[50, 40, 60])
    clock = SystemClock(platform)
    assert clock.names == {"deadline": names[0], "uptime": names[1]}
    readings = [(clock.now_ns(), clock.uptime_ns()) for _ in range(3)]
    assert readings == [(100, 50), (100, 50), (160, 60)]
    # 60 ns of deadline time passed against 10 ns of uptime: 50 ns were spent suspended.
    assert suspend_gap_ns(readings[1], readings[2]) == 50

    test_clock = TestClock()
    before = (test_clock.now_ns(), test_clock.uptime_ns())
    test_clock.suspend(60_000 * MS)
    after_sleep = (test_clock.now_ns(), test_clock.uptime_ns())
    assert after_sleep == (before[0] + 60_000 * MS, before[1])
    assert suspend_gap_ns(before, after_sleep) == 60_000 * MS
    test_clock.advance_ms(10)
    assert suspend_gap_ns(after_sleep, (test_clock.now_ns(), test_clock.uptime_ns())) == 0
    test_clock.set_raw(before[0])
    assert test_clock.now_ns() == after_sleep[0] + 10 * MS
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError):
            test_clock.advance(bad)


def test_unsupported_platforms_and_missing_clocks_are_refused(monkeypatch):
    for platform in ("win32", "cygwin", "emscripten"):
        with pytest.raises(PocketError) as raised:
            SystemClock(platform)
        assert raised.value.code == "unsupported_profile" and valid_hint(raised.value.hint)
    monkeypatch.delattr(time, "CLOCK_UPTIME_RAW", raising=False)
    with pytest.raises(PocketError) as missing:
        SystemClock("darwin")
    assert missing.value.code == "unsupported_profile"


def test_this_platforms_system_clock_counts_suspended_time():
    if not sys.platform.startswith(("darwin", "linux")):
        pytest.skip("b2b runs on macOS and Linux only")
    clock = SystemClock()
    # Uptime first: the deadline clock read after it includes every sleep since boot as well.
    readings = [(clock.uptime_ns(), clock.now_ns()) for _ in range(5)]
    assert readings == sorted(readings)
    assert all(now >= uptime for uptime, now in readings)
    assert datetime.fromisoformat(clock.wall_utc()).utcoffset().total_seconds() == 0


# Broker-level replay, degrade and events, driven by claims and page calls. The act-based halves of these
# designed tests live with the action tests.

def _broker(tmp_path, **harness):
    from b2b_harness import Harness

    h = Harness(tmp_path, **harness)
    h.register()
    h.attach_page()
    return h


def _claim_request(h, request_id: str, resources=("synthetic/right/gain",)) -> dict:
    basis = h.invitation_basis(h.invite(list(resources)))
    return {"request_id": request_id, **h.triple(), "resources": list(resources), "basis": basis,
            "reason": "shape the level", "lease_ms": 3000, "not_after_monotonic_ns": h.now + 5000 * MS}


def test_replay_lookup_precedes_the_stale_check(tmp_path):
    h = _broker(tmp_path)
    request = _claim_request(h, "claim-1")
    first = h.broker.claim(h.session_id, request)
    assert first["outcome"]["kind"] == "granted" and first["replayed"] is False
    assert h.observe()["control_revision"] > request["expected_control_revision"]
    again = h.broker.claim(h.session_id, request)  # its own grant moved the revision on; replay wins
    assert again["replayed"] is True and again["outcome"] == first["outcome"]
    assert len(h.observe()["grants"]) == 1
    assert len([e for e in h.entries("request_started") if e["data"]["request_id"] == "claim-1"]) == 1


def test_changed_inputs_under_the_same_request_id_fail_with_idempotency_conflict(tmp_path):
    h = _broker(tmp_path)
    request = _claim_request(h, "claim-1")
    h.broker.claim(h.session_id, request)
    with pytest.raises(PocketError) as caught:
        h.broker.claim(h.session_id, {**request, "reason": "something else"})
    assert caught.value.code == "idempotency_conflict" and "b2b_request_status" in caught.value.hint
    grant_id = h.observe()["grants"][0]["grant_id"]
    with pytest.raises(PocketError) as caught:
        h.broker.release(h.session_id, {"request_id": "claim-1", **h.triple(), "grant_id": grant_id})
    assert caught.value.code == "idempotency_conflict"
    assert len(h.observe()["grants"]) == 1


def test_stale_revision_and_shape_errors_do_not_consume_the_request_id(tmp_path):
    h = _broker(tmp_path)
    request = _claim_request(h, "claim-1")
    for broken, code in (({**request, "expected_control_revision": 0}, "stale_revision"),
                         ({**request, "lease_ms": True}, "invalid_arguments"),
                         ({**request, "resources": ["synthetic/right/volume"]}, "unsupported_profile")):
        with pytest.raises(PocketError) as caught:
            h.broker.claim(h.session_id, broken)
        assert caught.value.code == code
    assert [e for e in h.entries("request_started") if e["data"]["request_id"] == "claim-1"] == []
    assert h.broker.claim(h.session_id, request)["outcome"]["kind"] == "granted"


def test_replay_of_an_evicted_receipt_reads_and_verifies_its_journal_line(tmp_path, monkeypatch):
    import pocket_music.b2b_broker as broker_module

    monkeypatch.setattr(broker_module, "RECEIPT_CACHE", 1)
    h = _broker(tmp_path)
    first_request = _claim_request(h, "claim-1")
    first = h.broker.claim(h.session_id, first_request)
    h.broker.claim(h.session_id, _claim_request(h, "claim-2", ("synthetic/right/filter",)))
    assert ("agent", "claim-1") not in h.broker._receipts  # evicted from memory
    replay = h.broker.claim(h.session_id, first_request)
    assert replay["replayed"] is True and replay["outcome"] == first["outcome"]
    h.journal.sync()
    trusted, _ = read_entries(h.journal_path)
    offset = next(o for o, e in trusted
                  if e["kind"] == "request_completed" and e["data"]["request_id"] == "claim-1")
    data = bytearray(h.journal_path.read_bytes())
    end = data.index(b"\n", offset)
    data[offset:end] = bytes(data[offset:end]).replace(b"shape the level", b"shape the levem")
    h.journal_path.write_bytes(bytes(data))  # same length, so every offset still holds
    with pytest.raises(PocketError) as caught:
        h.broker.claim(h.session_id, first_request)
    assert caught.value.code == "request_not_complete" and "b2b_request_status" in caught.value.hint
    assert len(h.observe()["grants"]) == 2  # nothing was executed again


def test_full_journal_ends_agent_authority_but_human_priority_still_applies(tmp_path):
    from b2b_harness import default_draft

    h = _broker(tmp_path, journal_max_bytes=1024 * 1024)
    proposal = h.propose(default_draft())["proposal"]
    preview = h.broker.trusted_preview(h.channel, {"proposal_id": proposal["proposal_id"]})
    h.broker.trusted_issue(h.channel, {"request_id": "page-issue", "proposal_id": proposal["proposal_id"],
                                       "proposal_sha256": preview["proposal_sha256"],
                                       "preview_sha256": preview["preview_sha256"],
                                       "attribution_name": "Test person", "attribution_statement": ""})
    granted = h.broker.claim(h.session_id, _claim_request(h, "claim-1"))
    h.ack(granted["notice"])
    h.invite(["synthetic/mixer/crossfader"])
    with pytest.raises(JournalUnavailable):
        while True:  # a long session's entries, written straight to the journal, use up its budget
            h.journal.append("readback", {"pad": "x" * 100_000}, session_epoch=SESSION)
    observation = h.observe()  # the next broker entry sees the journal leave "ok"
    assert observation["grants"] == observation["invitations"] == observation["permissions"] == []
    assert observation["evidence"]["journal"]["state"] == "full"
    degraded = [e for e in h.broker.events(h.session_id)["events"] if e["type"] == "journal_degraded"]
    assert degraded and degraded[0]["state"] == "full"
    invite = h.invite(["synthetic/right/filter"])
    assert invite["outcome"] == {"kind": "refused", "code": "journal_unavailable",
                                 "detail": "The journal is full"}
    claim = h.claim(["synthetic/right/filter"], {"kind": "invitation", "invitation_id": "i-0000000000000000"})
    assert claim["outcome"]["code"] == "journal_unavailable"
    h.move("controller/synthetic/channel-2/trim", 40)  # human priority keeps working in memory
    gain = next(r for r in h.observe()["resources"] if r["resource_id"] == "synthetic/right/gain")
    assert gain["owner"]["kind"] == "human"
    held = h.broker.reduce_hold({"request_id": "page-hold", "resources": ["synthetic/left/gain"]})
    assert held["outcome"]["kind"] == "held"
    assert h.observe()["evidence"]["journal"]["lost_entries"] > 0


def test_events_cursor_pages_within_bounds_and_reports_cursor_expired(tmp_path, monkeypatch):
    import pocket_music.b2b_broker as broker_module

    h = _broker(tmp_path)
    for _ in range(4):
        h.invite(["synthetic/right/gain"])
    everything = h.broker.events(h.session_id, {"max_events": 256})
    sequences = [e["sequence"] for e in everything["events"]]
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
    page = h.broker.events(h.session_id, {"max_events": 2})
    assert [e["sequence"] for e in page["events"]] == sequences[:2]
    rest = h.broker.events(h.session_id, {"after": page["next_cursor"], "max_events": 256})
    assert [e["sequence"] for e in rest["events"]] == sequences[2:]
    tight = h.broker.events(h.session_id, {"max_bytes": 1024})
    assert tight["truncated_by_bytes"] is True and len(tight["events"]) >= 1
    other = h.broker.events(h.session_id, {"after": {"host_epoch": "h" + "f" * 16, "sequence": 0}})
    assert (other["cursor_expired"], other["cursor_expired_reason"], other["events"]) == (
        True, "host_epoch_changed", [])
    monkeypatch.setattr(broker_module, "MAX_EVENTS", 4)
    for _ in range(4):
        h.invite(["synthetic/right/filter"])
    expired = h.broker.events(h.session_id, {"after": {"host_epoch": HOST_EPOCH, "sequence": 1}})
    assert expired["cursor_expired"] is True and expired["cursor_expired_reason"] == "before_retained"
    assert expired["next_cursor"]["sequence"] == expired["earliest_retained_sequence"] - 1
    resumed = h.broker.events(h.session_id, {"after": expired["next_cursor"]})
    assert resumed["cursor_expired"] is False and len(resumed["events"]) == 4
    for bad in ({"max_events": 0}, {"max_bytes": 100}, {"after": {"sequence": 1}}):
        with pytest.raises(PocketError) as caught:
            h.broker.events(h.session_id, bad)
        assert caught.value.code == "invalid_arguments"


def test_request_status_never_redispatches(tmp_path):
    h = _broker(tmp_path)
    claim = h.broker.claim(h.session_id, _claim_request(h, "claim-1"))
    started = len(h.entries("request_started"))
    status = h.broker.request_status(h.session_id, "claim-1")
    assert status["state"] == "complete" and status["receipt"]["replayed"] is True
    assert status["receipt"]["outcome"] == claim["outcome"]
    assert status["redispatched"] is False and status["automatic_retry"] is False
    assert h.broker.request_status(h.session_id, "claim-9")["state"] == "not_found"
    assert len(h.entries("request_started")) == started and len(h.observe()["grants"]) == 1


GAIN = "synthetic/right/gain"
RAMP = {"kind": "ramp", "resource": GAIN, "target": 40, "duration_ms": 500}


def _granted(h, resources=(GAIN,)) -> str:
    receipt = h.broker.claim(h.session_id, _claim_request(h, h.request_id(), resources))
    assert receipt["outcome"]["kind"] == "granted", receipt["outcome"]
    assert h.ack(receipt["notice"])["accepted"]
    return receipt["grant"]["grant_id"]


def _act_request(h, request_id: str, grant_id: str, action: dict) -> dict:
    return {"request_id": request_id, **h.triple(), "grant_id": grant_id, "action": action,
            "not_after_monotonic_ns": h.now + 5000 * MS}


def _finish(h, run, limit_ms=5000) -> dict:
    for _ in range(limit_ms // 10):
        result = h.broker.act_result(run)
        if result is not None:
            return result
        h.tick(10)
    raise AssertionError("the act did not finish")


def test_identical_request_id_replays_without_a_second_move(tmp_path):
    h = _broker(tmp_path)
    request = _act_request(h, "act-1", _granted(h), {"kind": "set", "resource": GAIN, "value": 70})
    first = _finish(h, h.broker.act_begin(h.session_id, request))
    sent = h.adapter.sent()
    assert first["outcome"]["kind"] == "verified" and len(sent) == 1
    h.tick(100)
    run = h.broker.act_begin(h.session_id, request)  # a lost response, retried exactly
    again = h.broker.act_result(run)
    assert run.ready is not None and again["replayed"] is True and again["outcome"] == first["outcome"]
    assert h.adapter.sent() == sent
    assert len([e for e in h.entries("request_started") if e["data"]["request_id"] == "act-1"]) == 1


def test_in_flight_duplicate_is_request_not_complete(tmp_path):
    h = _broker(tmp_path)
    request = _act_request(h, "act-1", _granted(h), RAMP)
    run = h.broker.act_begin(h.session_id, request)
    h.tick(100)
    with pytest.raises(PocketError) as caught:
        h.broker.act_begin(h.session_id, request)
    assert caught.value.code == "request_not_complete" and "b2b_request_status" in caught.value.hint
    with pytest.raises(PocketError) as caught:
        h.broker.act_begin(h.session_id, {**request, "action": {**RAMP, "target": 50}})
    assert caught.value.code == "idempotency_conflict"
    status = h.broker.request_status(h.session_id, "act-1")
    assert status["state"] == "running" and status["receipt"] is None
    finished = _finish(h, run)
    assert finished["outcome"]["kind"] == "verified" and len(h.adapter.sent()) == 10
    assert h.broker.act_result(h.broker.act_begin(h.session_id, request))["replayed"] is True


def test_journal_failure_after_submit_is_outcome_unknown_and_blocks_admissions(tmp_path):
    failing = {"kind": None}

    def writer(fd, data):
        if failing["kind"] is not None and f'"kind":"{failing["kind"]}"'.encode() in data:
            raise OSError(errno.ENOSPC, "No space left on device")
        return os.write(fd, data)

    h = _broker(tmp_path, writer=writer)
    gid = _granted(h)
    run = h.broker.act_begin(h.session_id, _act_request(h, h.request_id(), gid, RAMP))
    h.tick(100)
    assert len(h.adapter.sent()) == 3
    failing["kind"] = "step_submitted"  # step 4 goes out, then its record can't be written
    h.tick(50)
    result = h.broker.act_result(run)
    assert result["outcome"]["kind"] == "outcome_unknown" and result["outcome"]["automatic_retry"] is False
    assert len(h.adapter.sent()) == 4
    observation = h.observe()
    assert observation["evidence"]["journal"]["state"] == "failed" and observation["grants"] == []
    h.tick(500)
    assert len(h.adapter.sent()) == 4  # nothing more is admitted
    assert h.invite(["synthetic/right/filter"])["outcome"]["code"] == "journal_unavailable"
    refused = h.claim(["synthetic/right/filter"],
                      {"kind": "invitation", "invitation_id": "i-0000000000000000"})
    assert refused["outcome"]["code"] == "journal_unavailable"
    h.move("controller/synthetic/channel-2/trim", 30)  # human priority still applies in memory
    owner = next(r for r in h.observe()["resources"] if r["resource_id"] == GAIN)["owner"]
    assert owner["kind"] == "human"


def test_journal_failure_before_admission_cancels_the_unsent_steps(tmp_path):
    h = _broker(tmp_path, journal_max_bytes=1024 * 1024)
    gid = _granted(h)
    before = h.broker.act_begin(h.session_id, _act_request(h, h.request_id(), gid, RAMP))
    h.tick(100)
    h.journal.max_bytes = h.journal.bytes + RESERVE_BYTES  # step 4's admission overflows the budget
    h.tick(50)
    result = h.broker.act_result(before)
    # Steps already went out, so the outcome can't be known for certain once the journal is gone.
    assert result["outcome"]["kind"] == "outcome_unknown" and len(h.adapter.sent()) == 3
    assert h.observe()["evidence"]["journal"]["state"] == "full"


class _OrderSpy:
    """Wraps the synthetic adapter's submit to read the journal file as each step is submitted."""

    def __init__(self, h):
        self.h, self.seen = h, []
        self.submit = h.adapter.submit
        h.adapter.submit = self

    def __call__(self, step, admission):
        lines = self.h.journal_path.read_bytes().splitlines()
        last = json.loads(lines[-1])
        self.seen.append((last["kind"], last["data"].get("step"), step.index + 1))
        return self.submit(step, admission)


def test_admission_is_written_before_submit_and_fsync_runs_outside_the_lock(tmp_path):
    held = []
    box = {}

    def fsync(fd):
        held.append(box["broker"]._lock.locked())
        os.fsync(fd)

    h = _broker(tmp_path, fsync=fsync)
    box["broker"] = h.broker
    spy = _OrderSpy(h)
    gid = _granted(h)
    result = _finish(h, h.broker.act_begin(h.session_id, _act_request(h, h.request_id(), gid, RAMP)))
    assert result["outcome"]["kind"] == "verified"
    # Each step's admission is in the kernel before submit runs (no fsync needed to read it back).
    assert spy.seen == [("step_admitted", n, n) for n in range(1, 11)]
    assert held == []  # the broker itself never fsyncs
    assert h.journal.sync() == "ok"  # the host's RPC thread syncs after the broker returns
    assert held == [False]


def test_synthetic_hour_stays_within_memory_and_journal_budgets(tmp_path):
    # A bound check, not a soak measurement: 60 TestClock minutes with encoder noise every 10 ms
    # on the left deck while the agent claims, acts and releases on the right deck.
    import pocket_music.b2b_broker as broker_module

    h = _broker(tmp_path, journal_max_bytes=32 * 1024 * 1024)
    noise = [h.raw("controller/synthetic/channel-1/trim", v) for v in range(128)]
    outcomes = {}
    for second in range(3600):
        if second % 15 == 0:
            gid = _granted(h, (GAIN, "synthetic/right/filter"))
            run = h.broker.act_begin(h.session_id, _act_request(
                h, h.request_id(), gid, {"kind": "ramp", "resource": GAIN, "target": 40 + second % 50,
                                         "duration_ms": 300}))
        for tick in range(100):
            h.adapter.inject_message(noise[(second * 100 + tick) % 128])
            h.tick_ns(10 * MS)
        if second % 15 == 0:
            result = h.broker.act_result(run)
            outcomes[result["outcome"]["kind"]] = outcomes.get(result["outcome"]["kind"], 0) + 1
            h.broker.release(h.session_id, {"request_id": h.request_id(), **h.triple(), "grant_id": gid})
            h.broker.trusted_accept_pickup(h.channel, {"request_id": h.request_id("page"), **h.triple(),
                                                       "resource": GAIN})
    b = h.broker
    assert outcomes == {"verified": 240}
    assert len(b._events) <= broker_module.MAX_EVENTS and b._event_bytes <= broker_module.MAX_EVENT_BYTES
    assert len(b._receipts) <= broker_module.RECEIPT_CACHE
    assert len(b._requests["agent"]) == 3 * 240 and len(b._requests["page"]) == 2 * 240
    assert len(b._runs) <= broker_module.MAX_RUN_HISTORY and len(b._grants) <= broker_module.MAX_GRANT_HISTORY
    assert len(b._notices) <= broker_module.MAX_NOTICES
    assert len(b._invitations) <= broker_module.MAX_ENDED_RECORDS + broker_module.MAX_INVITATIONS
    assert len(b._windows) <= 4096 + 1
    status = h.journal.status()
    assert status["state"] == "ok" and status["lost_entries"] == 0
    assert status["bytes"] <= status["max_bytes"] - RESERVE_BYTES


def test_an_incomplete_read_never_says_nothing_was_sent(tmp_path):
    j, path, _ = open_journal(tmp_path)
    started(j, "r-act")
    started(j, "r-later")  # its admission sits past the read bound below
    admitted(j, "r-later")
    j.sync()
    bound = len(path.read_bytes().splitlines(keepends=True)[0]) + 10
    assert read_request(path, "r-act", max_bytes=bound)["state"] == "outcome_unknown"  # bound reached
    assert read_request(path, "r-later", max_bytes=bound)["state"] == "outcome_unknown"
    assert read_request(path, "r-act")["state"] == "stopped_before_dispatch"  # a full read can say so
    data = bytearray(path.read_bytes())
    second = data.index(b"\n") + 1
    data[second + 20:second + 21] = b"X"  # tamper with the second line: the chain breaks there
    path.write_bytes(bytes(data))
    broken = read_request(path, "r-missing")
    assert broken["journal"]["chain"] == "chain_broken" and broken["state"] == "outcome_unknown"


def test_refusals_while_the_journal_is_unavailable_stay_bounded(tmp_path, monkeypatch):
    import pocket_music.b2b_broker as broker_module

    monkeypatch.setattr(broker_module, "MAX_REQUESTS", 4)
    monkeypatch.setattr(broker_module, "RECEIPT_CACHE", 2)
    h = _broker(tmp_path, journal_max_bytes=1024 * 1024)
    h.journal.max_bytes = h.journal.bytes + RESERVE_BYTES
    with pytest.raises(JournalUnavailable):
        h.journal.append("readback", {"pad": "x"}, session_epoch=SESSION)  # the journal is now full
    basis = {"kind": "invitation", "invitation_id": "i-0000000000000000"}
    accepted = 0
    for _ in range(8):
        try:
            receipt = h.claim([GAIN], basis)
        except PocketError as error:
            assert error.code == "invalid_request"
            break
        assert receipt["outcome"]["code"] == "journal_unavailable"
        accepted += 1
    assert accepted < 8 and len(h.broker._requests["agent"]) <= 4 and len(h.broker._receipts) <= 2
