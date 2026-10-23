# SPDX-License-Identifier: AGPL-3.0-only
"""The b2b host: its runtime dir, agent socket, trusted page routes and the page-link seam.

Hosts here run in-process on a TestClock with the scheduler off, so each test ticks the host
itself. Socket and HTTP binds can be refused in sandboxed runs; those tests skip. Nothing here
starts Serato, MIDI, an audio device or a subprocess.
"""
from __future__ import annotations

import http.client
import io
import json
import shutil
import socket
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest
from b2b_harness import MS, START_NS, Harness, default_draft

from pocket_music import b2b
from pocket_music.b2b_clock import TestClock
from pocket_music.b2b_host import B2bHost, _socket_dir, main
from pocket_music.b2b_ui import TrustedPage
from pocket_music.errors import PocketError
from pocket_music.loopback import RequestRefused

GAIN, FILTER = "synthetic/right/gain", "synthetic/right/filter"
WEB = Path(__file__).resolve().parents[1] / "src" / "pocket_music" / "web" / "b2b"


@pytest.fixture
def socket_parent():
    try:
        parent = tempfile.mkdtemp(dir="/tmp")  # tmp_path can be too long for an AF_UNIX path
    except OSError as error:
        pytest.skip(f"No short socket directory: {error}")
    yield parent
    shutil.rmtree(parent, ignore_errors=True)


def _host(tmp_path, socket_parent, name="runtime") -> B2bHost:
    try:
        host = B2bHost(tmp_path / name, clock=TestClock(START_NS), scheduler=False,
                       socket_parent=socket_parent)
    except PermissionError as error:
        pytest.skip(f"Sockets are not available here: {error}")
    host.start()
    return host


@pytest.fixture
def host(tmp_path, socket_parent):
    running = _host(tmp_path, socket_parent)
    yield running
    running.stop()


def tick(host: B2bHost, ms: int, step: int = 10, *, presence: str | None = None) -> None:
    """Advance the host's TestClock; with ``presence`` the page reports itself every second, as it does."""
    for count in range(1, ms // step + 1):
        host.clock.advance(step * MS)
        host.tick()
        if presence is not None and count * step % 1000 == 0:
            issuer(host, "POST", "/api/presence", {"ui_epoch": presence, "visible": True})


def http_call(host: B2bHost, method: str, path: str, body=None, *, key=None, origin=None, host_header=None,
              content_type="application/json"):
    port = int(host.page_origin.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": host_header or f"127.0.0.1:{port}"}
    if origin is not False:
        headers["Origin"] = origin or host.page_origin
    if key is not None:
        headers["X-Pocket-CSRF"] = key
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers.update({"Content-Type": content_type, "Content-Length": str(len(payload))})
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw
    return response.status, data, dict(response.getheaders())


def issuer(host: B2bHost, method: str, path: str, body=None):
    status, data, _ = http_call(host, method, path, body, key=host.page.issuer_token)
    assert status == 200, (path, status, data)
    return data


def public(host: B2bHost, path: str, body):
    csrf = http_call(host, "GET", "/api/public")[1]["csrf_token"]
    status, data, _ = http_call(host, "POST", path, body, key=csrf)
    assert status == 200, (path, status, data)
    return data


def attach(host: B2bHost) -> str:
    ui_epoch = issuer(host, "POST", "/api/attach", {})["ui_epoch"]
    issuer(host, "POST", "/api/presence", {"ui_epoch": ui_epoch, "visible": True})
    return ui_epoch


def prepare(host: B2bHost, tmp_path, **options) -> str:
    session_dir = tmp_path / "session"
    result = b2b.b2b_prepare(str(host.runtime_dir), str(session_dir), **options)
    assert result["status"] == "ok" and result["outcome"]["kind"] == "prepared"
    return str(session_dir)


def triple(record: dict) -> dict:
    return {"expected_session_epoch": record["session_epoch"],
            "expected_control_revision": record["control_revision"],
            "expected_state_sha256": record["state_sha256"]}


def ack_pending(host: B2bHost, ui_epoch: str) -> None:
    state = issuer(host, "GET", f"/api/state?ui_epoch={ui_epoch}")  # the page fetches, then acknowledges
    for notice in state["notices"]:
        if notice["state"] == "pending":
            answer = issuer(host, "POST", "/api/notices/ack", {
                "notice_id": notice["notice_id"], "grant_id": notice["grant_id"],
                "session_epoch": notice["session_epoch"], "ui_epoch": ui_epoch,
                "action_deadline_monotonic_ns": notice["action_deadline_monotonic_ns"]})
            assert answer["accepted"] is True, answer


def raw_rpc(host: B2bHost, message) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(30)
        stream.connect(str(host.socket_path))
        stream.sendall((message if isinstance(message, bytes) else json.dumps(message).encode()) + b"\n")
        return json.loads(stream.makefile("rb").readline())


# Runtime dir and the page link --------------------------------------------------------------------

def test_host_refuses_an_existing_runtime_dir(tmp_path, socket_parent):
    (tmp_path / "runtime").mkdir()
    with pytest.raises(PocketError) as caught:
        B2bHost(tmp_path / "runtime", clock=TestClock(), scheduler=False, socket_parent=socket_parent)
    assert caught.value.code == "io_error" and "new --runtime-dir" in caught.value.hint
    assert list((tmp_path / "runtime").iterdir()) == []
    assert list(Path(socket_parent).iterdir()) == []


class _Terminal(io.StringIO):
    def close(self):
        self.text = self.getvalue()
        super().close()


def _no_terminal():
    raise OSError("no terminal")


def test_page_link_goes_to_the_terminal_or_requires_the_piped_stdout_flag(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    assert main(["--runtime-dir", str(tmp_path / "refused")], open_tty=_no_terminal, stdout=out, stderr=err,
                wait=lambda host: None) == 2
    assert not (tmp_path / "refused").exists() and out.getvalue() == ""
    assert "Start the b2b host from your own terminal" in err.getvalue()
    terminal, out, err = _Terminal(), io.StringIO(), io.StringIO()
    try:
        code = main(["--runtime-dir", str(tmp_path / "tty")], open_tty=lambda: terminal, stdout=out,
                    stderr=err, wait=lambda host: None)
    except PermissionError as error:
        pytest.skip(f"Sockets are not available here: {error}")
    assert code == 0 and "Open the b2b page: http://127.0.0.1:" in terminal.text and "/#k=" in terminal.text
    started = json.loads(out.getvalue())
    assert started["page_link"] == "printed_to_terminal" and "#k=" not in out.getvalue() + err.getvalue()
    assert started["adapter_profile"] == "synthetic-two-deck/v1"
    out, err = io.StringIO(), io.StringIO()
    assert main(["--runtime-dir", str(tmp_path / "piped"), "--allow-piped-stdout"], open_tty=_no_terminal,
                stdout=out, stderr=err, wait=lambda host: None) == 0
    assert "/#k=" in json.loads(out.getvalue())["page"]
    assert "carries the right to issue standing permissions" in err.getvalue()
    for name in ("tty", "piped"):  # only these two files, and the link is in neither
        files = sorted(p.name for p in (tmp_path / name).iterdir())
        assert files == ["connection.json", "journal.ndjson"]
        assert b"#k=" not in b"".join((tmp_path / name / f).read_bytes() for f in files)


def test_page_token_appears_in_no_file_receipt_event_or_journal(host, tmp_path):
    ui_epoch = attach(host)
    session = prepare(host, tmp_path, proposed_policy=default_draft())
    receipts = [b2b.b2b_authorize(session, "auth-1", **triple(b2b.b2b_observe(session)),
                                  permission=default_draft())]
    invitation = issuer(host, "POST", "/api/invitations", {"request_id": "page-1",
                                                            **triple(b2b.b2b_observe(session)),
                                                            "resources": [GAIN]})
    observed = b2b.b2b_observe(session)
    invited = invitation["outcome"]["invitation"]["invitation_id"]
    receipts.append(b2b.b2b_claim(session, "claim-1", **triple(observed), resources=[GAIN],
                                  basis={"kind": "invitation", "invitation_id": invited},
                                  reason="shape the level", lease_ms=3000,
                                  not_after_monotonic_ns=observed["monotonic_ns"] + 5000 * MS))
    ack_pending(host, ui_epoch)
    claim = receipts[-1]
    receipts.append(b2b.b2b_act(session, "act-1", **triple(claim), grant_id=claim["grant"]["grant_id"],
                                action={"kind": "set", "resource": GAIN, "value": 80},
                                not_after_monotonic_ns=claim["monotonic_ns"] + 4000 * MS))
    assert receipts[-1]["outcome"]["kind"] == "verified"
    receipts += [b2b.b2b_events(session, max_events=256, max_bytes=262_144), b2b.b2b_observe(session),
                 b2b.b2b_request_status(session, "act-1")]
    secrets = (host.page.issuer_token, host.page.public_csrf)
    blob = json.dumps(receipts)
    assert not any(secret in blob for secret in secrets)
    for folder in (host.runtime_dir, Path(session)):
        for path in folder.rglob("*"):
            data = path.read_bytes()
            assert not any(secret.encode() in data for secret in secrets), path
    journal = (host.runtime_dir / "journal.ndjson").read_bytes()
    assert host.nonce.encode() not in journal  # the nonce lives only in the private connection.json


# The agent socket ----------------------------------------------------------------------------------

def test_agent_socket_rejects_nonce_mismatch_unknown_ops_and_trusted_operations(host):
    wrong = raw_rpc(host, {"op": "observe", "nonce": "0" * 48, "session_id": "x"})
    assert wrong["status"] == "error" and wrong["code"] == "invalid_request" and "nonce" in wrong["hint"]
    for op in ("ack", "trusted_ack", "invite", "issue", "hold", "panic", "inject", "synthetic_input",
               "acknowledge"):
        refused = raw_rpc(host, {"op": op, "nonce": host.nonce, "session_id": "x", "request": {}})
        assert refused["status"] == "error" and refused["code"] == "invalid_request", op
        assert "belong to the host page" in refused["hint"]
    assert raw_rpc(host, b"{not json")["code"] == "invalid_request"
    extra = raw_rpc(host, {"op": "observe", "nonce": host.nonce, "session_id": "x", "issuer": "me"})
    assert extra["code"] == "invalid_request"
    # The host still answers after every refusal; an unknown session is refused too.
    unknown = raw_rpc(host, {"op": "observe", "nonce": host.nonce, "session_id": "x"})
    assert unknown["code"] == "invalid_request"


class _SilentHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()  # take the request, then close without answering


def _fake_session(tmp_path, socket_path: str) -> str:
    runtime = tmp_path / "fake-runtime"
    runtime.mkdir()
    (runtime / "connection.json").write_text(json.dumps({
        "schema": "pocket.b2b-connection/v1", "socket": socket_path, "nonce": "n" * 48,
        "host_epoch": "hfake"}))
    session = tmp_path / "fake-session"
    session.mkdir()
    b2b._seal(session / "b2b.json", {"schema": "pocket.b2b-session/v1", "session_id": "s" * 32,
                                    "runtime_dir": str(runtime), "host_epoch": "hfake"})
    return str(session)


def test_rpc_errors_keep_their_code_and_hint(host, tmp_path, socket_parent):
    attach(host)
    session = prepare(host, tmp_path)
    observed = b2b.b2b_observe(session)
    with pytest.raises(PocketError) as caught:
        b2b.b2b_claim(session, "claim-1", **{**triple(observed), "expected_control_revision": 999},
                      resources=[GAIN], basis={"kind": "invitation", "invitation_id": "i-0000000000000000"},
                      reason="x", lease_ms=3000, not_after_monotonic_ns=observed["monotonic_ns"] + MS)
    assert caught.value.code == "stale_revision" and "b2b_observe" in caught.value.hint
    silent_path = str(Path(socket_parent) / "silent.sock")
    server = socketserver.ThreadingUnixStreamServer(silent_path, _SilentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        fake = _fake_session(tmp_path, silent_path)
        with pytest.raises(PocketError) as caught:
            b2b.b2b_release(fake, "release-1", "hfake.1", 0, "0" * 64, "g-" + "0" * 32)
        assert caught.value.code == "request_not_complete" and "b2b_request_status" in caught.value.hint
        with pytest.raises(PocketError) as caught:
            b2b.b2b_observe(fake)
        assert caught.value.code == "io_error" and "nothing changed" in caught.value.hint
    finally:
        server.shutdown()
        server.server_close()
    (tmp_path / "gone").mkdir()
    gone = _fake_session(tmp_path / "gone", str(Path(socket_parent) / "missing.sock"))
    with pytest.raises(PocketError) as caught:
        b2b.b2b_release(gone, "release-2", "hfake.1", 0, "0" * 64, "g-" + "0" * 32)
    assert caught.value.code == "io_error" and "did not receive this request" in caught.value.hint


# The trusted page ------------------------------------------------------------------------------------

def test_trusted_page_operations_validate_without_a_socket(tmp_path):
    h = Harness(tmp_path)
    h.register()
    page = TrustedPage(h.broker, h.channel, adapter=h.adapter)
    for call, body in ((page.attach, {"extra": 1}),
                       (page.presence, {"ui_epoch": "x", "visible": True, "x": 1}),
                       (page.synthetic_input, {"message": [256, 1, 1]}),
                       (page.synthetic_input, {"message": [0xB1, 19, 90], "origin": "agent"}),
                       (page.synthetic_fault, {"action": "explode"})):
        with pytest.raises(PocketError) as caught:
            call(body)
        assert caught.value.code == "invalid_arguments"
    ui_epoch = page.attach({})["ui_epoch"]
    assert page.presence({"ui_epoch": ui_epoch, "visible": True}) == {"current": True, "visible": True}
    with pytest.raises(PocketError):
        page.wait(ui_epoch, "-1")
    state = page.state(ui_epoch)
    invited = page.invite({"request_id": "page-1", **triple(state), "resources": [GAIN]})
    assert invited["outcome"]["kind"] == "invited"
    assert page.synthetic_input({"message": [0xB1, 19, 90]}) == {"delivered": 1, "types": ["physical_input"]}
    panel = page.synthetic_fault({"action": "disconnect"})["panel"]
    assert panel["dependencies"]["controller"] == "unavailable"
    shown = json.dumps(page.public())
    assert page.public_csrf in shown and page.issuer_token not in shown
    (tmp_path / "bare").mkdir()
    bare = TrustedPage(Harness(tmp_path / "bare").broker, None)
    with pytest.raises(RequestRefused) as refused:
        bare.synthetic_input({"message": [0xB1, 19, 90]})
    assert refused.value.status == 404


def test_authority_routes_require_the_issuer_token_and_loopback_guards(host):
    status, page, headers = http_call(host, "GET", "/")
    assert status == 200 and b"Stop agent control" in page
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert http_call(host, "GET", "/api/state?ui_epoch=x")[0] == 403
    csrf = http_call(host, "GET", "/api/public")[1]["csrf_token"]
    assert http_call(host, "GET", "/api/state?ui_epoch=x", key=csrf)[0] == 403
    assert http_call(host, "POST", "/api/attach", {}, key=csrf)[0] == 403  # the public token can't attach
    token = host.page.issuer_token
    assert http_call(host, "POST", "/api/attach", {}, key=token, host_header="localhost:1")[0] == 403
    assert http_call(host, "POST", "/api/attach", {}, key=token, origin="http://evil.example")[0] == 403
    assert http_call(host, "POST", "/api/attach", {}, key=token, origin=False)[0] == 403
    assert http_call(host, "POST", "/api/attach", b"x" * 20_000, key=token)[0] == 413
    assert http_call(host, "POST", "/api/attach", {}, key=token, content_type="text/plain")[0] == 415
    assert http_call(host, "POST", "/api/synthetic/input", {"message": [0xB1, 19, 1]}, key=csrf)[0] == 403
    assert http_call(host, "OPTIONS", "/api/attach")[0] == 405
    assert http_call(host, "POST", "/api/attach", {}, key=token)[0] == 200


def test_reduce_only_routes_work_after_a_reload_without_the_issuer_token(host, tmp_path):
    ui_epoch = attach(host)
    session = prepare(host, tmp_path)
    invitation = issuer(host, "POST", "/api/invitations", {"request_id": "page-1",
                                                            **triple(b2b.b2b_observe(session)),
                                                            "resources": [GAIN]})
    observed = b2b.b2b_observe(session)
    claim = b2b.b2b_claim(session, "claim-1", **triple(observed), resources=[GAIN],
                          basis={"kind": "invitation",
                                 "invitation_id": invitation["outcome"]["invitation"]["invitation_id"]},
                          reason="shape the level", lease_ms=3000,
                          not_after_monotonic_ns=observed["monotonic_ns"] + 5000 * MS)
    reloaded = http_call(host, "GET", "/api/public")[1]  # a reloaded page has only what /api/public serves
    assert host.page.issuer_token not in json.dumps(reloaded)
    (notice,) = reloaded["notices"]
    csrf = reloaded["csrf_token"]
    status, _, _ = http_call(host, "POST", "/api/notices/ack", {
        "notice_id": notice["notice_id"], "grant_id": notice["grant_id"],
        "session_epoch": claim["session_epoch"], "ui_epoch": ui_epoch,
        "action_deadline_monotonic_ns": notice["action_deadline_monotonic_ns"]}, key=csrf)
    assert status == 403  # no acknowledgement without the issuer token
    assert http_call(host, "POST", "/api/policy/issue", {"request_id": "page-x"}, key=csrf)[0] == 403
    denied = public(host, "/api/notices/deny", {"request_id": "page-2", "notice_id": notice["notice_id"],
                                                 "grant_id": notice["grant_id"]})
    assert denied["outcome"]["kind"] == "denied"
    held = public(host, "/api/holds", {"request_id": "page-3", "resources": [FILTER]})
    assert held["outcome"]["kind"] == "held"
    sent = host.adapter.sent_count
    stopped = public(host, "/api/panic", {"request_id": "page-4"})
    assert stopped["outcome"]["kind"] == "panic" and host.adapter.sent_count == sent


def test_page_assets_use_text_content_const_and_strip_the_fragment():
    script = (WEB / "b2b.js").read_text()
    model = (WEB / "b2b-model.mjs").read_text()
    html = (WEB / "index.html").read_text()
    for source in (script, model):
        assert "innerHTML" not in source and "outerHTML" not in source and "insertAdjacentHTML" not in source
        assert "document.write" not in source and "eval(" not in source
        assert not any(line.lstrip().startswith(("let ", "var ")) or " let " in line or " var " in line
                       for line in source.splitlines())
        assert "https://" not in source and "http://" not in source  # no external resources
    assert "textContent" in script and 'history.replaceState(null, "", location.pathname)' in script
    assert script.index("tokenFromHash(location.hash)") < script.index("history.replaceState")
    assert script.splitlines()[0] == model.splitlines()[0] == "// SPDX-License-Identifier: AGPL-3.0-only"
    assert (WEB / "b2b.css").read_text().splitlines()[0] == "/* SPDX-License-Identifier: AGPL-3.0-only */"
    assert html.splitlines()[:2] == ["<!doctype html>", "<!-- SPDX-License-Identifier: AGPL-3.0-only -->"]
    assert 'src="/b2b.js"' in html and 'href="/b2b.css"' in html and "<script>" not in html
    assert " style=" not in html and "http" not in html.replace("http-equiv", "")


def test_socket_path_falls_back_to_tmp_and_refuses_when_too_long(tmp_path):
    long_parent = tmp_path / ("p" * 100)
    long_parent.mkdir()
    made = _socket_dir(long_parent)
    try:
        assert str(made).startswith(("/tmp/", "/private/tmp/")) and len(str(made / "agent.sock")) <= 100
    finally:
        made.rmdir()
    with pytest.raises(PocketError) as caught:
        _socket_dir(long_parent, fallback=str(long_parent))
    assert caught.value.code == "io_error" and "TMPDIR" in caught.value.hint
    assert list(long_parent.iterdir()) == []


# Restart and the whole workflow ----------------------------------------------------------------------

def test_restart_never_inherits_authority(tmp_path, socket_parent):
    first = _host(tmp_path, socket_parent, "run-1")
    try:
        attach(first)
        old = prepare(first, tmp_path / "old", proposed_policy=default_draft())
        observed = b2b.b2b_observe(old)
        proposal = observed["proposals"][0]
        preview = issuer(first, "POST", "/api/policy/preview", {"proposal_id": proposal["proposal_id"]})
        issued = issuer(first, "POST", "/api/policy/issue", {
            "request_id": "page-issue", "proposal_id": proposal["proposal_id"],
            "proposal_sha256": preview["proposal_sha256"], "preview_sha256": preview["preview_sha256"],
            "attribution_name": "Test person", "attribution_statement": ""})
        assert issued["outcome"]["kind"] == "issued"
        observed = b2b.b2b_observe(old)
        assert observed["permissions"]
        b2b.b2b_revoke(old, "revoke-1", **triple(observed), target={"kind": "all_own_authority"})
    finally:
        first.stop()
    with pytest.raises(PocketError) as caught:
        b2b.b2b_observe(old)
    assert caught.value.code == "io_error"
    status = b2b.b2b_request_status(old, "revoke-1")
    assert (status["source"], status["state"], status["host_running"]) == ("retained_journal", "complete",
                                                                            False)
    assert status["receipt"]["outcome"]["kind"] == "revoked" and status["journal"]["chain"] == "intact"
    assert b2b.b2b_request_status(old, "never-sent")["state"] == "not_found"
    second = _host(tmp_path, socket_parent, "run-2")
    try:
        new = prepare(second, tmp_path / "new")
        fresh = b2b.b2b_observe(new)
        assert fresh["host_epoch"] != observed["host_epoch"]
        assert fresh["permissions"] == fresh["grants"] == fresh["invitations"] == []
        assert fresh["authority_state"]["permissions"] == []
    finally:
        second.stop()


def _act_in_thread(*args, **kwargs):
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=b2b.b2b_act(*args, **kwargs)))
    thread.start()
    return thread, box


def test_end_to_end_handback_proactive_and_stop_over_socket_and_page(host, tmp_path):
    session = prepare(host, tmp_path, human_reserved=["synthetic/left/gain"], proposed_policy=default_draft())
    ui_epoch = attach(host)
    # 1. The person long-presses Hand back on the synthetic controller panel.
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 127]})
    tick(host, 600)
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 0]})
    observed = b2b.b2b_observe(session)
    (invitation,) = observed["invitations"]
    assert invitation["source"] == "gesture" and GAIN in invitation["resources"]
    # 2. The agent claims under it; the visible page acknowledges; a set is verified by readback.
    claim = b2b.b2b_claim(session, "claim-1", **triple(observed), resources=[GAIN, FILTER],
                          basis={"kind": "invitation", "invitation_id": invitation["invitation_id"]},
                          reason="shape the level", lease_ms=3000,
                          not_after_monotonic_ns=observed["monotonic_ns"] + 5000 * MS)
    assert claim["outcome"]["kind"] == "granted"
    ack_pending(host, ui_epoch)
    grant_id = claim["grant"]["grant_id"]
    set_receipt = b2b.b2b_act(session, "act-1", **triple(claim), grant_id=grant_id,
                              action={"kind": "set", "resource": GAIN, "value": 80},
                              not_after_monotonic_ns=claim["monotonic_ns"] + 4000 * MS)
    assert set_receipt["outcome"]["kind"] == "verified"
    # 3. A trim move takes the gain back at once; nothing is restored.
    issuer(host, "POST", "/api/synthetic/input", {"message": [0xB1, 19, 90]})
    observed = b2b.b2b_observe(session)
    gain = next(r for r in observed["resources"] if r["resource_id"] == GAIN)
    assert gain["owner"]["kind"] == "human" and host.adapter.value(GAIN) == 90
    assert [g["resources"] for g in observed["grants"]] == [[FILTER]]
    # 4. The person issues the agent's proposal on the page.
    proposal = observed["proposals"][0]
    preview = issuer(host, "POST", "/api/policy/preview", {"proposal_id": proposal["proposal_id"]})
    assert preview["lines"][0].startswith("May claim these controls once each has been idle for 4.0 s")
    head = issuer(host, "GET", f"/api/state?ui_epoch={ui_epoch}")["permission_head"]
    assert head is None
    issued = issuer(host, "POST", "/api/policy/issue", {
        "request_id": "page-issue", "proposal_id": proposal["proposal_id"],
        "proposal_sha256": preview["proposal_sha256"], "preview_sha256": preview["preview_sha256"],
        "attribution_name": "Test person", "attribution_statement": "Go ahead"})
    permission = issued["outcome"]["permission"]
    # 5. After the idle interval the agent claims proactively, the page acknowledges and a ramp runs.
    tick(host, 4100, presence=ui_epoch)
    observed = b2b.b2b_observe(session)
    proactive = b2b.b2b_claim(session, "claim-2", **triple(observed), resources=[GAIN],
                              basis={"kind": "standing_permission",
                                     "permission_id": permission["permission_id"],
                                     "permission_version": permission["version"],
                                     "revocation_generation": permission["revocation_generation"]},
                              reason="ease it down", lease_ms=3000,
                              not_after_monotonic_ns=observed["monotonic_ns"] + 5000 * MS)
    assert proactive["outcome"]["kind"] == "granted", proactive["outcome"]
    ack_pending(host, ui_epoch)
    before = host.adapter.sent_count
    thread, box = _act_in_thread(session, "act-2", **triple(proactive),
                                 grant_id=proactive["grant"]["grant_id"],
                                 action={"kind": "ramp", "resource": GAIN, "target": 60, "duration_ms": 500},
                                 not_after_monotonic_ns=proactive["monotonic_ns"] + 4000 * MS)
    for _ in range(100):  # wait on the broker's own condition until the act's first step is admitted
        if host.adapter.sent_count > before or not thread.is_alive():
            break
        host.broker.wait_for_event(host.broker.last_event_sequence, 0.1)
    for _ in range(200):
        if not thread.is_alive():
            break
        tick(host, 10)
    thread.join(10)
    assert box["result"]["outcome"]["kind"] == "verified", (box["result"]["outcome"], box["result"]["steps"])
    assert host.adapter.value(GAIN) == 60
    # 6. Stop agent control ends everything and sends nothing.
    sent = host.adapter.sent_count
    stopped = public(host, "/api/panic", {"request_id": "page-stop"})
    assert stopped["outcome"]["kind"] == "panic" and host.adapter.sent_count == sent
    observed = b2b.b2b_observe(session)
    assert observed["grants"] == observed["permissions"] == []
    assert host.adapter.value("synthetic/right/transport") == "playing"
    # 7. Events page in order, and request_status replays without redispatch.
    types = [e["type"] for e in b2b.b2b_events(session, max_events=256, max_bytes=262_144)["events"]]
    assert types.index("invitation_created") < types.index("grant_created") < types.index("panic")
    status = b2b.b2b_request_status(session, "act-2")
    assert status["state"] == "complete" and status["receipt"]["outcome"]["kind"] == "verified"
    assert status["redispatched"] is False and host.adapter.sent_count == sent
