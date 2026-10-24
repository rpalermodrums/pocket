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
import re
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
    except OSError as error:  # PermissionError included: sandboxed runs may refuse the binds
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
    code = main(["--runtime-dir", str(tmp_path / "tty")], open_tty=lambda: terminal, stdout=out,
                stderr=err, wait=lambda host: None)
    if code == 2 and "could not start" in err.getvalue():  # sandboxed runs may refuse the binds
        pytest.skip(f"Sockets are not available here: {err.getvalue()}")
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
    receipts += [b2b.b2b_events(session, max_events=256, max_bytes=196_608), b2b.b2b_observe(session),
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


def test_inputs_carrying_a_host_secret_are_refused_before_anything_is_recorded(host, tmp_path):
    # The agent can read the nonce and fetch the public CSRF token; echoing either back must not reach the
    # journal.
    attach(host)
    session = prepare(host, tmp_path)
    observed = b2b.b2b_observe(session)
    public_csrf = http_call(host, "GET", "/api/public")[1]["csrf_token"]
    claim = {**triple(observed), "resources": [GAIN],
             "basis": {"kind": "invitation", "invitation_id": "i-0000000000000000"}, "lease_ms": 3000,
             "not_after_monotonic_ns": observed["monotonic_ns"] + 5000 * MS}
    for request_id, reason in ((host.nonce, "shape the level"), ("claim-1", f"echo {public_csrf}")):
        with pytest.raises(PocketError) as caught:
            b2b.b2b_claim(session, request_id, reason=reason, **claim)
        assert caught.value.code == "invalid_arguments" and "host secret" in str(caught.value)
    entries = [json.loads(line) for line in (host.runtime_dir / "journal.ndjson").read_text().splitlines()]
    assert not [e for e in entries if e["kind"] == "request_started"]
    retried = b2b.b2b_claim(session, "claim-1", reason="shape the level", **claim)
    assert retried["outcome"]["kind"] == "refused"  # the request_id was not used up


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
    try:
        server = socketserver.ThreadingUnixStreamServer(silent_path, _SilentHandler)
    except OSError as error:
        pytest.skip(f"Sockets are not available here: {error}")
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


def _code_only(source: str) -> str:
    """JavaScript with comments and string or template literals removed, for declaration checks."""
    return re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`", " ",
                  source, flags=re.DOTALL)


def test_page_assets_use_text_content_const_and_strip_the_fragment():
    assert re.search(r"\b(let|var)\b", _code_only("for (let i = 0;;) {let x}"))  # the check can fail
    assert not re.search(r"\b(let|var)\b", _code_only('const a = "let it be"; // var'))
    script = (WEB / "b2b.js").read_text()
    model = (WEB / "b2b-model.mjs").read_text()
    html = (WEB / "index.html").read_text()
    for source in (script, model):
        assert "innerHTML" not in source and "outerHTML" not in source and "insertAdjacentHTML" not in source
        assert "document.write" not in source and "eval(" not in source
        assert not re.search(r"\b(let|var)\b", _code_only(source))
        assert "https://" not in source and "http://" not in source  # no external resources
    assert "textContent" in script and 'history.replaceState(null, "", location.pathname)' in script
    assert script.index("tokenFromHash(location.hash)") < script.index("history.replaceState")
    assert script.splitlines()[0] == model.splitlines()[0] == "// SPDX-License-Identifier: AGPL-3.0-only"
    assert (WEB / "b2b.css").read_text().splitlines()[0] == "/* SPDX-License-Identifier: AGPL-3.0-only */"
    assert html.splitlines()[:2] == ["<!doctype html>", "<!-- SPDX-License-Identifier: AGPL-3.0-only -->"]
    assert 'src="/b2b.js"' in html and 'href="/b2b.css"' in html and "<script>" not in html
    assert " style=" not in html and "http" not in html.replace("http-equiv", "")
    # The pad's rules live in padControl (tested in b2b_page_model.test.mjs); the page must use it.
    assert "pad.control = padControl(gesture, send, () => performance.now()," in script
    assert "ms => new Promise(done => setTimeout(done, ms))" in script  # padControl's sleep
    assert "event.button !== 0 || !event.isPrimary" in script and "setPointerCapture" in script
    assert 'window.addEventListener("blur", up)' in script and "if (event.isPrimary) up();" in script
    assert 'changed("public-permission"' in script and "(event.buttons & 1) === 0" in script
    assert 'app.signatures.delete("permission")' in script
    assert 'app.signatures.delete("public-permission")' in script
    assert script.index("setInterval(renderClock, 250)") < script.index("if (isLocked(token)) {")
    # The person's lists keep unchanged items (reconcile, tested in b2b_page_model.test.mjs), and the
    # controls are built once with their buttons in fixed places that the agent can't move or swap.
    assert all(f'reconcile($("{name}"), ' in script for name in ("notices", "grants"))
    assert 'const SLOTS = ["hold", "take", "release", "invite", "accept"];' in script
    assert 'classList.toggle("slot-off", !shown[slot])' in script
    css = (WEB / "b2b.css").read_text()
    assert ".slot-off { visibility: hidden; }" in css and "table-layout: fixed" in css
    assert "#error[hidden] { display: block; visibility: hidden; }" in css and "#notices { height:" in css
    assert ".bar { position: sticky; top: 0;" in css
    body = html[html.index("<body>"):]
    assert body.index('class="bar"') < body.index('id="stop"') < body.index("<header>")
    # An increase sent right after the page moved is refused, so a click can't land on a button that slid in.
    assert "const MOVE_GUARD_MS = 800;" in script
    assert "if (guarded && justMoved()) {" in script and 'guarded = key !== "public"' in script
    assert 'narrow("/api/proposals/dismiss", id)' in script  # what only narrows or refuses isn't guarded
    assert "new ResizeObserver(moved)" in script and "if (justMoved()) {  // the panel just moved" in script
    assert ".countdown { display: inline-block; min-width: 7.5em;" in css and "#events { height:" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "proposals.map(p => [p.proposal_id, p.sha256])" in script
    assert "return (app.current && app.snapshot) || app.publicView || app.snapshot;" in script
    assert script.count("  redraw();\n") == 2 and "if (!app.current) return;  // an answer" in script
    assert "pad.control.noteReduction(path, fields)" in script
    assert "pad.control.press()" in script and "pad.control.release()" in script


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
    thread = threading.Thread(target=lambda: box.update(result=b2b.b2b_act(*args, **kwargs)), daemon=True)
    thread.start()
    return thread, box


def test_end_to_end_handback_proactive_and_stop_over_socket_and_page(host, tmp_path):
    session = prepare(host, tmp_path, human_reserved=["synthetic/left/gain"], proposed_policy=default_draft())
    ui_epoch = attach(host)
    # 1. The person long-presses Hand back on the synthetic controller panel.
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 127]})
    tick(host, 600)
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 0], "held_ms": 600})  # as the page
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
    types = [e["type"] for e in b2b.b2b_events(session, max_events=256, max_bytes=196_608)["events"]]
    assert types.index("invitation_created") < types.index("grant_created") < types.index("panic")
    status = b2b.b2b_request_status(session, "act-2")
    assert status["state"] == "complete" and status["receipt"]["outcome"]["kind"] == "verified"
    assert status["redispatched"] is False and host.adapter.sent_count == sent


def test_host_faults_keep_a_code_that_says_what_the_agent_should_do(host, tmp_path, monkeypatch):
    attach(host)
    session = prepare(host, tmp_path)
    session_id = json.loads((Path(session) / "b2b.json").read_text())["session_id"]

    def broken(*_args):
        raise RuntimeError("a host bug")

    monkeypatch.setattr(host.broker, "claim", broken)
    monkeypatch.setattr(host.broker, "observe", broken)
    mutation = raw_rpc(host, {"op": "claim", "nonce": host.nonce, "session_id": session_id, "request": {}})
    assert mutation["code"] == "request_not_complete" and "b2b_request_status" in mutation["hint"]
    read = raw_rpc(host, {"op": "observe", "nonce": host.nonce, "session_id": session_id})
    assert read["code"] == "invalid_request" and "nothing changed" in read["hint"]


def test_writes_fsync_outside_the_broker_lock_and_report_a_failed_sync(host, tmp_path, monkeypatch):
    attach(host)
    session = prepare(host, tmp_path)
    held = []
    real = host.journal._fsync

    def spy(fd):
        held.append(host.broker._lock.locked())
        real(fd)

    monkeypatch.setattr(host.journal, "_fsync", spy)
    observed = b2b.b2b_observe(session)
    b2b.b2b_revoke(session, "revoke-1", **triple(observed), target={"kind": "all_own_authority"})
    public(host, "/api/holds", {"request_id": "page-hold", "resources": [FILTER]})  # the page syncs too
    assert held == [False, False]
    monkeypatch.setattr(host.journal, "sync", lambda: "failed")
    observed = b2b.b2b_observe(session)
    late = b2b.b2b_revoke(session, "revoke-2", **triple(observed), target={"kind": "all_own_authority"})
    assert late["warnings"][-1].startswith("journal_sync_failed")
    monkeypatch.setattr(host.page, "_sync", lambda: "failed")  # the page holds its own handle on sync
    held_late = public(host, "/api/holds", {"request_id": "page-hold-2", "resources": [GAIN]})
    assert held_late["warnings"][-1].startswith("journal_sync_failed")  # the page says so too
    monkeypatch.setattr(host.page, "_sync", lambda: "full")
    full = public(host, "/api/holds", {"request_id": "page-hold-3", "resources": [GAIN]})
    assert full["warnings"][-1].startswith("journal_full")


def test_stop_ends_agent_authority_before_the_scheduler_stops(host, monkeypatch):
    seen = []
    shutdown = host.broker.shutdown

    def spy(reason):
        seen.append(host._stop.is_set())  # the scheduler is still running here
        return shutdown(reason)

    monkeypatch.setattr(host.broker, "shutdown", spy)
    host.stop()
    assert seen == [False] and host._stop.is_set()



def test_a_stopping_host_still_answers_reads_but_refuses_changes(host, tmp_path):
    attach(host)
    session = prepare(host, tmp_path)
    observed = b2b.b2b_observe(session)
    b2b.b2b_revoke(session, "revoke-1", **triple(observed), target={"kind": "all_own_authority"})
    host._closing.set()  # the first thing stop() does
    status = b2b.b2b_request_status(session, "revoke-1")
    assert (status["source"], status["host_running"], status["state"]) == ("host", True, "complete")
    with pytest.raises(PocketError) as caught:
        b2b.b2b_revoke(session, "revoke-2", **triple(observed), target={"kind": "all_own_authority"})
    assert caught.value.code == "io_error" and "stopping" in str(caught.value)


class _RefusingHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.rfile.readline()
        self.wfile.write(b'{"status":"error","error":"broken","code":"invalid_request","hint":null}\n')


def test_request_status_reads_the_journal_when_the_host_cannot_answer(tmp_path, socket_parent):
    path = str(Path(socket_parent) / "refusing.sock")
    try:
        server = socketserver.ThreadingUnixStreamServer(path, _RefusingHandler)
    except OSError as error:
        pytest.skip(f"Sockets are not available here: {error}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session = _fake_session(tmp_path, path)
        runtime = Path(json.loads((Path(session) / "b2b.json").read_text())["runtime_dir"])
        (runtime / "journal.ndjson").write_bytes(b"")
        status = b2b.b2b_request_status(session, "act-1")
        assert (status["source"], status["state"], status["host_error"]) == ("retained_journal", "not_found",
                                                                             "invalid_request")
    finally:
        server.shutdown()
        server.server_close()


def test_prepare_leaves_no_folder_when_the_host_cannot_be_reached(tmp_path, socket_parent):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "connection.json").write_text(json.dumps({
        "schema": "pocket.b2b-connection/v1", "socket": str(Path(socket_parent) / "none.sock"),
        "nonce": "n" * 48, "host_epoch": "hgone"}))
    with pytest.raises(PocketError) as caught:
        b2b.b2b_prepare(str(runtime), str(tmp_path / "session"))
    assert caught.value.code == "io_error" and not (tmp_path / "session").exists()


def test_a_prepare_whose_folder_cannot_be_made_binds_no_session(host, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    with pytest.raises(OSError):
        b2b.b2b_prepare(str(host.runtime_dir), str(blocker / "session"))  # the folder comes first
    assert prepare(host, tmp_path)  # so the host holds no session yet, and a valid prepare works


def test_the_page_snapshot_carries_the_session_settings_and_their_defaults(host, tmp_path):
    ui_epoch = attach(host)
    b2b.b2b_prepare(str(host.runtime_dir), str(tmp_path / "session"), settings={"uncertainty_ms": 0})
    state = issuer(host, "GET", f"/api/state?ui_epoch={ui_epoch}")
    assert state["settings"]["uncertainty_ms"] == 0 and state["settings_defaults"]["uncertainty_ms"] == 2000


def test_request_status_prefers_the_journal_when_the_host_no_longer_holds_a_request(host, tmp_path):
    attach(host)
    session = prepare(host, tmp_path)
    observed = b2b.b2b_observe(session)
    b2b.b2b_revoke(session, "revoke-1", **triple(observed), target={"kind": "all_own_authority"})
    del host.broker._requests["agent"]["revoke-1"]  # as if the host had forgotten it
    host.broker._receipts.pop(("agent", "revoke-1"))
    status = b2b.b2b_request_status(session, "revoke-1")
    assert (status["source"], status["state"], status["host_running"]) == ("retained_journal", "complete",
                                                                            True)


def test_the_page_pad_sends_its_own_hold_time_and_a_slow_request_stays_a_tap(host, tmp_path):
    session = prepare(host, tmp_path)
    attach(host)
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 127]})
    tick(host, 600)  # the release request arrived late; the page saw an 80 ms tap
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 0], "held_ms": 80})
    assert b2b.b2b_observe(session)["invitations"] == []
    # A Stop or Hold the host saw before this press: the page says so on the release.
    tick(host, 100)  # past the debounce, so this press counts
    issuer(host, "POST", "/api/synthetic/input", {"message": [0x97, 64, 127]})
    tick(host, 700)
    issuer(host, "POST", "/api/synthetic/input",
           {"message": [0x97, 64, 0], "held_ms": 700, "cancelled": True})
    assert b2b.b2b_observe(session)["invitations"] == []
    events = b2b.b2b_events(session, max_events=256)["events"]
    released = [e for e in events if e["type"] == "gesture_released"]
    assert released[-1]["outcome"] == "voided"  # cancelled, not a bounce or a lost release
    for bad in ({"held_ms": -1}, {"cancelled": "yes"}):
        status, refused, _ = http_call(host, "POST", "/api/synthetic/input",
                                       {"message": [0x97, 64, 0], **bad}, key=host.page.issuer_token)
        assert status == 400 and next(iter(bad)) in json.dumps(refused)


def test_fault_reports_are_queued_then_printed_and_a_closed_terminal_stops_nothing(host, monkeypatch):
    shown = io.StringIO()
    monkeypatch.setattr("sys.stderr", shown)
    host._report_fault(RuntimeError("a host bug"))  # under the broker lock: queued, not printed
    assert host.faulted.is_set() and shown.getvalue() == ""
    host._print_faults()
    assert "agent control stopped" in shown.getvalue() and "a host bug" in shown.getvalue()
    shown.close()  # a closed terminal
    host._report_fault(RuntimeError("another"))
    host._print_faults()  # returns quietly; the scheduler would keep running


def test_a_scheduler_failure_still_stops_agent_control_on_a_closed_terminal(host, tmp_path, monkeypatch):
    session = prepare(host, tmp_path)
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stderr", closed)
    monkeypatch.setattr(host, "tick", lambda: 1 / 0)
    host._schedule()  # returns after failing closed, as the scheduler thread would
    assert host.failed.is_set()
    events = b2b.b2b_events(session, max_events=256)["events"]
    assert any(e["type"] == "panic" for e in events)  # the panic ran though nothing could be printed


def test_an_existing_output_dir_gets_the_hint_for_what_happened(tmp_path, socket_parent):
    first = _host(tmp_path, socket_parent, name="run-1")
    try:
        session = prepare(first, tmp_path)
        with pytest.raises(PocketError) as caught:  # this host's finished prepare: reuse it
            b2b.b2b_prepare(str(first.runtime_dir), session)
        assert caught.value.code == "io_error" and "use this folder as session_dir" in caught.value.hint
        empty = tmp_path / "interrupted"
        empty.mkdir()
        with pytest.raises(PocketError) as caught:
            b2b.b2b_prepare(str(first.runtime_dir), str(empty))
        assert "Remove it and call b2b_prepare again" in caught.value.hint
    finally:
        first.stop()
    second = _host(tmp_path, socket_parent, name="run-2")
    try:
        with pytest.raises(PocketError) as caught:  # a restarted host never reuses the old session
            b2b.b2b_prepare(str(second.runtime_dir), session)
        assert "Pass a new output_dir" in caught.value.hint
    finally:
        second.stop()


def test_a_request_cut_short_is_refused_and_dispatches_nothing(host, tmp_path):
    frame = json.dumps({"op": "register", "nonce": host.nonce,
                        "request": {"output_dir_resolved": str(tmp_path / "never")}}).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.connect(str(host.socket_path))
        stream.sendall(frame)  # the whole JSON, but no closing newline
        stream.shutdown(socket.SHUT_WR)
        answer = json.loads(stream.makefile("rb").readline())
    assert (answer["status"], answer["code"]) == ("error", "invalid_request")
    assert prepare(host, tmp_path)  # so the host holds no session yet


class _SendFails:
    """A socket whose send fails after connecting, as a dropped connection mid-request would."""

    def __init__(self, *_args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def settimeout(self, _seconds):
        pass

    def connect(self, _path):
        pass

    def sendall(self, _payload):
        raise BrokenPipeError("the host hung up after the request went out")


def test_a_send_that_fails_counts_as_possibly_delivered(monkeypatch):
    monkeypatch.setattr(b2b.socket, "socket", _SendFails)
    connection = {"socket": "/unused", "nonce": "n" * 48}
    with pytest.raises(PocketError) as caught:
        b2b._send(connection, "claim", {"request": {}}, mutation=True)
    assert caught.value.code == "request_not_complete"  # never "nothing was recorded"
    assert "b2b_request_status" in caught.value.hint


def test_a_host_that_cannot_start_leaves_nothing_and_can_be_retried(tmp_path, socket_parent):
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
    except OSError as error:
        busy.close()
        pytest.skip(f"Sockets are not available here: {error}")
    port = busy.getsockname()[1]
    runtime = tmp_path / "runtime"
    try:
        with pytest.raises(OSError):
            B2bHost(runtime, clock=TestClock(START_NS), port=port, scheduler=False,
                    socket_parent=socket_parent)
        assert not runtime.exists() and list(Path(socket_parent).iterdir()) == []
        out, err = io.StringIO(), io.StringIO()
        assert main(["--runtime-dir", str(runtime), "--port", str(port), "--allow-piped-stdout"],
                    open_tty=_no_terminal, stdout=out, stderr=err, wait=lambda host: None) == 2
        refused = json.loads(err.getvalue().splitlines()[-1])
        assert refused["code"] == "io_error" and "could not start" in refused["error"]
        assert not runtime.exists()
    finally:
        busy.close()
    host = _host(tmp_path, socket_parent)  # the same --runtime-dir works once the port is free
    host.stop()
