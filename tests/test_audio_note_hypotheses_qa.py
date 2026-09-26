# SPDX-License-Identifier: AGPL-3.0-only
"""Independent synthetic decoder/projection expectations; no optional model runtime."""
import copy
from fractions import Fraction

import numpy as np
import pytest

from pocket_music.audio_note_projection import decode_note_frames, project_note_arrays, vendor_frame_times
from pocket_music.errors import PocketError


def matrices(kind):
    frames = np.zeros((80, 88), dtype=np.float32)
    onsets = np.zeros_like(frames)
    end = 31 if kind == 'length11' else 32 if kind == 'length12' else 50
    frames[20:end, 48] = .75
    onsets[20, 48] = .5
    if kind == 'plateau': onsets[21, 48] = .5
    if kind == 'first':
        frames[:50, 48] = .75;onsets[:] = 0;onsets[0, 48] = .8
    if kind == 'last': onsets[:] = 0;onsets[-1, 48] = .8
    if kind in ('adjacent', 'nonadjacent'):
        index = 49 if kind == 'adjacent' else 51
        frames[20:50, index] = .75;onsets[20, index] = .5
    if kind == 'exactthreshold': frames[20:50, 48] = np.float32(.3)
    if kind == 'belowthreshold': onsets[20, 48] = np.nextafter(np.float32(.5), np.float32(0))
    return frames, onsets


@pytest.mark.parametrize(('kind', 'expected'), [
    ('length11', []), ('length12', [[20, 32, 69, .75]]), ('plateau', []), ('first', []), ('last', []),
    ('adjacent', [[20, 50, 70, .75]]), ('nonadjacent', [[20, 50, 72, .75], [20, 50, 69, .75]]),
    ('exactthreshold', [[20, 50, 69, 0.30000004172325134]]), ('belowthreshold', []),
])
def test_exact_vendor_boundary_vectors_and_float32_reduction(kind, expected):
    frames, onsets = matrices(kind)
    before = frames.tobytes(), onsets.tobytes()
    assert decode_note_frames(frames, onsets) == expected
    assert (frames.tobytes(), onsets.tobytes()) == before


@pytest.mark.parametrize('bad', ['nan', 'inf', 'negative', 'overone', 'float64', 'bigendian', 'bins', 'empty', 'long', 'list', 'mismatch'])
def test_malformed_activation_matrix_domain_refusal(bad):
    frames, onsets = matrices('length12')
    if bad == 'nan': frames[0, 0] = np.nan
    elif bad == 'inf': onsets[0, 0] = np.inf
    elif bad == 'negative': frames[0, 0] = -.01
    elif bad == 'overone': frames[0, 0] = 1.01
    elif bad == 'float64': frames = frames.astype(np.float64)
    elif bad == 'bigendian': frames = frames.astype('>f4')
    elif bad == 'bins': frames = frames[:, :87]
    elif bad == 'empty': frames = frames[:0]
    elif bad == 'long': frames = np.zeros((1721, 88), np.float32)
    elif bad == 'list': frames = frames.tolist()
    else: onsets = onsets[:79]
    with pytest.raises(PocketError): decode_note_frames(frames, onsets)


def windows(resampled=88200, start=141, end=181):
    count = len(range(0, resampled + 3840, 36164))
    result = {name: np.zeros((count, 172, bins), dtype=np.float32)
              for name, bins in [('note', 88), ('onset', 88), ('contour', 264)]}
    for i in range(start, end): result['note'][i // 142, 15 + i % 142, 48] = .75
    result['onset'][start // 142, 15 + start % 142, 48] = .5
    return result


def source(rate=22050, start=701, frames=88200):
    return {'start_frame': start, 'end_frame_exclusive': start + frames, 'sample_rate': rate}


def test_raw142_seam_differs_from172_time_boundary_exact_envelope():
    raw = windows()
    before = {k: v.tobytes() for k, v in raw.items()}
    result = project_note_arrays(raw, source(), 88200)
    assert result['frame_count'] == 344 and result['window_starts'] == [0, 36164, 72328]
    row = result['ledger'][0]
    assert (row['start_model_frame'], row['end_model_frame']) == (141, 181)
    assert row['raw_start'] == {'window_index': 0, 'frame_index': 156}
    assert row['raw_end'] == {'window_index': 1, 'frame_index': 54}
    correction = (256 / 22050) * (172 - 43844 / 256) + .0018
    left = Fraction.from_float(141 * 256 / 22050) * 22050 + 701
    right = Fraction.from_float(181 * 256 / 22050 - correction) * 22050 + 701
    assert row['original_start_frame_q'] == {'n': left.numerator, 'd': left.denominator}
    assert row['original_end_frame_q'] == {'n': right.numerator, 'd': right.denominator}
    assert result['events'][0]['start_frame'] == left.numerator // left.denominator
    assert result['events'][0]['end_frame_exclusive'] == -(-right.numerator // right.denominator)
    assert not result['excluded']
    assert {k: v.tobytes() for k, v in raw.items()} == before


def test_float_estimate_outward_start_differs_from_nominal_rational():
    row = project_note_arrays(windows(start=21, end=151), source(start=0), 88200)['ledger'][0]
    assert row['outward_envelope'] == {'start_frame': 5375, 'end_frame_exclusive': 38656}
    assert row['local_start_hex'] == '0x1.f3526859b8cecp-3'
    assert Fraction(21 * 256, 22050) * 22050 == 5376


@pytest.mark.parametrize('location', ['discarded_start', 'discarded_end', 'unused_tail', 'contour'])
def test_bad_samples_anywhere_raw_including_unused_edges_refuse(location):
    raw = windows()
    if location == 'discarded_start': raw['note'][0, 0, 0] = np.nan
    elif location == 'discarded_end': raw['onset'][0, 171, 0] = np.inf
    elif location == 'unused_tail': raw['note'][-1, 155, 0] = -1
    else: raw['contour'][0, 0, 0] = 2
    with pytest.raises(PocketError): project_note_arrays(raw, source(), 88200)


def test_all_zero_and_signed_zero_no_fabricated_notes():
    raw = {name: np.zeros_like(value) for name, value in windows().items()}
    raw['note'][0, 0, 0] = np.float32(-0.)
    before = raw['note'].tobytes()
    result = project_note_arrays(raw, source(), 88200)
    assert result['events'] == result['ledger'] == result['excluded'] == []
    assert raw['note'].tobytes() == before


@pytest.mark.parametrize('bad', [True, 0, 1721, 1.5, '172'])
def test_vendor_clock_count_strict(bad):
    with pytest.raises(PocketError): vendor_frame_times(bad)


def test_unwrapped_incomplete_last_window_trim_and_discarded_event():
    raw = windows(resampled=44101, start=180, end=200)
    result = project_note_arrays(raw, source(frames=44101), 44101)
    assert result['frame_count'] == 172
    assert result['events'] == []


def test_source_outside_envelope_retains_excluded_proof_not_clamping():
    result = project_note_arrays(windows(start=21, end=151), source(frames=6000), 88200)
    assert result['events'] == [] and len(result['excluded']) == 1
    assert result['excluded'][0]['outward_envelope']['end_frame_exclusive'] == 39357
    assert result['excluded'][0]['reason'] == 'outside_or_empty_half_open_source_envelope'


def test_source_array_mutation_changes_complete_ledger_not_just_count():
    raw = windows(start=21, end=151)
    original = project_note_arrays(raw, source(), 88200)
    revised = copy.deepcopy(raw)
    revised['onset'][0, 36, 48] = 0
    assert project_note_arrays(revised, source(), 88200)['events'] == []
    assert original['events']

# Faithful protocol fixture only; no external model/runtime is executed.
from test_audio_note_hypotheses import args as note_args
from test_audio_note_hypotheses import note_fixture  # noqa: F401

from pocket_music import audio_note_hypotheses as notes
from pocket_music.artifact_store import put_record, read_record


def initial(fixture):
    result = notes.audio_note_hypotheses(request_id='qa-source', **note_args(fixture))
    return result, read_record(result['artifacts']['hypotheses'], fixture['store_root'])


@pytest.mark.parametrize('mutation', ['omit', 'add', 'pitch', 'amplitude', 'time', 'raw_start', 'hex', 'source_rate', 'transform', 'settings', 'attribution'])
def test_resealed_initial_event_source_projection_proofs_refuse(note_fixture, mutation):  # noqa: F811
    f = note_fixture; _, body = initial(f)
    evidence = read_record(body['analysis'], f['store_root'])
    projection = evidence['analysis']['projection']
    if mutation == 'omit': projection['events'] = [];projection['ledger'] = []
    elif mutation == 'add': projection['events'].append(copy.deepcopy(projection['events'][0]))
    elif mutation == 'pitch': projection['events'][0]['midi_note'] += 1
    elif mutation == 'amplitude': projection['ledger'][0]['amplitude_estimate'] = .9
    elif mutation == 'time': projection['ledger'][0]['outward_envelope']['start_frame'] += 1
    elif mutation == 'raw_start': projection['ledger'][0]['raw_start']['window_index'] += 1
    elif mutation == 'hex': projection['ledger'][0]['local_start_hex'] = '0x0.0p+0'
    elif mutation == 'source_rate': body['source']['sample_rate'] = 44100
    elif mutation == 'transform': evidence['transform']['trim_frames'] = 14
    elif mutation == 'settings': body['settings']['melodia_trick'] = True
    else: body['annotations'][0]['attribution']['actor'] = 'Musician'
    body['analysis'] = put_record(evidence, f['store_root'])
    # Forge matching IDs as well: complete ledger must still be recomputed from arrays.
    if mutation not in ('source_rate', 'settings', 'attribution'):
        body['annotations'] = notes._annotations(body['analysis'], projection)
        body['annotation_count'] = len(body['annotations'])
    with pytest.raises(PocketError): notes.load_note_hypotheses(put_record(body, f['store_root']), f['store_root'])


@pytest.mark.parametrize('mutation', ['binding', 'scope', 'adapter_profile'])
def test_resealed_model_claims_refuse(note_fixture, mutation):  # noqa: F811
    from pocket_music.artifact_store import digest
    f = note_fixture;_,body = initial(f);model = read_record(body['model'], f['store_root'])
    if mutation == 'binding': model['binding'] = {'arbitrary': 'claimed trusted runtime'}
    elif mutation == 'scope': model['qualification_scope'] = 'Human audition and native verified'
    else:
        profile = read_record(model['profile'], f['store_root'])
        profile['profile'] = {'adapter': 'basic_pitch_onnx_cpu_v1'}
        profile['profile_sha256'] = digest(profile['profile']);model['profile_sha256'] = profile['profile_sha256']
        model['declaration']['expected_profile'] = profile['profile_sha256'];model['profile'] = put_record(profile, f['store_root'])
        qualification = read_record(model['qualification'], f['store_root']);qualification['profile'] = model['profile']
        model['qualification'] = put_record(qualification, f['store_root'])
    with pytest.raises(PocketError): notes.load_audio_note_model(put_record(model, f['store_root']), f['store_root'])


def test_relocated_artifacts_only_no_model_or_source_execution(note_fixture, tmp_path, monkeypatch):  # noqa: F811
    import shutil
    from pathlib import Path
    f = note_fixture;result,body = initial(f)
    target = tmp_path / 'relocated'
    shutil.copytree(Path(f['store_root']) / 'artifacts', target / 'artifacts')
    Path(f['source']['path']).unlink()
    def forbidden(*a, **kw): raise AssertionError('Retained query tried source/runtime')
    monkeypatch.setattr(notes, '_runner', forbidden);monkeypatch.setattr(notes, '_declaration', forbidden)
    before = {str(p): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    assert notes.load_note_hypotheses(result['artifacts']['hypotheses'], str(target)) == body
    assert before == {str(p): p.read_bytes() for p in target.rglob('*') if p.is_file()}


def test_cancellation_after_runner_never_publishes_initial_hypotheses(note_fixture, monkeypatch):  # noqa: F811
    import json
    from pathlib import Path
    f = note_fixture; completed = False
    def runner(*a, **kw):
        nonlocal completed
        value = f['runner'](*a, **kw);completed = True
        return value
    def cancellation():
        if completed: raise PocketError('QA cancelled after runner')
    monkeypatch.setattr(notes, '_runner', runner)
    with notes.model_execution_context(cancellation_check=cancellation), pytest.raises(PocketError, match='cancelled'):
        notes.audio_note_hypotheses(request_id='qa-cancel', **note_args(f))
    assert not any(json.loads(p.read_text()).get('schema') == notes.SCHEMA for p in Path(f['store_root']).rglob('record.json'))


def test_odd44100_resample_rounds_half_up(note_fixture):  # noqa: F811
    import hashlib
    import io
    import wave

    from pocket_music.artifact_store import put_bytes
    f = note_fixture;_,body = initial(f);evidence = read_record(body['analysis'], f['store_root']);raw=evidence['analysis']['raw']
    output = io.BytesIO()
    with wave.open(output, 'wb') as w:
        w.setparams((1,2,44100,88201,'NONE','not compressed'));w.writeframes(bytes(88201*2))
    payload=output.getvalue();src={**body['source'],'sample_rate':44100,'frames':88201,'end_frame_exclusive':88201,'sha256':hashlib.sha256(payload).hexdigest()}
    raw['mono_sha256']=hashlib.sha256(bytes(88201*4)).hexdigest();raw['resampled_frames']=44101
    raw['resampled']={'shape':[44101],'dtype':'float32le','handle':put_bytes(bytes(44101*4),f['store_root'],'resampled.f32',notes.ARRAY_SCHEMA)}
    assert notes._projection(raw,src,payload,f['store_root'])['frame_count']==172


@pytest.mark.parametrize('mutation', ['name', 'bytes', 'shape', 'boolshape', 'endian', 'hash', 'nonfinite', 'truncated', 'symlink'])
def test_owned_binary_protocol_refuses_invalid_descriptor_or_file(tmp_path, mutation):
    import hashlib
    payload=np.zeros(2,dtype='<f4').tobytes();path=tmp_path/'resampled.f32';path.write_bytes(payload)
    descriptor={'file':path.name,'bytes':8,'shape':[2],'dtype':'float32le','sha256':hashlib.sha256(payload).hexdigest()}
    if mutation=='name':descriptor['file']='../resampled.f32'
    elif mutation=='bytes':descriptor['bytes']=16*1024**2+4
    elif mutation=='shape':descriptor['shape']=[441000,441000,441000]
    elif mutation=='boolshape':descriptor['shape']=[True,2]
    elif mutation=='endian':descriptor['dtype']='float32be'
    elif mutation=='hash':descriptor['sha256']='0'*64
    elif mutation=='nonfinite':
        payload=np.array([np.nan,0],dtype='<f4').tobytes();path.write_bytes(payload);descriptor['sha256']=hashlib.sha256(payload).hexdigest()
    elif mutation=='truncated':path.write_bytes(bytes(4))
    else:
        target=tmp_path/'target';target.write_bytes(payload);path.unlink();path.symlink_to(target)
    with pytest.raises(PocketError):notes._binary(tmp_path,descriptor)


def test_owned_file_symlink_swap_after_precheck_refuses(tmp_path,monkeypatch):
    import hashlib
    from pathlib import Path
    payload=bytes(8);path=tmp_path/'resampled.f32';path.write_bytes(payload);target=tmp_path/'outside';target.write_bytes(payload)
    descriptor={'file':path.name,'bytes':8,'shape':[2],'dtype':'float32le','sha256':hashlib.sha256(payload).hexdigest()}
    original=Path.is_file
    def swap(value):
        result=original(value)
        if value==path:
            path.unlink();path.symlink_to(target)
        return result
    monkeypatch.setattr(Path,'is_file',swap)
    with pytest.raises(PocketError):notes._binary(tmp_path,descriptor)


def test_owned_fifo_open_is_nonblocking_then_refuses(tmp_path,monkeypatch):
    import os
    path=tmp_path/'fifo';os.mkfifo(path);real=os.open
    def guarded(path,flags,*a,**kw):
        assert flags & os.O_NONBLOCK, 'Unsafe FIFO-race open could block before fstat'
        return real(path,flags,*a,**kw)
    monkeypatch.setattr(os,'open',guarded)
    with pytest.raises(PocketError):notes._owned_bytes(path,16)


@pytest.mark.parametrize('shape',['depth','nodes'])
def test_binary_manifest_structure_budget(shape):
    if shape=='depth':
        value={}
        for _ in range(34):value={'next':value}
    else:value=[None]*100001
    with pytest.raises(PocketError,match='structure'):notes._structure(value)


REAL_NOTE_RUNNER = notes._runner


@pytest.mark.parametrize('reason', ['cancel', 'timeout'])
def test_owned_child_lease_passed_then_cancel_timeout_kills_reaps(note_fixture,tmp_path,monkeypatch,reason):  # noqa: F811
    import os
    f=note_fixture;seen={};checks=0
    class Child:
        returncode=None
        killed=False
        waited=False
        def poll(self):return -9 if self.killed else None
        def kill(self):self.killed=True
        def wait(self,timeout):self.waited=True;return -9
    child=Child()
    def launch(command,**options):seen.update(command=command,options=options);return child
    def cancel():
        nonlocal checks
        checks+=1
        if reason=='cancel' and checks>=2:raise PocketError('synthetic owned cancel')
    monkeypatch.setattr(notes.subprocess,'Popen',launch)
    if reason=='timeout':
        ticks=iter([0.,100.]);monkeypatch.setattr(notes.time,'monotonic',lambda:next(ticks))
    read_fd,write_fd=os.pipe()
    try:
        with notes.model_execution_context(lease_fds=(read_fd,),cancellation_check=cancel),pytest.raises(PocketError):
            REAL_NOTE_RUNNER(f['model']['declaration'],'none',tmp_path/'unused-weights')
        assert seen['options']['pass_fds']==(read_fd,)
        assert seen['command'][0]==f['model']['declaration']['executable']['path']
        assert child.killed and child.waited
        assert notes._CONTEXT.get()==((),None)
    finally:os.close(read_fd);os.close(write_fd)


@pytest.mark.parametrize('mutation',['repeat_edge','missing_contour','reshaped','mono','resampled'])
def test_resealed_raw_tensor_completeness_refuses(note_fixture,mutation):  # noqa: F811
    from pocket_music.artifact_store import put_bytes, read_bytes
    f=note_fixture;_,body=initial(f);evidence=read_record(body['analysis'],f['store_root']);raw=evidence['analysis']['raw']
    if mutation=='repeat_edge':
        descriptor=raw['arrays']['0:note'];data=bytearray(read_bytes(descriptor['handle'],f['store_root']));data[:4]=np.float32(.25).tobytes()
        descriptor['handle']=put_bytes(bytes(data),f['store_root'],'edge.f32',notes.ARRAY_SCHEMA)
    elif mutation=='missing_contour':del raw['arrays']['1:contour']
    elif mutation=='reshaped':raw['arrays']['0:note']['shape']=[172,2,88]
    elif mutation=='mono':raw['mono_sha256']='0'*64
    else:
        descriptor=raw['resampled'];data=bytearray(read_bytes(descriptor['handle'],f['store_root']));data[:4]=np.float32(.25).tobytes();descriptor['handle']=put_bytes(bytes(data),f['store_root'],'altered.f32',notes.ARRAY_SCHEMA)
    body['analysis']=put_record(evidence,f['store_root'])
    with pytest.raises(PocketError):notes.load_note_hypotheses(put_record(body,f['store_root']),f['store_root'])


def test_public_note_correction_and_relocated_query_need_no_runtime(note_fixture,tmp_path,monkeypatch):  # noqa: F811
    import shutil
    from pathlib import Path

    from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query
    f=note_fixture;result,body=initial(f);parent=result['artifacts']['hypotheses'];target=tmp_path/'correction-store'
    shutil.copytree(Path(f['store_root'])/'artifacts',target/'artifacts');Path(f['source']['path']).unlink()
    def forbidden(*a,**kw):raise AssertionError('Correction/query executed model/source')
    monkeypatch.setattr(notes,'_runner',forbidden);monkeypatch.setattr(notes,'_declaration',forbidden)
    support=body['annotations'][0]['annotation_id']
    changed=audio_hypothesis_correct(store_root=str(target),request_id='authored-note',parent=parent,
        expected_revision=parent['sha256'],corrections=[{'correction_id':'supplied-alternative',
            'supersedes':[support],'annotation':{'kind':'note_hypothesis','start_frame':1000,
                'end_frame_exclusive':9000,'midi_note':57,'cents':0,'tuning_ref':'declared-test-only'},
            'support':[{'kind':'annotation_id','reference':support}],'uncertainty':['Synthetic authored alternative']}],
        attribution=f['attribution'])['artifacts']['hypotheses']
    page=audio_hypothesis_query(store_root=str(target),hypotheses=changed,view='annotations',limit=128)
    assert page['items'][0]==body['annotations'][0]
    assert page['items'][-1]['annotation']['midi_note']==57
    assert page['items'][-1]['attribution']['actor_kind']=='agent'


@pytest.mark.parametrize('mutation',['extra','schema','profile','implementation','vendor','numpy_type','numpy_empty','scipy_bad','scope','missing'])
def test_projection_provenance_strict_resealed_fields(note_fixture,mutation):  # noqa: F811
    f=note_fixture;_,body=initial(f);evidence=read_record(body['analysis'],f['store_root']);provenance=evidence['projection_provenance']
    if mutation=='extra':provenance['native_verified']=True
    elif mutation=='schema':provenance['schema']='pocket.note-projection-provenance/v2'
    elif mutation=='profile':provenance['profile']='basic_pitch_inferred_v1'
    elif mutation=='implementation':provenance['implementation_sha256']='0'*64
    elif mutation=='vendor':provenance['vendor_source_sha256']='0'*64
    elif mutation=='numpy_type':provenance['numpy_version']=[]
    elif mutation=='numpy_empty':provenance['numpy_version']=''
    elif mutation=='scipy_bad':provenance['scipy_version']='not a version with spaces'
    elif mutation=='scope':provenance['replay_semantics']='Equivalent across every runtime'
    else:del provenance['schema']
    body['analysis']=put_record(evidence,f['store_root']);body['annotations']=notes._annotations(body['analysis'],evidence['analysis']['projection'])
    with pytest.raises(PocketError):notes.load_note_hypotheses(put_record(body,f['store_root']),f['store_root'])


def test_legacy_provenance_absence_and_new_identity_remain_separate(note_fixture,monkeypatch):  # noqa: F811
    from pathlib import Path

    from pocket_music.audio_hypotheses import audio_hypothesis_correct, audio_hypothesis_query
    f=note_fixture;result,body=initial(f);new_handle=result['artifacts']['hypotheses'];evidence=read_record(body['analysis'],f['store_root'])
    retained=copy.deepcopy(evidence['projection_provenance']);assert retained['schema']=='pocket.note-projection-provenance/v1'
    del evidence['projection_provenance'];body['analysis']=put_record(evidence,f['store_root']);body['annotations']=notes._annotations(body['analysis'],evidence['analysis']['projection']);legacy=put_record(body,f['store_root'])
    assert legacy!=new_handle
    original_bytes=(Path(f['store_root'])/legacy['artifact_uri']).read_bytes()
    Path(f['source']['path']).unlink()
    def forbidden(*a,**kw):raise AssertionError('Legacy proof queried optional source/runtime')
    monkeypatch.setattr(notes,'_runner',forbidden);monkeypatch.setattr(notes,'_declaration',forbidden)
    assert notes.load_note_hypotheses(legacy,f['store_root'])==body
    old_summary=audio_hypothesis_query(store_root=f['store_root'],hypotheses=legacy)['items'][0]
    assert old_summary['projection_provenance'] is None
    assert old_summary['projection_provenance_status']=='legacy_not_retained'
    new_summary=audio_hypothesis_query(store_root=f['store_root'],hypotheses=new_handle)['items'][0]
    assert new_summary['projection_provenance']==retained
    assert new_summary['projection_provenance_status']=='retained'
    support=body['annotations'][0]['annotation_id']
    revised=audio_hypothesis_correct(store_root=f['store_root'],request_id='legacy-correct',parent=legacy,
        expected_revision=legacy['sha256'],corrections=[{'correction_id':'legacy-authored','supersedes':[],
            'annotation':{'kind':'attack','source_frame':1000,'strength_relative':None},
            'support':[{'kind':'annotation_id','reference':support}],'uncertainty':['Synthetic']}],attribution=f['attribution'])
    audio_hypothesis_query(store_root=f['store_root'],hypotheses=revised['artifacts']['hypotheses'],view='annotations')
    assert (Path(f['store_root'])/legacy['artifact_uri']).read_bytes()==original_bytes
    assert 'projection_provenance' not in read_record(body['analysis'],f['store_root'])
    new_body=notes.load_note_hypotheses(new_handle,f['store_root'])
    assert read_record(new_body['analysis'],f['store_root'])['projection_provenance']==retained
