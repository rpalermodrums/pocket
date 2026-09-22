"""Adapter owner checks: actual loopback transport and independent saved XML binding."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest

from pocket_music.artifact_store import canonical_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.native_midi import (
    GUARDS,
    NUMERIC_GUARDS,
    _core,
    build_native_midi_device,
    load_native_midi_observation,
    native_midi_read,
    native_midi_status,
)

NODE = shutil.which('node')
TARGET = {'location': 'arrangement', 'track_index': 0, 'clip_index': 0}


def fixture_core(saved_path=''):
    note = {'note_id': 1, 'pitch': 60, 'start_time': 0, 'duration': 0.5, 'velocity': 73,
            'mute': 0, 'probability': 1, 'velocity_deviation': 0, 'release_velocity': 37}
    clip = {'index': 0, 'runtime_id': 3, 'name': 'Probe', 'is_midi_clip': 1,
            'start_time': 0, 'end_time': 8, 'looping': 0, 'loop_start': 0,
            'loop_end': 8, 'start_marker': 0, 'end_marker': 8}
    guards = {scope: {name: {'status': 'available', 'value': 0, 'raw': [0]}
                     for name in names} for scope, names in GUARDS.items()}
    for properties in guards.values():
        for name, guard in properties.items():
            value = saved_path if name == 'file_path' else clip.get(name, 120 if name == 'tempo' else 4 if name in NUMERIC_GUARDS else 0)
            guard.update(value=value, raw=[value])
    guards['clip']['is_session_clip'] = {'status': 'unsupported', 'raw_type': 'number', 'raw_text': '5e-324'}
    return {'host': {'runtime_version': '12.4.5', 'file_path': saved_path},
            'topology': {'song': {'runtime_id': 1}, 'tracks': [
                {'index': 0, 'runtime_id': 2, 'name': 'Owned', 'devices': [
                    {'index': 0, 'runtime_id': 4, 'name': 'Operator', 'class_name': 'Operator'}],
                 'arrangement_clips': [clip]}], 'return_tracks': []},
            'target': {**TARGET, 'track_runtime_id': 2, 'clip_runtime_id': 3,
                       'path': 'live_set tracks 0 arrangement_clips 0', 'device_track_runtime_id': 2},
            'notes': [note], 'guards': guards, 'raw_notes_json': json.dumps({'notes': [note]})}


FAKE_MAX = r"""
const fs = require('fs');
const handlers = {};
const dictionaries = {};
const core = JSON.parse(fs.readFileSync(process.env.TEST_CORE,'utf8'));
let nonce, packageHash;
module.exports = {
  addHandler: (name, fn) => { handlers[name] = fn; },
  post: () => {},
  getDict: name => Promise.resolve(dictionaries[name]),
  outlet: (name, ...args) => {
    if (name === 'probe') { setImmediate(() => handlers.ready()); }
    if (name === 'bind_session') {
      [nonce, packageHash] = args;
      dictionaries['pocket_write_input_' + nonce] = {json_chunks: []};
      setImmediate(() => handlers.bound(nonce));
    }
    if (name === 'observe') {
      fs.appendFileSync(process.env.TEST_LOG, JSON.stringify([name,...args])+'\n');
      const result = {schema:'pocket.native-midi-observation/v1',request_key:args[0],session_nonce:nonce,
        package_sha256:packageHash,protocol:'pocket.native-midi-wire/v1',read_started_at:new Date().toISOString(),
        read_ended_at:new Date().toISOString(),status:'ok',observation:core};
      const serialized = JSON.stringify(result);
      const chunks = [];
      for (let i=0; i<serialized.length; i+=4096) chunks.push(serialized.slice(i,i+4096));
      dictionaries.fixture = {schema:'pocket.native-midi-wire/v1',request_key:args[0],session_nonce:nonce,json_chunks:chunks};
      setTimeout(() => handlers.result(args[0], 'fixture'), Number(process.env.TEST_DELAY || 5));
    }
  }
};
"""


@pytest.fixture
def bridge(tmp_path):
    if NODE is None:
        pytest.skip('Node.js is required for loopback bridge tests')
    package = tmp_path / 'package'
    store = tmp_path / 'store'
    built = build_native_midi_device(str(package), str(store), 'build')
    module = package / 'node_modules' / 'max-api'
    module.mkdir(parents=True)
    (module / 'index.js').write_text(FAKE_MAX)
    core_path = tmp_path / 'core.json'
    folder = tmp_path / 'bridge'
    log = tmp_path / 'dispatches'
    processes = []

    def start(core=None, delay=5):
        core_path.write_text(json.dumps(core or fixture_core()))
        process = subprocess.Popen([NODE, str(package / 'bridge.js')], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env={**os.environ, 'POCKET_NATIVE_MIDI_DIR': str(folder),
                                                               'TEST_CORE': str(core_path), 'TEST_LOG': str(log),
                                                               'TEST_DELAY': str(delay)})
        processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            paths = list(folder.glob('connection-*.json'))
            if paths:
                return {'store': str(store), 'folder': str(folder), 'log': log, 'package': package,
                        'descriptor': json.loads(paths[-1].read_text()), 'built': built}
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            time.sleep(0.02)
        pytest.fail('No native bridge descriptor')
    yield start
    for process in processes:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_native_read_actual_transport_and_historical_replay(bridge):
    live = bridge()
    args = {'store_root': live['store'], 'request_id': 'read', 'target': TARGET, 'bridge_dir': live['folder']}
    first = native_midi_read(**args)
    assert first['status'] == 'ok' and first['total_notes'] == 1
    assert first['notes'][0]['release_velocity'] == 37
    record = load_native_midi_observation(first['artifacts']['observation'], live['store'])
    assert record['saved_binding'] is None
    assert record['guards']['clip']['is_session_clip']['status'] == 'unsupported'
    assert record['coverage']['saved_equivalence'] == 'not_claimed'
    assert native_midi_read(**args) == first
    assert len(live['log'].read_text().splitlines()) == 1
    status = native_midi_status(live['store'], 'status', first['native_request'], live['folder'])
    assert status['journal_state'] == 'complete'
    assert status['coverage']['redispatched'] is False


def test_timeout_retains_late_result_without_redispatch(bridge):
    live = bridge(delay=300)
    result = native_midi_read(live['store'], 'timeout', TARGET, bridge_dir=live['folder'], timeout_seconds=0.1)
    assert result['status'] == 'outcome_unknown'
    time.sleep(0.35)
    status = native_midi_status(live['store'], 'late', result['native_request'], live['folder'])
    assert status['journal_state'] == 'complete'
    assert len(live['log'].read_text().splitlines()) == 1


def test_reader_requires_neither_daw_nor_candidate_for_unavailable_result(tmp_path):
    result = native_midi_read(str(tmp_path / 'store'), 'absent', TARGET, bridge_dir=str(tmp_path / 'absent'))
    assert result['status'] == 'unsupported'
    assert result['coverage']['native_dispatched'] is False


def test_missing_journal_status_does_not_need_live(tmp_path):
    result = native_midi_status(str(tmp_path / 'store'), 'status', {'session_nonce': 'a' * 32, 'request_key': 'b' * 64}, str(tmp_path / 'bridge'))
    assert result['journal_state'] == 'not_found'


def test_saved_primary_binding(bridge, tmp_path):
    saved = tmp_path / 'fixture.als'
    saved.write_bytes(gzip.compress(b'''<Ableton Creator="Live test"><LiveSet><Tracks><MidiTrack Id="9"><Name><EffectiveName Value="Owned"/></Name><DeviceChain><DeviceChain><Devices><Operator Id="0"/></Devices></DeviceChain><MainSequencer><ClipTimeable><ArrangerAutomation><Events><MidiClip Id="7"><Name Value="Probe"/><CurrentStart Value="0"/><CurrentEnd Value="8"/></MidiClip></Events></ArrangerAutomation></ClipTimeable></MainSequencer></DeviceChain></MidiTrack></Tracks></LiveSet></Ableton>'''))
    live = bridge(fixture_core(str(saved)))
    binding = {'saved_als': str(saved), 'expected_sha256': hashlib.sha256(saved.read_bytes()).hexdigest(), 'track_id': '9', 'clip_id': '7'}
    result = native_midi_read(live['store'], 'bound', TARGET, saved_binding=binding, bridge_dir=live['folder'])
    record = read_record(result['artifacts']['observation'], live['store'])
    assert record['saved_binding']['saved_als']['sha256'] == binding['expected_sha256']
    with pytest.raises(PocketError, match='identity mismatch'):
        native_midi_read(live['store'], 'wrong-clip', TARGET, saved_binding={**binding, 'clip_id': '8'}, bridge_dir=live['folder'])


def test_output_tamper_on_build_replay(tmp_path):
    package, store = tmp_path / 'package', str(tmp_path / 'store')
    build_native_midi_device(str(package), store, 'build')
    (package / 'core.js').write_text('tampered')
    with pytest.raises(PocketError, match='changed after build'):
        build_native_midi_device(str(package), store, 'build')


@pytest.mark.parametrize('change', [
    lambda v: v['notes'][0].update(velocity=True),
    lambda v: v['notes'][0].update(release_velocity=float('nan')),
    lambda v: v['notes'][0].update(note_id=1.0),
    lambda v: v['topology']['tracks'][0].update(runtime_id=1),
    lambda v: v['target'].update(device_track_runtime_id=999),
    lambda v: v['guards']['clip']['looping'].update(value=1, raw=[1]),
    lambda v: v['notes'][0].update(extra=1),
    lambda v: v['notes'][0].update(duration=0),
])
def test_strict_observation_validation(change):
    core = copy.deepcopy(fixture_core())
    change(core)
    with pytest.raises(PocketError):
        _core(core, TARGET)


@pytest.mark.parametrize('target', [dict(TARGET, track_index=True), dict(TARGET, clip_index=-1),
                                     dict(TARGET, location='session'), dict(TARGET, extra=1)])
def test_invalid_target_never_dispatches(target, tmp_path):
    with pytest.raises(PocketError):
        native_midi_read(str(tmp_path), 'bad', target)


def test_byte_budget_is_full_receipt_bound(bridge):
    core = fixture_core()
    core['notes'] = [{**core['notes'][0], 'note_id': i + 1, 'start_time': i / 4} for i in range(100)]
    core['raw_notes_json'] = json.dumps({'notes': core['notes']})
    live = bridge(core)
    result = native_midi_read(live['store'], 'budget', TARGET, bridge_dir=live['folder'], byte_budget=2048)
    assert len(canonical_bytes(result)) <= 2048
    assert 0 < len(result['notes']) < 64
    assert result['next_offset'] == len(result['notes'])


def test_stored_pages_never_contact_bridge_and_preserve_identity(bridge, monkeypatch):
    import pocket_music.native_midi as provider
    core = fixture_core()
    core['notes'] = [{**core['notes'][0], 'note_id': i + 1} for i in range(3)]
    core['raw_notes_json'] = json.dumps({'notes': core['notes']})
    live = bridge(core)
    first = native_midi_read(live['store'], 'first', TARGET, bridge_dir=live['folder'], limit=2)
    monkeypatch.setattr(provider, '_connection', lambda *_: pytest.fail('Stored page must not discover Live'))
    second = native_midi_read(live['store'], 'second', **first['next_page'])
    assert second['notes'] == [core['notes'][2]]
    assert second['next_offset'] is None
    assert second['artifacts'] == first['artifacts']
    assert len(live['log'].read_text().splitlines()) == 1
    for extra in ({'target': TARGET}, {'bridge_dir': live['folder']}, {'timeout_seconds': 1},
                  {'expected_session_nonce': 'a' * 32}):
        with pytest.raises(PocketError, match='Stored observation mode'):
            native_midi_read(live['store'], 'mixed', observation=first['artifacts']['observation'], **extra)
    with pytest.raises(PocketError, match='Further pages'):
        native_midi_read(live['store'], 'fresh-offset', TARGET, offset=1)


FAKE_WRITER_MAX = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
require('os').homedir = () => process.env.TEST_HOST_ROOT;
const handlers = {}, dictionaries = new Map();
let sequence = 1;
const core = JSON.parse(fs.readFileSync(process.env.TEST_CORE,'utf8'));
const writer = require('../../writer.js');
function Dict(name) {
 this.name=name||'temporary-'+sequence++;
 this.parse=raw=>dictionaries.set(this.name,JSON.parse(raw));
 this.stringify=()=>JSON.stringify(dictionaries.get(this.name));this.freepeer=()=>{};
}
const context = {include:()=>{},NativeMidiWriter:writer,Dict,
 NativeMidiCore:{read:()=>JSON.parse(JSON.stringify(core))},
 LiveAPI:function(callback,path){return {call:(method,dictionary)=>{
   const notes=JSON.parse(dictionary.stringify()).notes;
   fs.appendFileSync(process.env.TEST_LOG,JSON.stringify({method,path,notes})+'\n');
   if (method==='add_new_notes') core.notes=notes.map((note,i)=>({...note,note_id:101+i}));
   else if (method==='apply_note_modifications') core.notes=core.notes.map(note=>notes.find(other=>other.note_id===note.note_id)||note);
   else throw Error('unqualified method');
   core.raw_notes_json=JSON.stringify({notes:core.notes});
   if(process.env.TEST_FAILURE==='call')throw Error('native result lost');
   return method==='add_new_notes'?core.notes.map(note=>note.note_id):[];
 }}},outlet:(port,method,key,name)=>{
   if(method==='ready')setImmediate(()=>handlers.ready());
   if(method==='bound')setImmediate(()=>handlers.bound(key));
   if(method==='result')setImmediate(()=>handlers.result(key,name));
 }};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../../device.js'),'utf8'),context);context.bang();
module.exports={addHandler:(name,handler)=>handlers[name]=handler,post:()=>{},
 getDict:async name=>dictionaries.get(name),setDict:async(name,value)=>{if(!dictionaries.has(name))throw Error('Max Dict must exist before setDict');dictionaries.set(name,value);},
 outlet:(method,...args)=>setImmediate(()=>context[method](...args))};
"""


@pytest.fixture
def public_writer_fixture(tmp_path, monkeypatch):

    from test_candidate_runtime_identity import runtime_fixture, writer_scope
    from test_thread import value

    import pocket_music.native_midi as provider

    if NODE is None:
        pytest.skip('Node required for public writer transport tests')
    package = tmp_path / 'writer-package'
    store = str(tmp_path / 'store')
    built = provider.build_native_midi_writer(str(package), store, 'build-writer')
    module = package / 'node_modules/max-api'
    module.mkdir(parents=True)
    (module / 'index.js').write_text(FAKE_WRITER_MAX)
    host_root = tmp_path / 'synthetic-host'
    host_root.mkdir()
    lease = host_root / '.pocket/native-midi-writer/host-lease.json'
    monkeypatch.setattr(provider, '_host_lease_path', lambda: lease)
    folder = tmp_path / 'writer-bridge'
    log = tmp_path / 'calls.jsonl'
    log.touch()
    processes = []
    failure = {'value': ''}

    def start(core):
        core['guards']['clip']['is_arrangement_clip'] = {'status': 'available', 'value': 1, 'raw': [1]}
        core_path = tmp_path / 'writer-core.json'
        core_path.write_text(json.dumps(core))
        process = subprocess.Popen([NODE, str(package / 'bridge.js')], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, 'POCKET_NATIVE_MIDI_WRITER_DIR': str(folder),
                                        'TEST_CORE': str(core_path), 'TEST_HOST_ROOT': str(host_root),
                                        'TEST_LOG': str(log), 'TEST_FAILURE': failure['value']})
        processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if list(folder.glob('connection-*.json')):
                return {'folder': str(folder)}
            if process.poll() is not None:
                pytest.fail(process.communicate()[1].decode())
            time.sleep(0.02)
        pytest.fail('No writer bridge')

    def prepare(fail=False):
        import xml.etree.ElementTree as ET
        failure['value'] = 'call' if fail else ''

        def metadata(root):
            clip = root.find('LiveSet/Tracks/MidiTrack').find('.//MidiClip')
            notes = clip.find('Notes')
            notes.clear()
            ET.SubElement(notes, 'KeyTracks')
            ET.SubElement(ET.SubElement(notes, 'PerNoteEventStore'), 'EventLists')
            ET.SubElement(notes, 'NoteProbabilityGroups')
            value(ET.SubElement(notes, 'ProbabilityGroupIdGenerator'), 'NextId', 1)
            value(ET.SubElement(notes, 'NoteIdGenerator'), 'NextId', 1)
            envelopes = clip.find('Envelopes')
            if envelopes is not None:
                clip.remove(envelopes)
            ET.SubElement(ET.SubElement(clip, 'Envelopes'), 'Envelopes')
            loop = clip.find('Loop')
            for name in ('LoopStart', 'StartRelative'):
                element = loop.find(name)
                if element is None:
                    value(loop, name, 0)
                else:
                    element.set('Value', '0')
        fixture = runtime_fixture(tmp_path, start, writer_package=package, empty_observed=True, observed_mutation=metadata)
        return {**fixture, 'scope': writer_scope(fixture), 'writer_device': built['artifacts']['device'], 'lease': lease, 'log': log}
    yield prepare
    for process in processes:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_public_writer_integrates_pending_guard_terminal_and_single_dispatch(public_writer_fixture):
    from pocket_music.native_midi import native_midi_write, validate_native_terminal

    fixture = public_writer_fixture()
    args = {**fixture['scope'], 'request_id': 'write-one', 'expected_observation': fixture['observation'],
            'writer_device': fixture['writer_device'], 'edit': {'kind': 'insert_empty', 'notes': [
                {'pitch': 60, 'onset_qn': {'n': 0, 'd': 1}, 'duration_qn': {'n': 1, 'd': 2}, 'velocity': 73, 'release_velocity': 37},
                {'pitch': 64, 'onset_qn': {'n': 1, 'd': 1}, 'duration_qn': {'n': 3, 'd': 4}, 'velocity': 81, 'release_velocity': 55},
                {'pitch': 67, 'onset_qn': {'n': 2, 'd': 1}, 'duration_qn': {'n': 1, 'd': 1}, 'velocity': 66, 'release_velocity': 18}]},
            'supervision': {'actor': 'Synthetic test', 'actor_kind': 'agent', 'observed_at': '2026-09-16T00:00:00Z',
                            'exclusive_native_session': True, 'no_ui_edits_after_native_read': True},
            'bridge_dir': fixture['live']['folder']}
    result = native_midi_write(**args)
    assert result['status'] == 'ok', result.get('reason', result)
    terminal = validate_native_terminal(result['artifacts']['terminal'], fixture['store'])
    assert terminal['dispatch_count'] == 1
    assert result['workspace']['state'] == 'awaiting_native'
    assert not fixture['lease'].exists()
    assert len(fixture['log'].read_text().splitlines()) == 1
    assert native_midi_write(**args) == result
    assert len(fixture['log'].read_text().splitlines()) == 1
    changed = native_midi_write(**{**args, 'request_id': 'velocity-one',
        'expected_revision': result['workspace']['revision'],
        'expected_observation': result['artifacts']['after_observation'],
        'edit': {'kind': 'set_velocity', 'note_id': 102, 'velocity': 89,
                 'insertion_terminal': result['artifacts']['terminal']}})
    assert changed['status'] == 'ok', changed.get('reason', changed)
    after = load_native_midi_observation(changed['artifacts']['after_observation'], fixture['store'])
    assert [note['velocity'] for note in after['notes']] == [73, 89, 66]
    assert [note['release_velocity'] for note in after['notes']] == [37, 55, 18]
    assert len(fixture['log'].read_text().splitlines()) == 2
    assert not fixture['lease'].exists()


def test_public_writer_lost_outcome_retains_both_quarantines(public_writer_fixture):
    from pocket_music.native_midi import native_midi_write

    fixture = public_writer_fixture(fail=True)
    result = native_midi_write(**fixture['scope'], request_id='lost', expected_observation=fixture['observation'],
        writer_device=fixture['writer_device'], edit={'kind': 'insert_empty', 'notes': [
            {'pitch': 60, 'onset_qn': {'n': 0, 'd': 1}, 'duration_qn': {'n': 1, 'd': 2}, 'velocity': 73, 'release_velocity': 37}]},
        supervision={'actor': 'Synthetic test', 'actor_kind': 'agent', 'observed_at': '2026-09-16T00:00:00Z',
                     'exclusive_native_session': True, 'no_ui_edits_after_native_read': True}, bridge_dir=fixture['live']['folder'])
    assert result['status'] == 'outcome_unknown', result
    state = json.loads((fixture['saved'].parent / 'workspace.json').read_text())
    assert state['state'] == 'outcome_unknown' and fixture['lease'].exists()
    assert len(fixture['log'].read_text().splitlines()) == 1


def test_python_lease_cleanup_serializes_and_cannot_delete_a_new_owner(tmp_path, monkeypatch):
    from pathlib import Path

    import pocket_music.native_midi as provider
    lease = tmp_path / 'host' / 'host-lease.json'
    lease.parent.mkdir()
    old = {'session_nonce': 'old', 'request_key': 'first'}
    lease.write_text(json.dumps(old))
    monkeypatch.setattr(provider, '_host_lease_path', lambda: lease)
    with provider._lease_release_lock(), pytest.raises(PocketError, match='busy'):
        provider._remove_matching_lease(old)
    assert json.loads(lease.read_text()) == old
    provider._remove_matching_lease(old)
    fresh = {'session_nonce': 'new', 'request_key': 'second'}
    lease.write_text(json.dumps(fresh))
    with pytest.raises(PocketError, match='another request'):
        provider._remove_matching_lease(old)
    assert json.loads(lease.read_text()) == fresh
    assert Path(lease.parent / 'lease-release.lock').is_file()
