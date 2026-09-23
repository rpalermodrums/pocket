"""Per-call verified-read snapshots: reuse inside one public call, never across calls or stores."""
import copy
import shutil
import threading
from pathlib import Path

import pytest
from test_musical_context import fixture

from pocket_music import artifact_store
from pocket_music.artifact_store import put_bytes, read_bytes, verification_scope
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_compare, practice_query, practice_render
from pocket_music.practice_feedback_query import practice_feedback_query
from pocket_music.practice_previews import practice_preview


def counted_reads(monkeypatch):
    calls = []
    original = Path.read_bytes

    def read(self):
        calls.append(self.name)
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", read)
    return calls


def test_identical_handles_reuse_one_verified_snapshot_within_a_call(tmp_path, monkeypatch):
    store = tmp_path / "store"
    handle = put_bytes(b"verified bytes", store, "blob.bin", "pocket.test-blob/v1")
    path = store / handle["artifact_uri"]
    calls = counted_reads(monkeypatch)
    with verification_scope():
        assert read_bytes(handle, store) == b"verified bytes"
        path.chmod(0o644)
        path.write_bytes(b"replaced bytes")
        # Proof inside the call keeps deriving from the bytes verified first.
        assert read_bytes(handle, store) == b"verified bytes"
        with verification_scope():  # nested scopes share the outer snapshot
            assert read_bytes(handle, store) == b"verified bytes"
    assert calls.count("blob.bin") == 1
    with pytest.raises(PocketError, match="integrity"):
        read_bytes(handle, store)  # the next call re-reads and re-verifies


def test_snapshots_are_keyed_by_store_and_never_leak_between_calls(tmp_path, monkeypatch):
    store = tmp_path / "store"
    handle = put_bytes(b"one", store, "blob.bin", "pocket.test-blob/v1")
    other = tmp_path / "other"
    shutil.copytree(store, other)
    moved = other / handle["artifact_uri"]
    moved.chmod(0o644)
    moved.write_bytes(b"two")
    with verification_scope():
        assert read_bytes(handle, store) == b"one"
        with pytest.raises(PocketError, match="integrity"):
            read_bytes(handle, other)
    calls = counted_reads(monkeypatch)
    read_bytes(handle, store)
    read_bytes(handle, store)
    assert calls.count("blob.bin") == 2  # outside a scope nothing is retained


def test_other_threads_do_not_share_a_callers_snapshot(tmp_path):
    store = tmp_path / "store"
    handle = put_bytes(b"shared?", store, "blob.bin", "pocket.test-blob/v1")
    path = store / handle["artifact_uri"]
    errors = []
    with verification_scope():
        read_bytes(handle, store)
        path.chmod(0o644)
        path.write_bytes(b"changed")

        def worker():
            try:
                read_bytes(handle, store)
            except PocketError as error:
                errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert errors and "integrity" in errors[0]


def test_payloads_above_the_snapshot_budget_are_reverified_each_time(tmp_path, monkeypatch):
    store = tmp_path / "store"
    handle = put_bytes(b"x" * 64, store, "blob.bin", "pocket.test-blob/v1")
    monkeypatch.setattr(artifact_store, "SNAPSHOT_BYTES", 32)
    calls = counted_reads(monkeypatch)
    with verification_scope():
        read_bytes(handle, store)
        read_bytes(handle, store)
    assert calls.count("blob.bin") == 2


def test_graph_bounds_still_apply_inside_a_scope(tmp_path):
    nested = []
    current = nested
    for _ in range(140):
        current.append([])
        current = current[0]
    with verification_scope(), pytest.raises(PocketError, match="verification bounds"):
        artifact_store._verify_handles(nested, tmp_path)


def outputs(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    raw = practice_render(store, "raw", context, ["first", "again"])["artifacts"]["render"]
    alt = practice_render(store, "alt", context, ["alternative"])["artifacts"]["render"]
    comparison = practice_compare(store, "cmp", raw, [alt], "Synthetic", True)["artifacts"]["comparison"]
    preview = practice_preview(store, "pv", alt, "browser-pcm16-original-rate/v1")["artifacts"]["preview"]
    from pocket_music.practice_audio import practice_feedback
    report = practice_feedback(store, "fb", comparison, alt, [0, 10], "Synthetic", "agent", "Fixture only", None,
                               preview=preview)["artifacts"]["feedback"]
    return store, [lambda: practice_query(store, comparison), lambda: practice_query(store, preview),
                   lambda: practice_query(store, report), lambda: practice_feedback_query(store, [report]),
                   lambda: practice_query(store, preview, section="mappings")]


def test_optimized_calls_return_identical_receipts(tmp_path, monkeypatch):
    _, calls = outputs(tmp_path)
    optimized = [call() for call in calls]
    monkeypatch.setattr(artifact_store, "SNAPSHOT_BYTES", 0)
    monkeypatch.setattr(artifact_store, "MEMO_ENTRIES", 0)
    assert [call() for call in calls] == optimized


def test_memoized_loads_hand_out_independent_copies(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    raw = practice_render(store, "raw", context, ["first", "again"])["artifacts"]["render"]
    from pocket_music.practice_audio import load_practice_render
    with verification_scope():
        first = load_practice_render(raw, store)
        pristine = copy.deepcopy(first)
        first["mappings"].clear()
        first["signal"]["frames"] = -1
        assert load_practice_render(raw, store) == pristine


def test_tamper_between_calls_is_detected_after_a_successful_call(tmp_path):
    store, context, _, _, _ = fixture(tmp_path)
    raw = practice_render(store, "raw", context, ["first", "again"])["artifacts"]["render"]
    preview = practice_preview(store, "pv", raw, "browser-pcm16-original-rate/v1")["artifacts"]["preview"]
    practice_query(store, preview)
    record = artifact_store.read_record(raw, store)
    audio = Path(store) / record["audio"]["artifact_uri"]
    audio.chmod(0o644)
    audio.write_bytes(audio.read_bytes()[:-8] + bytes(8))
    with pytest.raises(PocketError, match="integrity"):
        practice_query(store, preview)


def test_a_context_captured_inside_a_scope_cannot_reuse_it_later(tmp_path):
    import contextvars
    store = tmp_path / "store"
    handle = put_bytes(b"original", store, "blob.bin", "pocket.test-blob/v1")
    path = store / handle["artifact_uri"]
    with verification_scope():
        read_bytes(handle, store)
        captured = contextvars.copy_context()
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(PocketError, match="integrity"):
        captured.run(read_bytes, handle, store)
    errors = []

    def later():
        try:
            captured.run(read_bytes, handle, store)
        except PocketError as error:
            errors.append(str(error))
    worker = threading.Thread(target=later)
    worker.start()
    worker.join()
    assert errors and "integrity" in errors[0]


def test_a_thread_inheriting_the_callers_context_does_not_share_its_snapshot(tmp_path):
    import contextvars
    store = tmp_path / "store"
    handle = put_bytes(b"original", store, "blob.bin", "pocket.test-blob/v1")
    path = store / handle["artifact_uri"]
    errors = []
    with verification_scope():
        read_bytes(handle, store)
        path.chmod(0o644)
        path.write_bytes(b"tampered")
        context = contextvars.copy_context()  # what free-threaded builds pass to new threads

        def worker():
            try:
                context.run(read_bytes, handle, store)
            except PocketError as error:
                errors.append(str(error))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert errors and "integrity" in errors[0]


def test_one_store_identity_for_every_spelling(tmp_path, monkeypatch):
    store = tmp_path / "store"
    handle = put_bytes(b"shared", store, "blob.bin", "pocket.test-blob/v1")
    (tmp_path / "link").symlink_to(store)
    monkeypatch.chdir(tmp_path)
    calls = counted_reads(monkeypatch)
    with verification_scope():
        for spelling in (store, str(store) + "/", str(tmp_path / "link"), "store", "link"):
            assert read_bytes(handle, spelling) == b"shared"
    assert calls.count("blob.bin") == 1


@pytest.mark.parametrize("root", ["loop", "bad\x00root", "~no_such_user_for_pocket_tests/store"])
def test_memoized_validators_keep_their_original_error_for_malformed_handles(tmp_path, monkeypatch, root):
    from pocket_music.practice_audio import load_practice_render
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    monkeypatch.chdir(tmp_path)
    with verification_scope(), pytest.raises(PocketError, match="Expected pocket.artifact-handle/v1"):
        load_practice_render({"schema": "pocket.artifact-handle/v1"}, root)
