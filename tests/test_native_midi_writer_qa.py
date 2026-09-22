"""Independent writer adversaries; all native calls use synthetic LiveAPI only."""
import shutil
import subprocess
from pathlib import Path

import pytest
from test_native_midi_qa import FAKE

ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / 'src/pocket_music/devices/native_midi_writer/writer.js'
DEVICE = WRITER.with_name('device.js')
READER = WRITER.with_name('reader.js')
NODE = shutil.which('node')

BASE = FAKE + r'''
const writer=require(process.argv[2]);
state.rows=[];
const before=core.read(factory,target);
const inserted={pitch:60,start_time:0,duration:0.5,velocity:73,release_velocity:37};
const request={schema:'pocket.native-midi-write-request/v1',request_key:'a'.repeat(64),session_nonce:'b'.repeat(32),
  package_sha256:'c'.repeat(64),target,saved_als_path:'/synthetic/fixture.als',expected:before,
  edit:{kind:'insert_empty',notes:[inserted]}};
'''


def js(case, *, harness=BASE):
    if NODE is None:
        pytest.skip('Node is needed for synthetic native writer QA')
    run = subprocess.run([NODE, '-e', harness + '\n' + case, str(READER), str(WRITER), str(DEVICE)],
                         capture_output=True, text=True, timeout=20, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


def test_qa_writer_public_wire_maps_only_exact_whitelisted_note_payload():
    js('''
const plan=writer.plan(request,before);
assert.deepEqual(plan,{method:'add_new_notes',dictionary:{notes:[{...inserted,mute:0,probability:1,velocity_deviation:0}]}});
assert.deepEqual(request.expected.notes,[]);
assert.deepEqual(request.edit,{kind:'insert_empty',notes:[inserted]});
''')


@pytest.mark.parametrize('change', [
    "request.edit.notes[0].pitch=true;", "request.edit.notes[0].velocity='73';",
    "request.edit.notes[0].velocity=0;", "request.edit.notes[0].release_velocity=128;",
    "request.edit.notes[0].start_time=1/3;", "request.edit.notes[0].duration=2**-21;",
    "request.edit.notes[0].duration=0;", "request.edit.notes[0].start_time=-1;",
    "request.edit.notes[0].duration=9;", "request.edit.notes[0].expression=[];",
    "request.edit.kind='delete_all_notes';", "request.edit.method='remove_notes_extended';",
    "request.edit.notes.push({...inserted});", "request.edit.notes=Array(4).fill(inserted);",
])
def test_qa_writer_rejects_unqualified_wire_values_and_method_injection(change):
    js(change + '\nassert.throws(()=>writer.plan(request,before));')


@pytest.mark.parametrize('scope,name,value', [
    ('song', 'is_playing', 1), ('song', 'record_mode', 1), ('song', 'session_automation_record', 2),
    ('song', 'tempo', 121), ('song', 'signature_numerator', 3),
    ('track', 'arm', 1), ('clip', 'has_envelopes', 1), ('clip', 'has_groove', 1),
    ('clip', 'start_marker', 1), ('clip', 'is_arrangement_clip', 0),
])
def test_qa_writer_rejects_expected_but_unqualified_current_guard(scope, name, value):
    js(f"before.guards.{scope}.{name}.value={value};\nassert.throws(()=>writer.plan(request,before));")


@pytest.mark.parametrize('mutation', [
    "changed.notes=[{note_id:99,...inserted,mute:0,probability:1,velocity_deviation:0}];",
    "changed.topology.tracks[0].name='another source';",
    "changed.topology.return_tracks[0].devices[0].runtime_id=100;",
    "changed.guards.clip.is_take_lane_clip={status:'unsupported',error:'changed availability'};",
])
def test_qa_writer_expected_snapshot_is_complete_not_just_selected_note(mutation):
    js('const changed=JSON.parse(JSON.stringify(before));\n' + mutation + '\nassert.throws(()=>writer.plan(request,changed));')


DEVICE_HARNESS = BASE + r'''
const fs=require('node:fs'),vm=require('node:vm');
const data=new Map(), outputs=[], nativeCalls=[];
let nextDictionary=1, reads=0, snapshot=JSON.parse(JSON.stringify(before));
let alterDictionary=false, callFailure=false, lostAfter=false;
function Dictionary(name){this.name=name||('anonymous-'+nextDictionary++);
 this.parse=(raw)=>{const v=JSON.parse(raw);if(alterDictionary&&this.name.startsWith('anonymous-')&&v.notes)v.notes[0].release_velocity=0;data.set(this.name,v)};
 this.stringify=()=>JSON.stringify(data.get(this.name));this.freepeer=()=>{};
}
const context={include:()=>{},NativeMidiWriter:writer,Dict:Dictionary,
 NativeMidiCore:{read:()=>{reads++;if(lostAfter&&nativeCalls.length)throw Error('readback disconnected');return JSON.parse(JSON.stringify(snapshot))}},
 LiveAPI:function(callback,path){return{call:(method,dictionary)=>{
  const payload=JSON.parse(dictionary.stringify());nativeCalls.push({method,path,payload});
  if(callFailure)throw Error('native call result lost');
  if(method==='add_new_notes')snapshot.notes=payload.notes.map((note,i)=>({...note,note_id:i+101}));
  else if(method==='apply_note_modifications')snapshot.notes=snapshot.notes.map(note=>payload.notes.find(n=>n.note_id===note.note_id)||note);
  else throw Error('unqualified native method');return [];
 }}},outlet:(port,method,key,name)=>{if(method==='result')outputs.push(JSON.parse(data.get(name).json_chunks.join('')))}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);
context.bang();context.bind_session(request.session_nonce,request.package_sha256);
function prepare(){data.set('input',{json_chunks:[JSON.stringify(request)]});context.prepare_write(request.request_key,request.session_nonce,'input');return outputs.at(-1)}
function commit(){context.commit_write(request.request_key,request.session_nonce);return outputs.at(-1)}
'''


def test_qa_actual_max_writer_has_two_guards_and_exact_single_dispatch():
    js('''
assert.equal(reads,0);assert.equal(nativeCalls.length,0);
assert.equal(prepare().outcome,'ready');assert.equal(reads,1);assert.equal(nativeCalls.length,0);
const result=commit();assert.equal(result.outcome,'verified_readback');assert.equal(reads,3);
assert.equal(nativeCalls.length,1);assert.equal(nativeCalls[0].method,'add_new_notes');
assert.equal(nativeCalls[0].path,'live_set tracks 1 arrangement_clips 0');
assert.equal(result.after.notes[0].release_velocity,37);
commit();assert.equal(nativeCalls.length,1);
''', harness=DEVICE_HARNESS)


@pytest.mark.parametrize('change', [
    "snapshot.topology.tracks[0].name='changed source';",
    "snapshot.guards.song.is_playing.value=1;",
    "snapshot.notes=[{...inserted,note_id:8,mute:0,probability:1,velocity_deviation:0}];",
])
def test_qa_actual_max_writer_second_guard_refuses_between_prepare_and_commit(change):
    js("assert.equal(prepare().outcome,'ready');\n" + change + "\nconst result=commit();assert.equal(result.outcome,'refused_before_dispatch');assert.equal(nativeCalls.length,0);", harness=DEVICE_HARNESS)


def test_qa_actual_max_dictionary_roundtrip_loss_refuses_before_dispatch():
    js("assert.equal(prepare().outcome,'ready');alterDictionary=true;assert.equal(commit().outcome,'refused_before_dispatch');assert.equal(nativeCalls.length,0);", harness=DEVICE_HARNESS)


@pytest.mark.parametrize('failure', ['callFailure', 'lostAfter'])
def test_qa_actual_max_lost_native_outcome_never_becomes_cancelled_or_retryable(failure):
    js("assert.equal(prepare().outcome,'ready');" + failure + "=true;const result=commit();assert.equal(result.outcome,'outcome_unknown');assert.equal(result.dispatch_count,1);assert.equal(nativeCalls.length,1);commit();assert.equal(nativeCalls.length,1);", harness=DEVICE_HARNESS)


def test_qa_actual_max_cancel_prepared_state_performs_no_native_call():
    js("assert.equal(prepare().outcome,'ready');context.cancel_write(request.request_key);commit();assert.equal(nativeCalls.length,0);assert.equal(reads,1);", harness=DEVICE_HARNESS)


def test_qa_observed_session_automation_arming_is_preserved_without_transport_recording():
    # Actual read-only E1 evidence has this available flag1 while transport is
    # stopped. It is arming state, not proof that recording is in progress.
    js("before.guards.song.session_automation_record={status:'available',value:1,raw:[1]};assert.equal(writer.plan(request,before).method,'add_new_notes');")


@pytest.fixture
def writer_bridge(tmp_path):
    """Real bridge and Max device JS, independently fake native APIs/files only."""
    import hashlib
    import json
    import os
    import time

    if NODE is None:
        pytest.skip('Node is required for local writer bridge QA')
    package = tmp_path / 'writer-package'
    package.mkdir()
    names = ['Native MIDI Writer.maxpat', 'bridge.js', 'device.js', 'reader.js', 'writer.js']
    for name in names:
        shutil.copyfile(WRITER.parent / name, package / name)
    hashes = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in names}
    package_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (package / 'manifest.json').write_text(json.dumps({'schema': 'pocket.native-midi-package/v1',
        'protocol': 'pocket.native-midi-wire/v1', 'sources': hashes, 'package_sha256': package_hash}))
    stub = package / 'node_modules/max-api'
    stub.mkdir(parents=True)
    stub_code = FAKE.replace("require(process.argv[1])", "require('../../reader.js')") + r'''
const fs=require('node:fs'),vm=require('node:vm');
const trace=(event,fields={})=>fs.appendFileSync(process.env.QA_TRANSPORT_LOG,JSON.stringify({event,...fields})+'\n');
// Keep a future host-global lease inside this fixture process's synthetic home.
require('node:os').homedir=()=>process.env.QA_HOST_ROOT;
entries.live_set.props.file_path=process.env.QA_SAVED_ALS;state.rows=[];
if(process.env.QA_PUBLIC_FIXTURE==='1'){
 entries['live_set tracks 0'].props.name='Synthetic test track';
 Object.assign(entries['live_set tracks 0 arrangement_clips 0'].props,{name:'Fixture',end_time:16});
 entries['live_set return_tracks 0'].props.name='Reverb';entries['live_set return_tracks 1'].props.name='Delay';
 Object.assign(entries[targetPath].props,{start_time:4,end_time:8,loop_end:4,end_marker:4});
}
const handlers={},dictionaries=new Map(), calls=[];let dictionaryId=0;
function Dictionary(name){this.name=name||('anon-'+dictionaryId++);
 this.parse=raw=>{dictionaries.set(this.name,JSON.parse(raw));trace('dict_initialized',{name:this.name})};
 this.stringify=()=>JSON.stringify(dictionaries.get(this.name));this.freepeer=()=>{};
}
const context={include:()=>{},NativeMidiCore:core,NativeMidiWriter:require('../../writer.js'),Dict:Dictionary,
 LiveAPI:function(callback,path){const read=factory(path);return{...read,call:(method,arg)=>{
   if(method==='add_new_notes'||method==='apply_note_modifications'){
     const notes=JSON.parse(arg.stringify()).notes;calls.push({method,path,notes});
     fs.appendFileSync(process.env.QA_CALL_LOG,JSON.stringify(calls.at(-1))+'\n');
     if(method==='add_new_notes')state.rows=notes.map((n,i)=>({...n,note_id:101+i}));
     else state.rows=state.rows.map(n=>notes.find(other=>other.note_id===n.note_id)||n);
     if(process.env.QA_MODE==='call_failure')throw Error('native call failed after possible dispatch');
     return method==='add_new_notes'?state.rows.map(n=>n.note_id):[];
   }
   return read.call(method,arg);
 }}},outlet:(port,method,key,name)=>{
   if(method==='ready')setImmediate(()=>handlers.ready());
   else if(method==='bound'){trace('bound',{nonce:key});if(process.env.QA_MODE==='legacy_missing_dictionary')dictionaries.delete('pocket_write_input_'+key);if(process.env.QA_MODE==='legacy_nonempty_dictionary')dictionaries.set('pocket_write_input_'+key,{json_chunks:['stale']});setImmediate(()=>handlers.bound(key));}
   else if(method==='result'){
     const result=JSON.parse(dictionaries.get(name).json_chunks.join(''));
     if(process.env.QA_MODE==='lost_result'&&result.stage==='commit')return;
     setImmediate(()=>handlers.result(key,name));
   }
 }};
vm.createContext(context);vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../../device.js'),'utf8'),context);context.bang();
module.exports={addHandler:(name,handler)=>handlers[name]=handler,post:message=>trace('max_post',{message}),
 getDict:async name=>dictionaries.get(name),
 setDict:async(name,value)=>{if(process.env.QA_MODE==='missing_write_dictionary')dictionaries.delete(name);trace('set_dict',{name,exists:dictionaries.has(name)});if(!dictionaries.has(name))throw Error('Invalid response for setDict request. Please make sure the requested dict exists.');if(process.env.QA_MODE==='slow_dictionary')await new Promise(resolve=>setTimeout(resolve,150));dictionaries.set(name,value)},
 outlet:(method,...args)=>{
   const action=()=>{trace('max_message',{method});context[method](...args)};
   if((process.env.QA_MODE==='slow_prepare'&&method==='prepare_write')||(process.env.QA_MODE==='slow_commit'&&method==='commit_write'))setTimeout(action,350);
   else setImmediate(action);
 }};
'''
    (stub / 'index.js').write_text(stub_code)
    runs = []
    home = tmp_path / 'synthetic-home'
    home.mkdir()

    def start(mode='normal', *, saved_input=None):
        number = len(runs)
        if mode == 'custom_package':
            (package / 'writer.js').write_bytes((package / 'writer.js').read_bytes() + b'\n// externally modified package\n')
            custom_hashes = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in names}
            (package / 'manifest.json').write_text(json.dumps({'schema': 'pocket.native-midi-package/v1',
                'protocol': 'pocket.native-midi-wire/v1', 'sources': custom_hashes,
                'package_sha256': hashlib.sha256(json.dumps(custom_hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}))
        directory = tmp_path / f'writer-{number}'
        if saved_input is None:
            workspace = tmp_path / f'workspace-{number}'
            workspace.mkdir()
            saved = workspace / 'fixture.als'
            saved.write_bytes(b'Synthetic Node file-binding fixture; not an Ableton Set')
            statefile = workspace / 'workspace.json'
            statefile.write_text(json.dumps({'state': 'host_pending', 'workspace_id': f'workspace-{number}', 'revision': 2}))
        else:
            saved = saved_input
            statefile = saved.parent / 'workspace.json'
        log = tmp_path / f'native-calls-{number}.jsonl'
        log.touch()
        transport_log = tmp_path / f'transport-{number}.jsonl'
        transport_log.touch()
        process = subprocess.Popen([NODE, str(package / 'bridge.js')],
            env={**os.environ, 'POCKET_NATIVE_MIDI_WRITER_DIR': str(directory), 'QA_HOST_ROOT': str(home),
                 'QA_SAVED_ALS': str(saved), 'QA_CALL_LOG': str(log), 'QA_TRANSPORT_LOG': str(transport_log), 'QA_MODE': mode,
                 'QA_PUBLIC_FIXTURE': '1' if saved_input is not None else '0'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        runs.append(process)
        deadline = time.monotonic() + 5
        while not list(directory.glob('connection-*.json')):
            assert process.poll() is None, process.communicate(timeout=1)
            assert time.monotonic() < deadline
            if mode.startswith('legacy_') and 'Pocket writer not ready:' in transport_log.read_text():
                break
            time.sleep(.01)
        descriptors = list(directory.glob('connection-*.json'))
        descriptor = json.loads(descriptors[0].read_bytes()) if descriptors else None
        return {'directory': directory, 'descriptor': descriptor, 'saved': saved, 'state': statefile, 'log': log, 'transport_log': transport_log, 'process': process, 'host_lease': home / '.pocket/native-midi-writer/host-lease.json'}

    yield start
    for process in runs:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def wire_request(bridge, *, key='b', timeout=1000):
    import hashlib
    import json

    from test_native_midi_qa import http_request, read_request

    descriptor = bridge['descriptor']
    status, read = http_request(descriptor, '/read', read_request(descriptor, key='a'))
    assert status == 200
    expected = json.loads(read['journal']['result_json'])['observation']
    return {'schema': 'pocket.native-midi-write-request/v1', 'request_key': key * 64,
        'session_nonce': descriptor['session_nonce'], 'package_sha256': descriptor['package_sha256'],
        'target': {'location': 'arrangement', 'track_index': 1, 'clip_index': 0}, 'expected': expected,
        'edit': {'kind': 'insert_empty', 'notes': [{'pitch': 60, 'start_time': 0, 'duration': .5, 'velocity': 73, 'release_velocity': 37}]},
        'saved_als_path': str(bridge['saved']), 'saved_als_sha256': hashlib.sha256(bridge['saved'].read_bytes()).hexdigest(),
        'workspace_path': str(bridge['state']), 'workspace_sha256': hashlib.sha256(bridge['state'].read_bytes()).hexdigest(),
        'binding': {'workspace_id': bridge['state'].parent.name, 'pending_revision': 2}, 'timeout_ms': timeout}


def prepared_request(bridge, request):
    from test_native_midi_qa import http_request

    status, response = http_request(bridge['descriptor'], '/prepare', request)
    assert status == 200, response
    assert response['journal']['state'] == 'prepared', response
    return {'request_key': request['request_key'], 'session_nonce': request['session_nonce'],
            'prepare_sha256': response['journal']['prepare_result_sha256']}


def test_qa_writer_transport_durable_prepare_cancel_and_no_native_dispatch(writer_bridge):
    import json

    from test_native_midi_qa import http_request

    bridge = writer_bridge()
    request = wire_request(bridge)
    prepared = prepared_request(bridge, request)
    journal = bridge['directory'] / 'journals' / request['session_nonce'] / (request['request_key'] + '.json')
    assert json.loads(journal.read_bytes())['dispatch_intent_count'] == 0
    assert bridge['log'].read_bytes() == b''
    status, cancelled = http_request(bridge['descriptor'], '/cancel', prepared)
    assert status == 200
    terminal = json.loads(cancelled['journal']['result_json'])
    assert terminal['outcome'] == 'refused_before_dispatch' and terminal['dispatch_count'] == 0
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 409
    assert bridge['log'].read_bytes() == b''


def test_qa_writer_transport_commit_exactly_once_and_repeated_commit_refuses(writer_bridge):
    import json

    from test_native_midi_qa import http_request

    bridge = writer_bridge()
    request = wire_request(bridge)
    prepared = prepared_request(bridge, request)
    status, response = http_request(bridge['descriptor'], '/commit', prepared)
    assert status == 200
    terminal = json.loads(response['journal']['result_json'])
    assert terminal['outcome'] == 'verified_readback' and terminal['dispatch_count'] == 1
    assert terminal['after']['notes'][0]['release_velocity'] == 37
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 409
    assert len(bridge['log'].read_text().splitlines()) == 1


def test_qa_writer_transport_changed_saved_file_after_prepare_never_dispatches(writer_bridge):
    from test_native_midi_qa import http_request

    bridge = writer_bridge()
    request = wire_request(bridge)
    prepared = prepared_request(bridge, request)
    bridge['saved'].write_bytes(b'concurrent external save')
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 400
    assert bridge['log'].read_bytes() == b''


def wait_writer_journal(bridge, request, state):
    import json
    import time

    path = bridge['directory'] / 'journals' / request['session_nonce'] / (request['request_key'] + '.json')
    deadline = time.monotonic() + 3
    while True:
        value = json.loads(path.read_bytes())
        if value['state'] == state:
            return value
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_qa_writer_transport_prepare_timeout_can_cancel_only_after_late_ready(writer_bridge):
    import json

    from test_native_midi_qa import http_request

    bridge = writer_bridge('slow_prepare')
    request = wire_request(bridge, timeout=100)
    assert http_request(bridge['descriptor'], '/prepare', request)[0] == 504
    assert bridge['log'].read_bytes() == b''
    ready = wait_writer_journal(bridge, request, 'prepared')
    key = {'request_key': request['request_key'], 'session_nonce': request['session_nonce'], 'prepare_sha256': ready['prepare_result_sha256']}
    status, cancelled = http_request(bridge['descriptor'], '/cancel', key)
    assert status == 200
    assert json.loads(cancelled['journal']['result_json'])['dispatch_count'] == 0
    assert bridge['log'].read_bytes() == b''


def test_qa_writer_transport_commit_timeout_cannot_claim_cancellation_or_repeat(writer_bridge):
    import json

    from test_native_midi_qa import http_request

    bridge = writer_bridge('slow_commit')
    request = wire_request(bridge, timeout=100)
    prepared = prepared_request(bridge, request)
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 504
    assert http_request(bridge['descriptor'], '/cancel', prepared)[0] == 409
    final = wait_writer_journal(bridge, request, 'complete')
    assert json.loads(final['result_json'])['outcome'] == 'verified_readback'
    assert final['dispatch_intent_count'] == 1
    assert len(bridge['log'].read_text().splitlines()) == 1
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 409


def test_qa_writer_transport_lost_result_keeps_host_write_quarantined(writer_bridge):
    from test_native_midi_qa import http_request

    bridge = writer_bridge('lost_result')
    request = wire_request(bridge, timeout=100)
    prepared = prepared_request(bridge, request)
    assert http_request(bridge['descriptor'], '/commit', prepared)[0] == 504
    assert len(bridge['log'].read_text().splitlines()) == 1
    assert http_request(bridge['descriptor'], '/cancel', prepared)[0] == 409
    assert http_request(bridge['descriptor'], '/prepare', {**request, 'request_key': 'c' * 64})[0] == 409


def test_qa_writer_transport_prepared_request_reserves_host_across_bridge_instances(writer_bridge):
    from test_native_midi_qa import http_request

    first, second = writer_bridge(), writer_bridge()
    first_request, second_request = wire_request(first), wire_request(second)
    prepared_request(first, first_request)
    lease_before = first['host_lease'].read_bytes()
    assert_lease_refusal(http_request(second['descriptor'], '/prepare', second_request))
    assert first['host_lease'].read_bytes() == lease_before
    assert second['log'].read_bytes() == b''


def test_qa_writer_transport_async_dictionary_admission_cannot_prepare_two_requests(writer_bridge):
    from concurrent.futures import ThreadPoolExecutor

    from test_native_midi_qa import http_request

    bridge = writer_bridge('slow_dictionary')
    request = wire_request(bridge)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(http_request, bridge['descriptor'], '/prepare', request)
        second = pool.submit(http_request, bridge['descriptor'], '/prepare', {**request, 'request_key': 'c' * 64})
        statuses = sorted([first.result()[0], second.result()[0]])
    assert statuses == [200, 409]
    assert bridge['log'].read_bytes() == b''


def test_qa_writer_transport_uncertain_terminal_does_not_release_for_another_write(writer_bridge):
    import json

    from test_native_midi_qa import http_request

    bridge = writer_bridge('call_failure')
    request = wire_request(bridge)
    prepared = prepared_request(bridge, request)
    status, response = http_request(bridge['descriptor'], '/commit', prepared)
    assert status == 200
    assert json.loads(response['journal']['result_json'])['outcome'] == 'outcome_unknown'
    lease_before = bridge['host_lease'].read_bytes()
    assert_lease_refusal(http_request(bridge['descriptor'], '/prepare', {**request, 'request_key': 'c' * 64}))
    assert bridge['host_lease'].read_bytes() == lease_before
    assert len(bridge['log'].read_text().splitlines()) == 1


def assert_lease_refusal(response):
    import json

    status, response = response
    assert status == 200
    journal = response['journal']
    assert journal['state'] == 'complete' and journal['host_lease_acquired'] is False
    result = json.loads(journal['result_json'])
    assert result['stage'] == 'transport_refusal' and result['outcome'] == 'refused_before_dispatch'
    assert result['dispatch_count'] == 0 and result['before'] is None and result['after'] is None


@pytest.mark.parametrize('note_change', [
    {'onset_qn': {'n': True, 'd': 1}}, {'onset_qn': {'n': 1, 'd': 3}},
    {'onset_qn': {'n': 2, 'd': 2}}, {'onset_qn': {'n': 1, 'd': 2**21}},
    {'onset_qn': {'n': -1, 'd': 1}}, {'duration_qn': {'n': 0, 'd': 1}},
    {'onset_qn': {'n': 2**41, 'd': 1}}, {'velocity': True}, {'velocity': '73'},
    {'release_velocity': .5}, {'unknown': 'must not disappear'},
    {'onset_qn': {'n': 10**10000, 'd': 1}}, {'onset_qn': {'n': 1, 'd': 10**10000}},
    {'velocity': 10**10000}, {'onset_qn': {'n': -0.0, 'd': 1}},
    {'duration_qn': {'n': float('nan'), 'd': 1}}, {'duration_qn': {'n': float('inf'), 'd': 1}},
])
def test_qa_public_writer_malformed_note_payload_refuses_before_native_or_artifact_access(tmp_path, monkeypatch, note_change):
    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    monkeypatch.setattr(native_midi, '_connection', lambda *a, **kw: pytest.fail('Malformed input contacted Live'))
    monkeypatch.setattr(native_midi, 'load_native_midi_observation', lambda *a, **kw: pytest.fail('Malformed edit accessed unrelated artifacts'))
    note = {'pitch': 60, 'onset_qn': {'n': 0, 'd': 1}, 'duration_qn': {'n': 1, 'd': 2},
            'velocity': 73, 'release_velocity': 37, **note_change}
    with pytest.raises(PocketError):
        native_midi.native_midi_write(str(tmp_path / 'store'), 'qa-malformed-write',
            'workspace-' + 'a' * 24, 1, str(tmp_path / 'saved.als'), 'b' * 64, {}, {},
            {'kind': 'insert_empty', 'notes': [note]},
            {'actor': 'Synthetic reviewer', 'actor_kind': 'agent', 'observed_at': '2026-09-17T00:00:00+00:00',
             'exclusive_native_session': True, 'no_ui_edits_after_native_read': True})
    assert not (tmp_path / 'store').exists()


def public_writer_setup(tmp_path, writer_bridge, monkeypatch, mode='normal'):
    import gzip
    import xml.etree.ElementTree as ET

    from test_native_candidates import CONTEXT, material_fixture, snapshot, source_fixture
    from test_thread import value

    from pocket_music import native_midi
    from pocket_music.assets import sha256_file
    from pocket_music.native_candidates import candidate_prepare
    from pocket_music.native_normalization import BUILD

    source = source_fixture(tmp_path)
    root = ET.fromstring(gzip.decompress(source.read_bytes()))
    root.attrib = dict(BUILD)
    value(root.find('LiveSet'), 'LomId', 0)
    audio = root.find('LiveSet/Tracks/AudioTrack')
    value(audio, 'LomId', 0)
    value(audio.find('.//AudioClip'), 'LomId', 0)
    for ident, kind, device_id in [('2', 'Reverb', '2'), ('3', 'Delay', '7')]:
        track = ET.SubElement(root.find('LiveSet/Tracks'), 'ReturnTrack', Id=ident)
        value(ET.SubElement(track, 'Name'), 'EffectiveName', kind)
        value(track, 'LomId', 0)
        devices = ET.SubElement(ET.SubElement(ET.SubElement(track, 'DeviceChain'), 'DeviceChain'), 'Devices')
        value(ET.SubElement(devices, kind, Id=device_id), 'LomId', 0)
    source.write_bytes(gzip.compress(ET.tostring(root), mtime=0))
    source_snapshot = snapshot(source.parent)
    store = str(tmp_path / 'public-store')
    prepared = candidate_prepare(str(source), sha256_file(source), store, 'qa-public-prepare',
        mode='with_material', context=CONTEXT, material=material_fixture(store),
        layer={'track_name': 'Owned MIDI', 'instrument_device': 'Operator'})
    saved = Path(prepared['workspace']['mutable_als_path'])
    device = native_midi.build_native_midi_writer(str(tmp_path / 'published-writer'), store, 'qa-build-writer')
    if mode == 'custom_package':
        import hashlib
        import json

        from pocket_music.artifact_store import canonical_bytes, digest, put_bytes, put_record, read_record

        package = Path(device['device_path']).parent
        (package / 'writer.js').write_bytes((package / 'writer.js').read_bytes() + b'\n// externally modified package\n')
        manifest = json.loads((package / 'manifest.json').read_bytes())
        manifest['sources']['writer.js'] = hashlib.sha256((package / 'writer.js').read_bytes()).hexdigest()
        manifest['package_sha256'] = digest(manifest['sources'])
        (package / 'manifest.json').write_bytes(canonical_bytes(manifest))
        record = read_record(device['artifacts']['device'], store)
        record['package_sha256'] = manifest['package_sha256']
        for name in ('writer.js', 'manifest.json'):
            record['files'][name] = put_bytes((package / name).read_bytes(), store, name, 'pocket.native-midi-device-file/v1')
        device['artifacts']['device'] = put_record(record, store)
    root = ET.fromstring(gzip.decompress(saved.read_bytes()))
    track = ET.Element('MidiTrack', Id='99')
    root.find('LiveSet/Tracks').insert(1, track)
    value(ET.SubElement(track, 'Name'), 'EffectiveName', 'Owned MIDI')
    chain = ET.SubElement(track, 'DeviceChain')
    devices = ET.SubElement(ET.SubElement(chain, 'DeviceChain'), 'Devices')
    ET.SubElement(devices, 'Operator', Id='100')
    writer = ET.SubElement(devices, 'MxDeviceAudioEffect', Id='101')
    reference = ET.SubElement(ET.SubElement(writer, 'MxPatchRef'), 'FileRef')
    value(reference, 'Path', device['device_path'])
    events = ET.SubElement(ET.SubElement(ET.SubElement(ET.SubElement(chain, 'MainSequencer'), 'ClipTimeable'), 'ArrangerAutomation'), 'Events')
    clip = ET.SubElement(events, 'MidiClip', Id='1', Time='4')
    for name, val in [('Name', 'Clip'), ('CurrentStart', 4), ('CurrentEnd', 8)]:
        value(clip, name, val)
    loop = ET.SubElement(clip, 'Loop')
    for name, val in [('LoopOn', False), ('StartRelative', 0), ('LoopStart', 0), ('LoopEnd', 4)]:
        value(loop, name, val)
    notes = ET.SubElement(clip, 'Notes')
    ET.SubElement(notes, 'KeyTracks')
    ET.SubElement(ET.SubElement(notes, 'PerNoteEventStore'), 'EventLists')
    ET.SubElement(notes, 'NoteProbabilityGroups')
    for name in ['ProbabilityGroupIdGenerator', 'NoteIdGenerator']:
        value(ET.SubElement(notes, name), 'NextId', 1)
    ET.SubElement(ET.SubElement(clip, 'Envelopes'), 'Envelopes')
    saved.write_bytes(gzip.compress(ET.tostring(root), mtime=0))
    live = writer_bridge(mode, saved_input=saved)
    monkeypatch.setattr(native_midi, '_host_lease_path', lambda: live['host_lease'])
    read = native_midi.native_midi_read(store, 'qa-empty-writer-read',
        {'location': 'arrangement', 'track_index': 1, 'clip_index': 0},
        saved_binding={'saved_als': str(saved), 'expected_sha256': sha256_file(saved), 'track_id': '99', 'clip_id': '1'},
        bridge_dir=str(live['directory']))
    args = {'store_root': store, 'request_id': 'qa-native-insert', 'workspace_id': prepared['workspace']['workspace_id'],
        'expected_revision': 1, 'saved_als': str(saved), 'expected_sha256': sha256_file(saved),
        'expected_observation': read['artifacts']['observation'], 'writer_device': device['artifacts']['device'],
        'edit': {'kind': 'insert_empty', 'notes': [{'pitch': 48, 'onset_qn': {'n': 0, 'd': 1},
            'duration_qn': {'n': 1, 'd': 1}, 'velocity': 76, 'release_velocity': 64}]},
        'supervision': {'actor': 'Synthetic QA', 'actor_kind': 'agent', 'observed_at': '2026-09-17T00:00:00+00:00',
                       'exclusive_native_session': True, 'no_ui_edits_after_native_read': True},
        'bridge_dir': str(live['directory'])}
    return args, live, source, source_snapshot, prepared


def test_qa_public_writer_real_transport_completes_replay_without_save_or_listening(tmp_path, writer_bridge, monkeypatch):
    from test_native_candidates import snapshot

    from pocket_music import native_midi
    from pocket_music.native_candidates import candidate_inspect

    args, live, source, source_before, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    saved_before = Path(args['saved_als']).read_bytes()
    result = native_midi.native_midi_write(**args)
    assert result['status'] == 'ok', result
    terminal = native_midi.validate_native_terminal(result['artifacts']['terminal'], args['store_root'])
    assert terminal['outcome'] == 'verified_readback' and terminal['dispatch_count'] == 1
    assert native_midi.native_midi_write(**args) == result
    assert len(live['log'].read_text().splitlines()) == 1
    assert not live['host_lease'].exists()
    workspace = candidate_inspect(args['store_root'], args['workspace_id'], result['workspace']['revision'])
    assert workspace['workspace']['state'] == 'awaiting_native'
    assert Path(args['saved_als']).read_bytes() == saved_before
    assert snapshot(source.parent) == source_before
    assert result['coverage']['native_save_reopen'] == 'not_performed'
    assert result['coverage']['human_listening'] == 'none'


def test_qa_public_velocity_requires_insert_terminal_and_preserves_other_fields(tmp_path, writer_bridge, monkeypatch):
    import copy

    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    args, live, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    first = native_midi.native_midi_write(**args)
    before_handle = first['artifacts']['after_observation']
    before = native_midi.load_native_midi_observation(before_handle, args['store_root'])
    velocity_args = {**args, 'request_id': 'qa-native-velocity', 'expected_revision': first['workspace']['revision'],
        'expected_observation': before_handle,
        'edit': {'kind': 'set_velocity', 'note_id': 101, 'velocity': 89, 'insertion_terminal': first['artifacts']['terminal']}}
    wrong = copy.deepcopy(velocity_args)
    wrong['edit']['insertion_terminal']['artifact_schema'] = 'pocket.other/v1'
    with pytest.raises(PocketError):
        native_midi.native_midi_write(**wrong)
    assert len(live['log'].read_text().splitlines()) == 1
    result = native_midi.native_midi_write(**velocity_args)
    assert result['status'] == 'ok', result
    after = native_midi.load_native_midi_observation(result['artifacts']['after_observation'], args['store_root'])
    expected_notes = copy.deepcopy(before['notes'])
    expected_notes[0]['velocity'] = 89
    assert after['notes'] == expected_notes
    assert after['topology'] == before['topology'] and after['guards'] == before['guards']
    assert native_midi.native_midi_write(**velocity_args) == result
    assert len(live['log'].read_text().splitlines()) == 2


@pytest.mark.parametrize('mode', ['slow_prepare', 'slow_commit'])
def test_qa_public_timeout_has_explicit_cancel_or_late_terminal_reconciliation(tmp_path, writer_bridge, monkeypatch, mode):
    import json

    from test_native_candidates import snapshot

    from pocket_music import native_midi
    from pocket_music.native_candidates import candidate_native_reconcile

    args, live, source, original, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch, mode)
    result = native_midi.native_midi_write(**{**args, 'timeout_seconds': .1})
    assert result['status'] == 'outcome_unknown'
    statefile = Path(args['saved_als']).parent / 'workspace.json'
    assert json.loads(statefile.read_bytes())['state'] == 'outcome_unknown'
    wait_writer_journal(live, result['native_request'], 'prepared' if mode == 'slow_prepare' else 'complete')
    if mode == 'slow_prepare':
        completed = native_midi.native_midi_cancel(args['store_root'], 'qa-cancel-timeout', result['native_request'], str(live['directory']))
        assert completed['status'] == 'cancelled'
        assert live['log'].read_bytes() == b''
    else:
        completed = native_midi.native_midi_status(args['store_root'], 'qa-late-write-status', result['native_request'], str(live['directory']))
        assert completed['journal_state'] == 'complete'
        assert len(live['log'].read_text().splitlines()) == 1
    assert live['host_lease'].exists()
    revision = json.loads(statefile.read_bytes())['revision']
    reconciliation = candidate_native_reconcile(args['store_root'], args['workspace_id'], revision,
        completed['artifacts']['terminal'], {'actor': 'Synthetic QA', 'actor_kind': 'agent',
            'observed_at': '2026-09-17T00:00:00+00:00', 'reason': 'Verified retained synthetic terminal'}, 'qa-reconcile-timeout')
    assert reconciliation['workspace']['state'] == 'awaiting_native'
    assert not live['host_lease'].exists()
    assert snapshot(source.parent) == original


def test_qa_public_writer_rejects_external_package_symlink_before_pending_or_dispatch(tmp_path, writer_bridge, monkeypatch):
    import json

    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    args, live, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    package_file = tmp_path / 'published-writer/writer.js'
    moved = tmp_path / 'moved-writer.js'
    package_file.rename(moved)
    package_file.symlink_to(moved)
    with pytest.raises(PocketError, match='symlink'):
        native_midi.native_midi_write(**args)
    workspace = json.loads((Path(args['saved_als']).parent / 'workspace.json').read_bytes())
    assert workspace['state'] == 'awaiting_native' and workspace['revision'] == 1
    assert live['log'].read_bytes() == b'' and not live['host_lease'].exists()


@pytest.mark.parametrize('filename', ['writer.js', 'manifest.json', 'Native MIDI Writer.amxd'])
def test_qa_public_writer_rejects_package_file_artifact_family_substitution(tmp_path, writer_bridge, monkeypatch, filename):
    import json

    from pocket_music import native_midi
    from pocket_music.artifact_store import put_record, read_record
    from pocket_music.errors import PocketError

    args, live, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    record = read_record(args['writer_device'], args['store_root'])
    record['files'][filename]['artifact_schema'] = 'pocket.unrelated-data/v1'
    args['writer_device'] = put_record(record, args['store_root'])
    with pytest.raises(PocketError, match='schema|family|artifact'):
        native_midi.native_midi_write(**args)
    assert live['log'].read_bytes() == b''
    assert json.loads(live['state'].read_bytes())['revision'] == 1


def test_qa_new_native_dispatch_requires_qualified_bundled_sources_not_only_self_consistent_package(tmp_path, writer_bridge, monkeypatch):
    import json

    from pocket_music import native_midi
    from pocket_music.errors import PocketError

    args, live, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch, 'custom_package')
    # A self-consistent externally authored package remains inspectable evidence.
    record = native_midi.load_native_writer_device(args['writer_device'], args['store_root'])
    observed = native_midi.load_native_midi_observation(args['expected_observation'], args['store_root'])
    assert record['package_sha256'] == observed['package_sha256'] == live['descriptor']['package_sha256']
    with pytest.raises(PocketError, match='qualified|bundled|profile|package'):
        native_midi.native_midi_write(**args)
    assert live['log'].read_bytes() == b'' and not live['host_lease'].exists()
    assert json.loads(live['state'].read_bytes())['revision'] == 1


def test_qa_terminal_lineage_hidden_inside_wire_json_is_bounded(tmp_path, writer_bridge, monkeypatch):
    import copy
    import hashlib
    import json

    from pocket_music import native_midi
    from pocket_music.artifact_store import canonical_bytes, put_record, read_record
    from pocket_music.errors import PocketError

    args, _, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    result = native_midi.native_midi_write(**args)
    store = args['store_root']
    template = read_record(result['artifacts']['terminal'], store)
    journal_template = read_record(template['bridge_journal'], store)
    nested = result['artifacts']['terminal']
    # Each direct artifact graph is shallow. The malicious references live in
    # serialized request_json, which an ordinary artifact walker cannot see.
    for _ in range(220):
        journal = copy.deepcopy(journal_template)
        wire = json.loads(journal['request_json'])
        wire['edit'] = {'kind': 'set_velocity', 'note_id': 101, 'velocity': 90}
        wire['binding']['public_edit'] = {**wire['edit'], 'insertion_terminal': nested}
        journal['request_json'] = canonical_bytes(wire).decode()
        journal['request_sha256'] = hashlib.sha256(journal['request_json'].encode()).hexdigest()
        forged = copy.deepcopy(template)
        forged['bridge_journal'] = put_record(journal, store)
        nested = put_record(forged, store)
    with pytest.raises(PocketError):
        native_midi.validate_native_terminal(nested, store)


def test_qa_max_owned_input_dictionary_is_initialized_before_bound_and_prepare(tmp_path, writer_bridge, monkeypatch):
    import json

    from pocket_music import native_midi

    args, live, _, _, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch)
    result = native_midi.native_midi_write(**args)
    assert result['status'] == 'ok', result
    trace = [json.loads(line) for line in live['transport_log'].read_text().splitlines()]
    name = 'pocket_write_input_' + live['descriptor']['session_nonce']
    initialized = next(i for i, item in enumerate(trace) if item == {'event': 'dict_initialized', 'name': name})
    bound = next(i for i, item in enumerate(trace) if item['event'] == 'bound')
    sent = next(i for i, item in enumerate(trace) if item['event'] == 'set_dict')
    prepared = next(i for i, item in enumerate(trace) if item == {'event': 'max_message', 'method': 'prepare_write'})
    assert initialized < bound < sent < prepared
    assert trace[sent] == {'event': 'set_dict', 'name': name, 'exists': True}


def test_qa_missing_max_owned_dictionary_reproduces_preparing_failure_without_invented_terminal(tmp_path, writer_bridge, monkeypatch):
    import json

    from test_native_candidates import snapshot

    from pocket_music import native_midi
    from pocket_music.artifact_store import read_record

    args, live, source, original, _ = public_writer_setup(tmp_path, writer_bridge, monkeypatch, 'missing_write_dictionary')
    saved = Path(args['saved_als']).read_bytes()
    result = native_midi.native_midi_write(**args)
    assert result['status'] == 'outcome_unknown'
    assert 'HTTP 400' in result['reason']
    assert 'requested dict exists' in result['reason']
    assert 'terminal' not in result['artifacts']
    status = native_midi.native_midi_status(args['store_root'], 'qa-missing-dict-status', **result['recovery_arguments'])
    journal = read_record(status['artifacts']['bridge_journal'], args['store_root'])
    assert journal['state'] == 'preparing' and journal['host_lease_acquired'] is True
    assert journal['dispatch_intent_count'] == 0 and journal['prepare_result_json'] is None
    assert journal['result_json'] is None and live['host_lease'].exists()
    trace = [json.loads(line) for line in live['transport_log'].read_text().splitlines()]
    assert any(item['event'] == 'set_dict' and item['exists'] is False for item in trace)
    assert not any(item == {'event': 'max_message', 'method': 'prepare_write'} for item in trace)
    assert live['log'].read_bytes() == b''
    assert json.loads(live['state'].read_bytes())['state'] == 'outcome_unknown'
    assert native_midi.native_midi_write(**args) == result
    assert Path(args['saved_als']).read_bytes() == saved and snapshot(source.parent) == original


@pytest.mark.parametrize('mode', ['legacy_missing_dictionary', 'legacy_nonempty_dictionary'])
def test_qa_writer_readiness_refuses_missing_or_stale_max_owned_dictionary_before_advertising(mode, writer_bridge):
    import json

    live = writer_bridge(mode)
    assert live['descriptor'] is None
    assert not list(live['directory'].glob('connection-*.json'))
    assert not list((live['directory'] / 'journals').rglob('*.json'))
    assert not live['host_lease'].exists() and live['log'].read_bytes() == b''
    trace = [json.loads(line) for line in live['transport_log'].read_text().splitlines()]
    assert any(item['event'] == 'max_post' and 'Pocket writer not ready:' in item['message'] for item in trace)
    assert not any(item['event'] == 'max_message' and item['method'] in {'observe', 'prepare_write', 'commit_write'} for item in trace)
