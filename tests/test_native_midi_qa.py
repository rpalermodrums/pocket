"""Independent native-adapter QA against fake LiveAPI and actual local transports.

No test controls Live, claims native acceptance, or supplies private music.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'src/pocket_music/devices/native_midi/core.js'
DEVICE = ROOT / 'src/pocket_music/devices/native_midi/device.js'
NODE = shutil.which('node')

FAKE = r'''
const assert = require('node:assert/strict');
const core = require(process.argv[1]);
const events = [];
const clipFields = {name:'Clip',is_midi_clip:1,looping:0,start_time:0,end_time:8,
  loop_start:0,loop_end:8,start_marker:0,end_marker:8,has_envelopes:0,has_groove:0,muted:0,
  is_arrangement_clip:1,is_session_clip:0,is_take_lane_clip:0,is_overdubbing:0,is_playing:0,
  is_recording:0,is_triggered:0};
const note = {note_id:1,pitch:60,start_time:0,duration:0.5,velocity:73,mute:0,
  probability:1,velocity_deviation:0,release_velocity:37};
const targetPath = 'live_set tracks 1 arrangement_clips 0';
const entries = {
  'live_set':{id:1,props:{tempo:120,signature_numerator:4,signature_denominator:4,is_playing:0,
    record_mode:0,arrangement_overdub:0,session_record:0,session_automation_record:0,file_path:'/synthetic/fixture.als'},counts:{tracks:2,return_tracks:2}},
  'live_app':{id:1000,props:{},counts:{}},
  'live_set tracks 0':{id:4,props:{name:'Protected audio'},counts:{devices:0,arrangement_clips:1}},
  'live_set tracks 0 arrangement_clips 0':{id:5,props:{...clipFields,name:'Source',is_midi_clip:0},counts:{}},
  'live_set tracks 1':{id:2,props:{name:'Owned MIDI',is_frozen:0,is_grouped:0,is_foldable:0,mute:0,solo:0,arm:0},counts:{devices:2,arrangement_clips:1}},
  'live_set tracks 1 devices 0':{id:6,props:{name:'Operator fixture',class_name:'Operator'},counts:{}},
  'live_set tracks 1 devices 1':{id:7,props:{name:'Native MIDI',class_name:'MxDeviceAudioEffect'},counts:{}},
  'live_set tracks 1 arrangement_clips 0':{id:3,props:{...clipFields},counts:{}},
  'live_set return_tracks 0':{id:8,props:{name:'A-Reverb'},counts:{devices:1,arrangement_clips:0}},
  'live_set return_tracks 0 devices 0':{id:9,props:{name:'Reverb',class_name:'Reverb'},counts:{}},
  'live_set return_tracks 1':{id:10,props:{name:'B-Delay'},counts:{devices:1,arrangement_clips:0}},
  'live_set return_tracks 1 devices 0':{id:11,props:{name:'Delay',class_name:'Delay'},counts:{}}
};
const state = {rows:[note],duringNotes:()=>{}};
function factory(path) {
  const key = path === 'this_device canonical_parent' ? 'live_set tracks 1' : path;
  const entry = entries[key];
  assert.ok(entry, 'Unexpected native path '+path);
  return {get id(){return entry.id},get:(name)=>{assert.ok(Object.hasOwn(entry.props,name),name);return [entry.props[name]]},
    getcount:(name)=>{assert.ok(Object.hasOwn(entry.counts,name),name);return entry.counts[name]},
    call:(method)=>{events.push({path,method});
      if(path==='live_app'&&method==='get_version_string')return '12.4.5';
      assert.equal(path,targetPath);assert.equal(method,'get_all_notes_extended');
      state.duringNotes();return JSON.stringify({notes:state.rows});
    }};
}
const target = {location:'arrangement',track_index:1,clip_index:0};
'''


def js(case):
    if NODE is None:
        pytest.skip('Node is required for fake Max adapter QA')
    result = subprocess.run([NODE, '-e', FAKE + '\n' + case, str(CORE)], capture_output=True,
                            text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_qa_exact_reader_projects_ordered_topology_notes_and_no_write_method():
    js('''
const result = core.read(factory,target);
assert.deepEqual(result.notes,[note]);
assert.deepEqual(result.topology.tracks.map(t=>t.runtime_id),[4,2]);
assert.deepEqual(result.topology.return_tracks.map(t=>t.runtime_id),[8,10]);
assert.equal(result.target.track_runtime_id,2);assert.equal(result.target.clip_runtime_id,3);
assert.equal(result.target.device_track_runtime_id,2);
assert.equal(result.host.file_path,'/synthetic/fixture.als');
assert.equal(result.guards.clip.has_envelopes.status,'available');
assert.equal(result.guards.clip.has_envelopes.value,0);
assert.ok(events.every(e=>['get_all_notes_extended','get_version_string'].includes(e.method)));
''')


@pytest.mark.parametrize('mutation', [
    "entries[targetPath].props.start_time = 1;",
    "entries['live_set tracks 1'].props.name = 'Replacement track';",
    "entries['live_set return_tracks 0 devices 0'].props.class_name = 'Another device';",
])
def test_qa_observation_detects_topology_or_target_change_during_note_read(mutation):
    js("state.duringNotes=()=>{" + mutation + "};\nassert.throws(()=>core.read(factory,target));")


def test_qa_unsupported_subnormal_flags_are_not_coerced_to_false():
    js('''
entries[targetPath].props.is_take_lane_clip = 5e-324;
entries[targetPath].props.is_session_clip = 5e-324;
const result = core.read(factory,target);
for(const name of ['is_take_lane_clip','is_session_clip']) {
  assert.equal(result.guards.clip[name].status,'unsupported');
  assert.equal(Object.hasOwn(result.guards.clip[name],'value'),false);
}
''')


@pytest.mark.parametrize('mutation', [
    "state.rows[0].unknown_expression = 1;",
    "state.rows = [note,{...note}];",
    "state.rows[0].pitch = true;",
    "state.rows[0].release_velocity = -1;",
    "state.rows = Array.from({length:2001},(_,i)=>({...note,note_id:i+1}));",
    "entries['live_set tracks 1'].counts.arrangement_clips = 257;",
    "entries['live_set tracks 1 devices 0'].id = 4;",
])
def test_qa_malformed_or_exceeded_native_read_never_becomes_empty_success(mutation):
    js(mutation + '\nassert.throws(()=>core.read(factory,target));')


def test_qa_device_loading_and_stale_session_never_call_liveapi():
    if NODE is None:
        pytest.skip('Node is required for fake Max adapter QA')
    program = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert=require('node:assert/strict');
const calls=[];
const context={outlet:()=>{},include:()=>{},LiveAPI:function(){calls.push('native')},
  Dict:function(){this.name='fake';this.parse=()=>{};this.freepeer=()=>{}},NativeMidiCore:{read:()=>{calls.push('read');return {}}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert.deepEqual(calls,[]);
context.bang();context.probe();context.bind_session('a'.repeat(32),'b'.repeat(64));
assert.deepEqual(calls,[]);
context.observe('c'.repeat(64),'d'.repeat(32),1,0);
assert.deepEqual(calls,[]);
'''
    result = subprocess.run([NODE, '-e', program, str(DEVICE)], capture_output=True, text=True,
                            timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def local_reader_bridge(tmp_path):
    """Run actual bridge.js with a fake Max peer; never contact a native host."""
    import hashlib
    import os
    import time

    if NODE is None:
        pytest.skip('Node is required for local bridge QA')
    package = tmp_path / 'package'
    package.mkdir()
    names = ['Native MIDI.maxpat', 'bridge.js', 'core.js', 'device.js']
    for name in names:
        shutil.copyfile(CORE.parent / name, package / name)
    sources = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in names}
    package_hash = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (package / 'manifest.json').write_text(json.dumps({'schema': 'pocket.native-midi-package/v1',
        'protocol': 'pocket.native-midi-wire/v1', 'sources': sources, 'package_sha256': package_hash}))
    stub = package / 'node_modules/max-api'
    stub.mkdir(parents=True)
    stub_code = FAKE.replace("require(process.argv[1])", "require('../../core.js')") + r'''
if(process.env.QA_MODE==='many_notes')state.rows=Array.from({length:20},(_,i)=>({...note,note_id:i+1,start_time:i/4,pitch:60+i%12}));
const handlers={}; const dictionaries={}; const current={nonce:null,packageHash:null};
const api={addHandler:(name,handler)=>{handlers[name]=handler},post:()=>{},
  getDict:async name=>dictionaries[name],
  outlet:(method,...args)=>{
    if(method==='probe')setImmediate(()=>handlers.ready());
    else if(method==='bind_session'){
      current.nonce=args[0];current.packageHash=args[1];setImmediate(()=>handlers.bound(args[0]));
    } else if(method==='observe') {
      const [key,nonce,trackIndex,clipIndex]=args;
      const complete=()=>{
        const raw={schema:'pocket.native-midi-observation/v1',request_key:key,session_nonce:nonce,
          package_sha256:current.packageHash,protocol:'pocket.native-midi-wire/v1',
          read_started_at:new Date().toISOString(),read_ended_at:new Date().toISOString(),status:'ok',
          observation:core.read(factory,{location:'arrangement',track_index:trackIndex,clip_index:clipIndex})};
        if(process.env.QA_MODE==='wrong_nonce')raw.session_nonce='0'.repeat(32);
        const payload=JSON.stringify(raw);const chunks=[];
        for(let offset=0;offset<payload.length;offset+=4096)chunks.push(payload.slice(offset,offset+4096));
        dictionaries[key]={schema:'pocket.native-midi-wire/v1',request_key:key,session_nonce:nonce,json_chunks:chunks};
        handlers.result(key,key);
      };
      if(process.env.QA_MODE==='delayed')setTimeout(complete,350);
      else setImmediate(complete);
    } else if(method==='release')delete dictionaries[args[0]];
    else throw Error('Unexpected bridge→Max operation '+method);
  }};
module.exports=api;
'''
    (stub / 'index.js').write_text(stub_code)
    processes = []

    def start(mode='normal'):
        directory = tmp_path / ('bridge-' + str(len(processes)))
        proc = subprocess.Popen([NODE, str(package / 'bridge.js')],
            env={**os.environ, 'POCKET_NATIVE_MIDI_DIR': str(directory), 'QA_MODE': mode},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        deadline = time.monotonic() + 5
        while not list(directory.glob('connection-*.json')):
            assert proc.poll() is None, proc.communicate(timeout=1)
            assert time.monotonic() < deadline
            time.sleep(.01)
        descriptor_path = next(directory.glob('connection-*.json'))
        return directory, descriptor_path, json.loads(descriptor_path.read_bytes())

    yield start
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=5)


def http_request(descriptor, route, body=None, *, auth=True, origin=None, extra=None):
    import http.client

    connection = http.client.HTTPConnection('127.0.0.1', descriptor['port'], timeout=3)
    headers = {'Content-Type': 'application/json'}
    if auth:
        headers['Authorization'] = 'Bearer ' + descriptor['token']
    if origin is not None:
        headers['Origin'] = origin
    if extra:
        headers.update(extra)
    try:
        connection.request('GET' if body is None else 'POST', route,
                           json.dumps(body, separators=(',', ':')) if body is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def read_request(descriptor, key='a', timeout=1000):
    return {'schema': 'pocket.native-midi-read-request/v1', 'request_key': key * 64,
            'session_nonce': descriptor['session_nonce'],
            'target': {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, 'timeout_ms': timeout}


def test_qa_actual_bridge_auth_whitelist_and_strict_request_shape(local_reader_bridge):
    directory, descriptor_path, descriptor = local_reader_bridge()
    assert descriptor_path.stat().st_mode & 0o777 == 0o600
    status, health = http_request(descriptor, '/health')
    assert status == 200 and health['ready'] is True and health['write_available'] is False
    assert http_request(descriptor, '/health', auth=False)[0] == 403
    assert http_request(descriptor, '/health', origin='https://example.test')[0] == 403
    assert http_request(descriptor, '/write', {'method': 'add_new_notes'})[0] == 404
    for invalid in ({**read_request(descriptor), 'method': 'add_new_notes'},
                    {**read_request(descriptor), 'timeout_ms': True},
                    {**read_request(descriptor), 'target': {'location': 'arrangement', 'track_index': True, 'clip_index': 0}},
                    {**read_request(descriptor), 'session_nonce': '0' * 32}):
        assert http_request(descriptor, '/read', invalid)[0] == 400
    assert not list((directory / 'journals').glob('*/*.json'))


def test_qa_actual_bridge_completed_replay_never_dispatches_again(local_reader_bridge):
    directory, _, descriptor = local_reader_bridge()
    request = read_request(descriptor)
    status, response = http_request(descriptor, '/read', request)
    assert status == 200 and response['journal']['state'] == 'complete'
    assert response['journal']['dispatch_count'] == 1 and response['replay'] is False
    second_status, second = http_request(descriptor, '/read', request)
    assert second_status == 200 and second['replay'] is True and second['journal'] == response['journal']
    assert descriptor['token'] not in json.dumps(response)
    journal_path = directory / 'journals' / descriptor['session_nonce'] / ('a' * 64 + '.json')
    assert json.loads(journal_path.read_bytes()) == response['journal']
    assert http_request(descriptor, '/read', {**request, 'target': {**request['target'], 'clip_index': 1}})[0] == 409


def test_qa_actual_bridge_timeout_holds_slot_and_retains_late_result(local_reader_bridge):
    import time

    directory, _, descriptor = local_reader_bridge('delayed')
    request = read_request(descriptor, timeout=100)
    assert http_request(descriptor, '/read', request)[0] == 504
    assert http_request(descriptor, '/read', read_request(descriptor, key='b'))[0] == 409
    journal_path = directory / 'journals' / descriptor['session_nonce'] / ('a' * 64 + '.json')
    deadline = time.monotonic() + 3
    while json.loads(journal_path.read_bytes())['state'] != 'complete':
        assert time.monotonic() < deadline
        time.sleep(.01)
    status, late = http_request(descriptor, '/read', request)
    assert status == 200 and late['replay'] is True and late['journal']['dispatch_count'] == 1


def test_qa_actual_bridge_wrong_session_result_never_completes_or_releases_slot(local_reader_bridge):
    directory, _, descriptor = local_reader_bridge('wrong_nonce')
    assert http_request(descriptor, '/read', read_request(descriptor))[0] == 502
    assert http_request(descriptor, '/read', read_request(descriptor, key='b'))[0] == 409
    journal_path = directory / 'journals' / descriptor['session_nonce'] / ('a' * 64 + '.json')
    journal = json.loads(journal_path.read_bytes())
    assert journal['state'] == 'dispatched' and journal['result_json'] is None


def test_qa_public_reader_and_status_use_real_transport_without_candidate_or_saved_binding(tmp_path, local_reader_bridge):
    from pocket_music import native_midi
    from pocket_music.artifact_store import read_record

    directory, _, descriptor = local_reader_bridge()
    store = str(tmp_path / 'public-artifacts')
    args = {'store_root': store, 'request_id': 'qa-public-read',
                'target': {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, 'bridge_dir': str(directory)}
    result = native_midi.native_midi_read(**args)
    assert result['status'] == 'ok'
    assert result['notes'] == [{'note_id': 1, 'pitch': 60, 'start_time': 0, 'duration': .5,
        'velocity': 73, 'release_velocity': 37, 'mute': 0, 'probability': 1, 'velocity_deviation': 0}]
    record = native_midi.load_native_midi_observation(result['artifacts']['observation'], store)
    assert record['saved_binding'] is None
    assert record['coverage'] == {'consistency': 'sequential_not_atomic', 'note_expression': 'unobserved', 'saved_equivalence': 'not_claimed'}
    assert native_midi.native_midi_read(**args) == result
    status = native_midi.native_midi_status(store, 'qa-public-status', result['native_request'], str(directory))
    assert status['journal_state'] == 'complete'
    assert status['coverage']['redispatched'] is False
    assert read_record(status['artifacts']['bridge_journal'], store)['dispatch_count'] == 1
    assert descriptor['token'] not in json.dumps(result) + json.dumps(record) + json.dumps(status)


def test_qa_public_reader_timeout_status_recovers_late_evidence_without_redispatch(tmp_path, local_reader_bridge):
    import time

    from pocket_music import native_midi

    directory, _, _ = local_reader_bridge('delayed')
    store = str(tmp_path / 'public-artifacts')
    args = {'store_root': store, 'request_id': 'qa-timed-read', 'timeout_seconds': .1,
                'target': {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, 'bridge_dir': str(directory)}
    result = native_midi.native_midi_read(**args)
    assert result['status'] == 'outcome_unknown'
    assert native_midi.native_midi_read(**args) == result
    request = result['native_request']
    journal = directory / 'journals' / request['session_nonce'] / (request['request_key'] + '.json')
    deadline = time.monotonic() + 3
    while json.loads(journal.read_bytes())['state'] != 'complete':
        assert time.monotonic() < deadline
        time.sleep(.01)
    recovered = native_midi.native_midi_status(store, 'qa-late-status', request, str(directory))
    assert recovered['journal_state'] == 'complete'
    assert json.loads(journal.read_bytes())['dispatch_count'] == 1
    assert native_midi.native_midi_read(**args) == result  # Immutable historical request outcome.


def test_qa_public_status_rejects_malformed_list_journal_as_domain_error(tmp_path):
    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    folder = tmp_path / 'offline-bridge'
    path = folder / 'journals' / ('a' * 32) / ('b' * 64 + '.json')
    path.parent.mkdir(parents=True)
    path.write_text('[]')
    with pytest.raises(PocketError):
        native_midi.native_midi_status(str(tmp_path / 'store'), 'qa-malformed-status',
            {'session_nonce': 'a' * 32, 'request_key': 'b' * 64}, str(folder))


def test_qa_public_build_replay_verifies_package_and_uses_independent_mutable_files(tmp_path):
    from pocket_music import native_midi
    from pocket_music.artifact_store import read_record
    from pocket_music.errors import PocketError

    output, store = tmp_path / 'device-package', tmp_path / 'store'
    args = {'output_dir': str(output), 'store_root': str(store), 'request_id': 'qa-build'}
    receipt = native_midi.build_native_midi_device(**args)
    package = read_record(receipt['artifacts']['device'], str(store))
    for name, handle in package['files'].items():
        assert (output / name).stat().st_ino != (store / handle['artifact_uri']).stat().st_ino
        assert (output / name).stat().st_nlink == 1
    assert receipt['coverage']['write_available'] is False
    assert native_midi.build_native_midi_device(**args) == receipt
    (output / 'core.js').write_text('// mutated published package')
    with pytest.raises(PocketError, match='changed'):
        native_midi.build_native_midi_device(**args)


def test_qa_public_reader_descriptor_symlink_refuses_without_dispatch(tmp_path, local_reader_bridge):
    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    directory, descriptor_path, _ = local_reader_bridge()
    outside = tmp_path / 'moved-descriptor.json'
    descriptor_path.rename(outside)
    descriptor_path.symlink_to(outside)
    with pytest.raises(PocketError, match='symlink'):
        native_midi.native_midi_read(str(tmp_path / 'store'), 'qa-symlink',
            {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, bridge_dir=str(directory))
    assert list((directory / 'journals').rglob('*.json')) == []


@pytest.mark.parametrize('mutation', ['boolean_target_index', 'float_target_index', 'boolean_raw_guard'])
def test_qa_retained_observation_requires_strict_native_types(tmp_path, local_reader_bridge, mutation):
    import hashlib

    from pocket_music import native_midi
    from pocket_music.artifact_store import canonical_bytes, put_record, read_record
    from pocket_music.errors import PocketError

    directory, _, _ = local_reader_bridge()
    store = str(tmp_path / 'store')
    result = native_midi.native_midi_read(store, 'qa-native-types',
        {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, bridge_dir=str(directory))
    record = read_record(result['artifacts']['observation'], store)
    journal = read_record(record['bridge_journal'], store)
    response = json.loads(journal['result_json'])
    core = response['observation']
    if mutation in {'boolean_target_index', 'float_target_index'}:
        value = True if mutation == 'boolean_target_index' else 1.0
        core['target']['track_index'] = value
        record['target']['track_index'] = value
    else:
        core['guards']['clip']['is_midi_clip']['raw'] = [True]
        record['guards']['clip']['is_midi_clip']['raw'] = [True]
        guards = read_record(record['raw_guards'], store)
        guards['guards'] = record['guards']
        record['raw_guards'] = put_record(guards, store)
    journal['result_json'] = canonical_bytes(response).decode()
    journal['result_sha256'] = hashlib.sha256(journal['result_json'].encode()).hexdigest()
    record['bridge_journal'] = put_record(journal, store)
    forged = put_record(record, store)
    with pytest.raises(PocketError):
        native_midi.load_native_midi_observation(forged, store)


def test_qa_retained_native_pagination_is_complete_bounded_and_offline(tmp_path, local_reader_bridge, monkeypatch):
    from pocket_music import native_midi
    from pocket_music.artifact_store import canonical_bytes

    directory, _, _ = local_reader_bridge('many_notes')
    store = str(tmp_path / 'store')
    first = native_midi.native_midi_read(store, 'qa-page-zero',
        {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, bridge_dir=str(directory),
        limit=3, byte_budget=2048)
    expected = native_midi.load_native_midi_observation(first['artifacts']['observation'], store)['notes']
    assert len(expected) == 20
    monkeypatch.setattr(native_midi, '_connection', lambda *a, **kw: pytest.fail('Stored pages contacted native discovery'))
    shutil.rmtree(directory)
    result = first
    gathered = []
    pages = 0
    while True:
        assert len(canonical_bytes(result)) <= 2048
        gathered.extend(result['notes'])
        if result['next_page'] is None:
            break
        assert result['next_page']['offset'] > result['offset']
        pages += 1
        assert pages <= 20
        result = native_midi.native_midi_read(store, 'qa-page-' + str(pages), **result['next_page'])
        assert result['artifacts']['observation'] == first['artifacts']['observation']
    assert gathered == expected


@pytest.mark.parametrize('extra', [{'target': {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}},
                                  {'bridge_dir': '/should/not/read'}, {'timeout_seconds': 1}])
def test_qa_stored_native_pages_reject_mixed_live_inputs(tmp_path, local_reader_bridge, extra):
    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    directory, _, _ = local_reader_bridge()
    store = str(tmp_path / 'store')
    result = native_midi.native_midi_read(store, 'qa-mixed-base',
        {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, bridge_dir=str(directory))
    with pytest.raises(PocketError, match='Stored observation'):
        native_midi.native_midi_read(store, 'qa-mixed-refuse', observation=result['artifacts']['observation'], **extra)
