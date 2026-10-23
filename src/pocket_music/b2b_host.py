# SPDX-License-Identifier: AGPL-3.0-only
"""A b2b host the person starts in their own terminal: ``python -m pocket_music.b2b_host``.

One foreground process owns the broker, the synthetic adapter, the journal, a scheduler thread,
the agent's Unix socket and the trusted loopback page. Authority lives exactly as long as this
process: a new host means a new host epoch, a new runtime dir and no grants or permissions.
Nothing is loaded from an earlier journal; only ``b2b_request_status`` reads one.

The page address carries the right to issue standing permissions, so the host writes it to the
terminal (``/dev/tty``) and never to a file. Without a terminal it refuses to start unless
``--allow-piped-stdout`` says that whoever reads stdout may hold that right. This is a speed
bump, not a guarantee. The only adapter is the built-in synthetic two-deck mixer: no Serato,
controller, MIDI or audio device is opened.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shutil
import signal
import socketserver
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from .b2b_adapters import SyntheticAdapter, validate_profile
from .b2b_broker import Broker
from .b2b_clock import SystemClock
from .b2b_journal import DEFAULT_MAX_BYTES, MAX_MAX_BYTES, MIN_MAX_BYTES, Journal
from .b2b_ui import TrustedPage, make_page_server, page_link, with_sync_warning
from .errors import PocketError

CONNECTION_SCHEMA = "pocket.b2b-connection/v1"
MAX_RPC_BYTES = 256 * 1024
MAX_SOCKET_PATH = 100  # bytes; AF_UNIX paths are limited to about 104 on macOS and 108 on Linux
_AGENT_OPS = frozenset({"register", "observe", "claim", "act", "release", "revoke", "authorize", "events",
                        "request_status"})
_SHAPES = {"register": {"request"}, "observe": {"session_id"}, "request_status": {"session_id", "request_id"}}
_READS = frozenset({"observe", "events", "request_status"})
_NEED_TERMINAL = "Start the b2b host from your own terminal; it prints a private page address there."
_PIPED_WARNING = ("This page address carries the right to issue standing permissions. Whoever reads this "
                  "output holds it.")
_NONCE_HINT = ("Read connection.json from the runtime_dir of the host you started; each host has its own "
               "nonce.")
_OPS_HINT = ("The agent socket takes register, observe, authorize, claim, act, release, revoke, events and "
             "request_status only; notices, invitations and permissions belong to the host page.")
_STOPPING_HINT = ("This b2b host is stopping. Start a new host and prepare a new session; nothing carries "
                  "over.")


def _socket_dir(parent=None, fallback: str = "/tmp") -> Path:
    """A private directory short enough for an AF_UNIX path, under TMPDIR or else ``fallback``."""
    for base in (tempfile.gettempdir() if parent is None else str(parent), fallback):
        try:
            made = Path(tempfile.mkdtemp(prefix="pocket-b2b-", dir=base))
        except OSError:
            continue
        if len(os.fsencode(made / "agent.sock")) <= MAX_SOCKET_PATH:
            return made
        made.rmdir()
    raise PocketError("No directory is short enough for the b2b agent socket", code="io_error",
                      hint="Set TMPDIR to a short directory and start the host again.")


def _write_private(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


class _AgentServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class _AgentHandler(socketserver.StreamRequestHandler):
    """One newline-delimited JSON request per connection, answered with one line."""

    def handle(self):
        self.request.settimeout(30)
        message = None
        try:
            raw = self.rfile.readline(MAX_RPC_BYTES + 1)
            if len(raw) > MAX_RPC_BYTES:
                raise PocketError("This b2b request is larger than 256 KiB", code="invalid_request")
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise PocketError("A b2b request is one line of JSON", code="invalid_request") from error
            response = {"status": "ok", "result": self.server.host.dispatch(message)}
        except PocketError as error:
            response = {"status": "error", "error": str(error), "code": error.code, "hint": error.hint}
        except Exception as error:  # noqa: BLE001 - a host bug is reported to the agent and the terminal
            traceback.print_exc(file=sys.stderr)
            read = isinstance(message, dict) and message.get("op") in _READS
            response = {"status": "error", "error": f"The b2b host failed: {type(error).__name__}",
                        "code": "invalid_request" if read else "request_not_complete",
                        "hint": ("The b2b host failed while reading; nothing changed. See the host terminal."
                                 if read else "The b2b host may have recorded this request. Call "
                                 "b2b_request_status with this request_id before anything else.")}
        self.wfile.write(json.dumps(response, allow_nan=False, separators=(",", ":")).encode() + b"\n")


class B2bHost:
    """The broker, adapter, journal, agent socket, page server and scheduler of one host process."""

    def __init__(self, runtime_dir, *, adapter: str = "synthetic", clock=None, port: int = 0,
                 journal_max_bytes: int = DEFAULT_MAX_BYTES, scheduler: bool = True, socket_parent=None):
        if adapter != "synthetic":
            raise PocketError("This version's only b2b adapter is synthetic", code="unsupported_profile",
                              hint="Run the host with --adapter synthetic; Serato control is a later, gated "
                                   "step.")
        if type(journal_max_bytes) is not int or not MIN_MAX_BYTES <= journal_max_bytes <= MAX_MAX_BYTES:
            raise PocketError("--journal-max-bytes must be 1 MiB to 1 GiB", code="invalid_arguments",
                              hint="Pass a whole number of bytes from 1048576 to 1073741824.")
        self.clock = SystemClock() if clock is None else clock  # refuses platforms other than macOS and Linux
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise PocketError("The runtime dir already exists", code="io_error",
                              hint="Pass a new --runtime-dir; a host never reuses one, so a restart never "
                                   "inherits authority.") from error
        os.chmod(self.runtime_dir, 0o700)
        self.host_epoch = "h" + secrets.token_hex(8)
        self.nonce = secrets.token_hex(24)
        self._scheduler = scheduler
        self._stop = threading.Event()
        self._closing = threading.Event()
        self.failed = threading.Event()  # the scheduler failed: the host exits
        self.faulted = threading.Event()  # the broker failed closed: the host keeps serving the person
        self._threads = []
        self.socket_dir = _socket_dir(socket_parent)
        try:
            self.adapter = SyntheticAdapter()
            profile = validate_profile(self.adapter.profile)
            self.journal = Journal(self.runtime_dir / "journal.ndjson", clock=self.clock,
                                   host_epoch=self.host_epoch, max_bytes=journal_max_bytes)
            self.journal.append("host_started", {
                "clocks": list(getattr(self.clock, "names", ())), "profile": profile.name,
                "mapping_sha256": profile.sha256, "pid": os.getpid(), "adapter": "synthetic"},
                session_epoch=None)
            self.broker = Broker(self.adapter, self.clock, self.journal, host_epoch=self.host_epoch,
                                 on_fault=self._report_fault)
            self.page = TrustedPage(self.broker, self.broker.trusted_channel(), adapter=self.adapter,
                                    sync=self.journal.sync)
            host_secrets = (self.page.issuer_token, self.page.public_csrf, self.nonce)
            self.journal.forbid(*host_secrets)
            self.broker.forbid_inputs(*host_secrets)
            self.socket_path = self.socket_dir / "agent.sock"
            self._agent_server = _AgentServer(str(self.socket_path), _AgentHandler)
            self._agent_server.host = self
            os.chmod(self.socket_path, 0o600)
            self._page_server = make_page_server(self.page, port)
            _write_private(self.runtime_dir / "connection.json", {
                "schema": CONNECTION_SCHEMA, "socket": str(self.socket_path), "nonce": self.nonce,
                "host_epoch": self.host_epoch, "pid": os.getpid(), "adapter_profile": profile.name,
                "mapping_sha256": profile.sha256})
        except BaseException:
            shutil.rmtree(self.socket_dir, ignore_errors=True)
            raise

    @property
    def page_link(self) -> str:
        return page_link(self._page_server)

    @property
    def page_origin(self) -> str:
        return self._page_server.origin

    def start(self) -> None:
        targets = [lambda: self._agent_server.serve_forever(poll_interval=0.05),
                   lambda: self._page_server.serve_forever(poll_interval=0.05)]
        if self._scheduler:
            targets.append(self._schedule)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _report_fault(self, fault) -> None:
        """A host fault the broker failed closed on. Agent control has already stopped for good."""
        self.faulted.set()
        print("b2b host fault: agent control stopped for the rest of this host; your controls still work. "
              "Stop this host and start a new one to play with the agent again.", file=sys.stderr, flush=True)
        traceback.print_exception(fault, file=sys.stderr)

    def tick(self) -> None:
        """One scheduler pass: deliver adapter events, then the broker's due work (tests call it)."""
        self.adapter.pump(self.clock.now_ns())
        self.broker.tick()

    def _schedule(self) -> None:
        # stop.wait is the host's only wall-clock wait; the broker itself never sleeps.
        try:
            while not self._stop.wait(0.01):
                self.tick()
        except Exception:  # noqa: BLE001 - fail closed: agent control stops and the host exits
            traceback.print_exc(file=sys.stderr)
            print("b2b host: the scheduler failed, so agent control stopped and this host is exiting. Your "
                  "controls work as before.", file=sys.stderr, flush=True)
            try:
                # A fresh id: a fixed one could be posted first and turn this into a harmless replay.
                self.broker.panic({"request_id": f"host-scheduler-failed-{secrets.token_hex(12)}"})
            except Exception:  # noqa: BLE001 - the exit below still ends every grant with the process
                traceback.print_exc(file=sys.stderr)
            finally:
                self.failed.set()

    def stop(self) -> None:
        """End agent authority and pending moves, sync the journal, close both servers."""
        if self._closing.is_set():
            return
        self._closing.set()  # agent mutations are refused from here; reads still answer
        try:
            self.broker.shutdown("host_stopping")  # from here no step is admitted, by a tick or a page ack
        except Exception:  # noqa: BLE001 - keep closing; the process exit ends authority anyway
            traceback.print_exc(file=sys.stderr)
        finally:
            self._stop.set()
            for thread in self._threads[2:]:  # the scheduler
                thread.join(timeout=5)
            self.journal.sync()
            if self._threads:  # shutdown() waits for serve_forever, so only a started server can take it
                self._page_server.shutdown()
                self._agent_server.shutdown()
            for thread in self._threads[:2]:
                thread.join(timeout=5)
            self._agent_server.server_close()
            self._page_server.server_close()
            self.adapter.close()
            self.journal.close()
            shutil.rmtree(self.socket_dir, ignore_errors=True)

    def dispatch(self, message) -> dict:
        """The closed agent operation list. Nothing here can ack, invite, hold, issue or inject."""
        if not isinstance(message, dict):
            raise PocketError("A b2b request is one JSON object", code="invalid_request")
        nonce = message.get("nonce")
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce.encode(), self.nonce.encode()):
            raise PocketError("This request does not carry this host's nonce", code="invalid_request",
                              hint=_NONCE_HINT)
        op = message.get("op")
        if op not in _AGENT_OPS:
            raise PocketError("The b2b agent socket has no such operation", code="invalid_request",
                              hint=_OPS_HINT)
        if self._closing.is_set() and op not in _READS:
            raise PocketError("This b2b host is stopping", code="io_error", hint=_STOPPING_HINT)
        if set(message) - {"op", "nonce"} != _SHAPES.get(op, {"session_id", "request"}):
            raise PocketError("This b2b request has the wrong fields for its operation",
                              code="invalid_request")
        session_id, request = message.get("session_id"), message.get("request")
        if op == "register":
            return self._synced(self.broker.register(request))
        if op == "observe":
            return self.broker.observe(session_id)
        if op == "events":
            return self.broker.events(session_id, request)
        if op == "request_status":
            return self.broker.request_status(session_id, message["request_id"])
        if op == "act":
            return self._synced(self._act(session_id, request))
        return self._synced(getattr(self.broker, op)(session_id, request))

    def _synced(self, result: dict) -> dict:
        # fsync outside the broker lock, before the response leaves the host.
        return with_sync_warning(result, self.journal.sync())

    def _act(self, session_id, request) -> dict:
        """Plan the act, then wait (lock released) until it is terminal, within a real-time bound."""
        run = self.broker.act_begin(session_id, request)
        settings = self.broker.settings
        bound_s = (settings["notice_ack_timeout_ms"] + settings["max_action_ms"] + settings["ack_timeout_ms"]
                   + 1000) / 1000
        deadline = time.monotonic() + bound_s
        result = self.broker.act_result(run)
        while result is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.broker.cancel_run(run, "host_wait_exceeded")
                result = self.broker.act_result(run)
                if result is None:
                    raise PocketError("The act did not finish", code="request_not_complete",
                                      hint="Call b2b_request_status with this request_id first.")
                break
            result = self.broker.act_wait(run, min(remaining, 1.0))
        return result


def _open_tty():
    return open("/dev/tty", "w")


def main(argv=None, *, open_tty=_open_tty, stdout=None, stderr=None, wait=None) -> int:
    """Run the host until Ctrl-C or SIGTERM. ``open_tty``, the streams and ``wait`` are test seams."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    parser = argparse.ArgumentParser(
        prog="python -m pocket_music.b2b_host",
        description="Run a local b2b host with the built-in synthetic two-deck mixer. Ctrl-C stops it.")
    parser.add_argument("--runtime-dir", required=True,
                        help="New private folder for the journal and connection")
    parser.add_argument("--adapter", choices=("synthetic",), default="synthetic",
                        help="The synthetic two-deck mixer; no Serato, controller, MIDI or audio device")
    parser.add_argument("--port", type=int, default=0, help="Loopback page port; 0 chooses a free port")
    parser.add_argument("--journal-max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="Journal budget, 1 MiB to 1 GiB (default 32 MiB)")
    parser.add_argument("--allow-piped-stdout", action="store_true",
                        help="Without a terminal, print the private page address on stdout")
    args = parser.parse_args(argv)
    try:
        tty = open_tty()
    except OSError:
        tty = None
    if tty is None and not args.allow_piped_stdout:
        print(_NEED_TERMINAL, file=stderr, flush=True)
        return 2
    try:
        host = B2bHost(args.runtime_dir, adapter=args.adapter, port=args.port,
                       journal_max_bytes=args.journal_max_bytes)
    except PocketError as error:
        if tty is not None:
            tty.close()
        print(json.dumps({"status": "error", "error": str(error), "code": error.code, "hint": error.hint}),
              file=stderr, flush=True)
        return 2
    host.start()
    status = {"status": "started", "runtime_dir": str(host.runtime_dir), "host_epoch": host.host_epoch,
              "adapter_profile": host.broker.profile.name}
    if tty is not None:
        with tty:
            tty.write(f"Open the b2b page: {host.page_link}\n"
                      "Keep it visible while the agent works. Ctrl-C stops this host.\n")
        status["page_link"] = "printed_to_terminal"
    else:
        print(_PIPED_WARNING, file=stderr, flush=True)
        status["page"] = host.page_link
    print(json.dumps(status), file=stdout, flush=True)
    stopping = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        # Handle SIGINT explicitly too: a host started in the background inherits it as ignored.
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stopping.set())
    try:
        if wait is not None:
            wait(host)
        else:
            while not stopping.wait(0.5) and not host.failed.is_set():
                pass
    except KeyboardInterrupt:
        pass
    finally:
        host.stop()
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 1 if host.failed.is_set() or host.faulted.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
