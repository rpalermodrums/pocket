# SPDX-License-Identifier: AGPL-3.0-only
"""Independent discovery families and optional-codec boundary checks."""
from __future__ import annotations

import builtins

from test_audio_hypothesis_jobs_qa import fixture, inspect
from test_capability_families_qa import catalog
from test_midi_expression_qa import encoding, lifecycle, receiver
from test_midi_qa import import_wire, smf, vlq

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import read_record
from pocket_music.midi_expression import midi_expression_plan
from pocket_music.midi_lifecycle import midi_lifecycle


def test_qa_discovery_reports_direct_worker_source_and_terminal_conflict_result(tmp_path, monkeypatch):
    args, submitted, invocation = fixture(tmp_path, monkeypatch)
    jobs._run_worker(**invocation)
    status = inspect(args, submitted)
    conflict = jobs.job_cancel(args['store_root'], submitted['job']['job_id'], status['revision'])
    assert conflict['status'] == 'conflict' and conflict['state'] == 'completed'
    rows = catalog()
    for name, result in [('audio_hypothesis_submit', submitted), ('job_status', status), ('job_cancel', conflict)]:
        actual = {value['artifact_schema'] for value in result.values()
                  if isinstance(value, dict) and value.get('schema') == 'pocket.artifact-handle/v1'}
        assert actual <= set(rows[name]['returned_artifact_schemas'])


def test_qa_imported_lifecycle_and_expression_plan_do_not_require_optional_mido(tmp_path, monkeypatch):
    wire = smf([b'\0\x90\x3c\x50' + vlq(480) + b'\x80\x3c\x25\0\xff\x2f\0'])
    handle = import_wire(tmp_path, wire)
    store = str(tmp_path / 'store')
    record = read_record(handle, store)
    real_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == 'mido' or name.startswith('mido.'):
            raise AssertionError('File-independent analysis attempted optional MIDI codec import')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    life = midi_lifecycle(material=handle, store_root=store, request_id='life', **lifecycle(record, horizon=1))
    result = midi_expression_plan(material=handle, lifecycle=life['report'], receiver_assumption=receiver(),
                                  encoding=encoding(), store_root=store, request_id='expression')
    assert result['coverage']['native_verified'] is False
    rows = catalog()
    assert rows['midi_lifecycle']['status'] == rows['midi_expression_plan']['status'] == 'available'


def test_qa_discovery_optional_midi_absence_does_not_disable_expression_planner(monkeypatch):
    import importlib.util
    original = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name, *args, **kwargs:
                        None if name == 'mido' else original(name, *args, **kwargs))
    rows = catalog()
    assert rows['midi_export']['status'] == 'unsupported'
    assert rows['midi_lifecycle']['status'] == rows['midi_expression_plan']['status'] == 'available'
    assert rows['material_import']['conditional_dependencies'] == ['Optional midi extra for SMF sources']
