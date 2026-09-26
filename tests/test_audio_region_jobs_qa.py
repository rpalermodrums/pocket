# SPDX-License-Identifier: AGPL-3.0-only
"""Independent ownership/publication attacks on queued region capture."""
import hashlib
import json
import os
import struct
import threading
import time
from pathlib import Path

import pytest

import pocket_music.audio_hypothesis_jobs as jobs
import pocket_music.audio_region_analysis as wrapper
import pocket_music.audio_regions as capture
from pocket_music.artifact_store import digest
from pocket_music.errors import PocketError

pytestmark=pytest.mark.skipif(os.name!='posix',reason='Qualified POSIX ownership only')


def fixture(tmp_path,monkeypatch,large=False):
    path=tmp_path/'source.wav';size=(270*1024**2 if large else 32000)
    header=b'RIFF'+struct.pack('<I',size+36)+b'WAVEfmt '+struct.pack('<IHHIIHH',16,1,2,44100,176400,4,16)+b'data'+struct.pack('<I',size)
    with path.open('wb') as stream:stream.write(header);stream.truncate(size+44)
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024**2):h.update(chunk)
    args={'store_root':str(tmp_path/'store'),'request_id':'queued','region':{'kind':'inline','source':{'path':str(path),'expected_sha256':h.hexdigest(),'start_frame':7,'frames':7000,'source_origin':'user_recording'}},
          'analysis':{'kind':'peek','settings':{'bpm_hint':None,'beats_per_bar':4}},
          'attribution':{'actor':'Independent QA','actor_kind':'agent','statement':'Synthetic source only','uncertainty':[]}}
    owned={}
    def spawn(root,job_id,nonce,lease_fd):owned.update(root=root,job_id=job_id,nonce=nonce,lease_fd=os.dup(lease_fd))
    monkeypatch.setattr(jobs,'_spawn',spawn)
    submitted=jobs.audio_region_submit(**args)
    return args,submitted,owned


def status(args,submitted):return jobs.job_status(args['store_root'],submitted['job']['job_id'])


def reseal(owned,mutate):
    path=Path(owned['root'])/'jobs'/owned['job_id']/'journal.json';record=json.loads(path.read_text());mutate(record)
    record['revision_sha256']=digest({k:v for k,v in record.items() if k!='revision_sha256'});path.write_text(json.dumps(record))


def test_durable_queued_before_hash_and_missing_source_status(tmp_path,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('Capture occurred before owned worker')
    monkeypatch.setattr(capture,'_capture',forbidden)
    args,submitted,owned=fixture(tmp_path,monkeypatch)
    try:
        assert submitted['state']=='queued' and submitted['source'] is None
        journal=Path(owned['root'])/'jobs'/owned['job_id']/'journal.json';assert journal.exists()
        assert jobs._lease_held(Path(owned['root']),owned['job_id'])
        Path(args['region']['source']['path']).unlink()
        assert status(args,submitted)['state']=='queued'
        result=jobs.job_cancel(args['store_root'],owned['job_id'],submitted['revision']);assert result['state']=='cancel_requested'
    finally:os.close(owned['lease_fd'])


def test_real_large_source_cancel_midstream_no_partial_result(tmp_path,monkeypatch):
    args,submitted,owned=fixture(tmp_path,monkeypatch,True);real=capture._exact;seen=[]
    def at_block(stream,length):
        result=real(stream,length)
        if length==1024**2 and not seen:
            seen.append(length);current=status(args,submitted)
            assert current['state']=='running' and jobs._lease_held(Path(owned['root']),owned['job_id'])
            assert jobs.job_cancel(args['store_root'],owned['job_id'],current['revision'])['state']=='cancel_requested'
        return result
    monkeypatch.setattr(capture,'_exact',at_block)
    jobs._run_worker(**owned)
    final=status(args,submitted);assert final['state']=='cancelled' and final['result'] is None and seen==[1024**2]
    assert not list((Path(args['store_root'])/'artifacts').glob('*/record.json'))
    assert jobs.audio_region_submit(**args)==submitted


@pytest.mark.parametrize('mutation',['schema','settings','source','nonce'])
def test_cheap_checkpoint_rejects_resealed_owned_identity_change(tmp_path,monkeypatch,mutation):
    args,_submitted,owned=fixture(tmp_path,monkeypatch);observed=[]
    def malicious(**kwargs):
        def alter(record):
            if mutation=='schema':record.update(schema='pocket.analysis-job/v2',analysis_kind='audio_pulse_hypotheses')
            elif mutation=='settings':record['arguments']['analysis']['settings']['beats_per_bar']=3
            elif mutation=='source':record['arguments']['region']['source']['start_frame']=8
            else:record['nonce']='f'*64
        reseal(owned,alter)
        with pytest.raises(PocketError):capture._cancel()
        observed.append(True)
        raise PocketError('Test stops after refusal')
    monkeypatch.setattr(wrapper,'audio_region_hypotheses',malicious)
    with pytest.raises(PocketError):jobs._run_worker(**owned)
    assert observed==[True]
    assert not list((Path(args['store_root'])/'artifacts').glob('*/record.json'))


def test_collection_failure_retains_diagnostics_not_committed_success(tmp_path,monkeypatch):
    args,submitted,owned=fixture(tmp_path,monkeypatch);real=jobs._collect_graph
    def broken(result,staging,root):
        real(result,staging,root)
        if Path(root)==Path(args['store_root']):raise OSError('after copied result')
    monkeypatch.setattr(jobs,'_collect_graph',broken)
    with pytest.raises(OSError):jobs._run_worker(**owned)
    final=status(args,submitted);assert final['state']=='failed' and final['result'] is None
    assert jobs.audio_region_submit(**args)==submitted


def test_two_cancel_cas_and_preanalysis_cancel(tmp_path,monkeypatch):
    args,submitted,owned=fixture(tmp_path,monkeypatch);barrier=threading.Barrier(2);results=[]
    def cancel():
        barrier.wait()
        try:results.append(jobs.job_cancel(args['store_root'],owned['job_id'],submitted['revision']))
        except PocketError as e:results.append(e)
    threads=[threading.Thread(target=cancel) for _ in range(2)]
    for thread in threads:thread.start()
    for thread in threads:thread.join(5);assert not thread.is_alive()
    assert sum(isinstance(x,dict) and x['status']=='ok' for x in results)==1
    jobs._run_worker(**owned)
    assert status(args,submitted)['state']=='cancelled'


def test_actual_child_matches_direct_wrapper_and_survives_source_delete(tmp_path,monkeypatch):
    real_spawn=jobs._spawn
    args,submitted,owned=fixture(tmp_path,monkeypatch)
    # Launch the already queued owned job exactly once, preserving the durable journal.
    real_spawn(root=owned['root'], job_id=owned['job_id'], nonce=owned['nonce'], lease_fd=owned['lease_fd'])
    os.close(owned['lease_fd']);deadline=time.monotonic()+40
    while True:
        try:current=status(args,submitted)
        except PocketError as error:
            if 'busy' not in str(error):raise
            current=None
        if current and current['state'] not in ('queued','running'):break
        assert time.monotonic()<deadline;time.sleep(.05)
    assert current['state']=='completed',current
    direct=wrapper.audio_region_hypotheses(**{k:v for k,v in {**args,'request_id':'direct'}.items()})
    assert current['result']==direct['artifacts']['hypotheses']
    Path(args['region']['source']['path']).unlink()
    assert status(args,submitted)['result']==current['result']


@pytest.mark.parametrize('change',['source_interval','settings','model_kind','result'])
def test_completed_resealed_job_cannot_rebind_valid_result(tmp_path,monkeypatch,change):
    args,submitted,owned=fixture(tmp_path,monkeypatch);jobs._run_worker(**owned)
    assert status(args,submitted)['state']=='completed'
    def alter(record):
        if change=='source_interval':record['arguments']['region']['source']['start_frame']=8
        elif change=='settings':record['arguments']['analysis']['settings']['bpm_hint']=120
        elif change=='model_kind':record['arguments']['analysis']['kind']='learned_pulse'
        else:record['result']['artifacts']['hypotheses']=None
    reseal(owned,alter)
    with pytest.raises(PocketError):status(args,submitted)


def test_interruption_leaves_no_result_or_automatic_retry(tmp_path,monkeypatch):
    args,submitted,owned=fixture(tmp_path,monkeypatch)
    def crash(**kwargs):raise KeyboardInterrupt('synthetic owner interruption')
    monkeypatch.setattr(wrapper,'audio_region_hypotheses',crash)
    with pytest.raises(KeyboardInterrupt):jobs._run_worker(**owned)
    current=status(args,submitted);assert current['state']=='interrupted' and current['result'] is None
    assert jobs.audio_region_submit(**args)==submitted
    assert status(args,submitted)['state']=='interrupted'


def test_commit_rejects_owned_arguments_changed_after_last_capture_callback(tmp_path,monkeypatch):
    args,submitted,owned=fixture(tmp_path,monkeypatch);real=wrapper.audio_region_hypotheses
    def altered_after_analysis(**kwargs):
        result=real(**kwargs)
        equivalent=tmp_path/'other-name.wav';equivalent.write_bytes(Path(args['region']['source']['path']).read_bytes())
        reseal(owned,lambda record:record['arguments']['region']['source'].update(path=str(equivalent)))
        return result
    monkeypatch.setattr(wrapper,'audio_region_hypotheses',altered_after_analysis)
    with pytest.raises(PocketError):jobs._run_worker(**owned)
    current=status(args,submitted);assert current['state']!='completed' and current['result'] is None
