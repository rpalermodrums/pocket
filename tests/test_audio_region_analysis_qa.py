"""Independent region/local evidence composition and coordinate adversaries."""
import builtins
import copy
import hashlib
import shutil
import struct
from pathlib import Path
from unittest.mock import patch

import pytest
from test_audio_models_qa import fake_runner, setup  # noqa: F401

import pocket_music.audio_pulse_hypotheses as pulse
from pocket_music.artifact_store import canonical_bytes, put_record, read_record
from pocket_music.audio_hypotheses import audio_hypotheses, audio_hypothesis_correct
from pocket_music.audio_region_analysis import (
    audio_region_hypotheses,
    audio_region_query,
    load_audio_region_hypotheses,
)
from pocket_music.audio_regions import audio_region_capture, load_audio_region
from pocket_music.errors import PocketError


@pytest.fixture
def context(setup):  # noqa: F811 - independent fixture import
    args, declaration = setup
    return {'store_root': args['store_root'], 'request_id': 'region-inline',
            'region': {'kind': 'inline', 'source': args['source']},
            'analysis': {'kind': 'peek', 'settings': {'bpm_hint': None, 'beats_per_bar': 4}},
            'attribution': args['attribution']}, declaration


def query(args, handle, **kw):
    return audio_region_query(store_root=args['store_root'], hypotheses=handle, **kw)


def test_inline_captured_and_standalone_identity_source_stamp(context):
    args, _ = context;path=Path(args['region']['source']['path']);before=path.stat()
    captured=audio_region_capture(store_root=args['store_root'],request_id='capture',source=args['region']['source'])['artifacts']['region']
    first=audio_region_hypotheses(**args)
    second=audio_region_hypotheses(**{**args,'request_id':'composed','region':{'kind':'captured','region':captured}})
    assert first['artifacts']==second['artifacts']
    body=load_audio_region(captured,args['store_root']);source={'path':str(Path(args['store_root'])/body['crop']['artifact_uri']),
        'expected_sha256':body['crop']['sha256'],'start_frame':0,'frames':16000,'source_origin':'independently_acquired'}
    local=audio_hypotheses(store_root=args['store_root'],request_id='standalone',source=source,settings=args['analysis']['settings'],attribution=args['attribution'])
    wrapper=read_record(first['artifacts']['hypotheses'],args['store_root'])
    assert wrapper['local_hypotheses']==local['artifacts']['hypotheses']
    assert audio_region_hypotheses(**args)==first
    assert (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(path.stat().st_ino,path.stat().st_size,path.stat().st_mtime_ns,path.stat().st_ctime_ns)
    assert query(args,first['artifacts']['hypotheses'])['items'][0]['interval']=={'start_frame':701,'end_frame_exclusive':16701}


@pytest.mark.parametrize('change',['mapping','attribution','analysis_kind','local_source','local_interval','history_count'])
def test_resealed_relationships_cannot_change(context,change):
    args,_=context;result=audio_region_hypotheses(**args);wrapper=read_record(result['artifacts']['hypotheses'],args['store_root'])
    if change=='mapping':wrapper['coordinate_mapping']['original_start_frame']=702
    elif change=='attribution':wrapper['request_attribution']['actor']='different'
    elif change=='analysis_kind':wrapper['analysis_kind']='learned_pulse'
    else:
        local=read_record(wrapper['local_hypotheses'],args['store_root'])
        if change=='local_source':local['source']['source_origin']='user_recording'
        elif change=='local_interval':local['source']['start_frame']=1
        else:local['annotation_count']+=1
        wrapper['local_hypotheses']=put_record(local,args['store_root'])
    with pytest.raises(PocketError):query(args,put_record(wrapper,args['store_root']))


def test_authored_local_history_original_mapping_lattice_and_relocated_query(context,tmp_path,monkeypatch):
    args,_=context;result=audio_region_hypotheses(**args);wrapper=read_record(result['artifacts']['hypotheses'],args['store_root'])
    parent=wrapper['local_hypotheses']
    corrections=[]
    for key,annotation in [('start',{'kind':'attack','source_frame':0,'strength_relative':None}),
        ('end',{'kind':'phrase_anchor','start_frame':15999,'end_frame_exclusive':16000,'label':'Synthetic end'}),
        ('lattice',{'kind':'pulse_candidate','start_frame':0,'end_frame_exclusive':16000,'bpm':120,'source_lattice_origin_seconds':.1})]:
        corrections.append({'correction_id':key,'supersedes':[],'annotation':annotation,
            'support':[{'kind':'annotation_id','reference':read_record(parent,args['store_root'])['annotations'][0]['annotation_id']}],
            'uncertainty':['Authored synthetic alternative']})
    child=audio_hypothesis_correct(store_root=args['store_root'],request_id='local-correction',parent=parent,
        expected_revision=parent['sha256'],corrections=corrections,attribution=args['attribution'])['artifacts']['hypotheses']
    wrapper['local_hypotheses']=child;wrapped=put_record(wrapper,args['store_root'])
    moved=tmp_path/'relocated';shutil.copytree(Path(args['store_root'])/'artifacts',moved/'artifacts')
    Path(args['region']['source']['path']).unlink();real=builtins.__import__
    def blocked(name,*a,**kw):
        if name.split('.')[0] in {'torch','beat_this','torchaudio','soxr'}:raise AssertionError('optional model import')
        return real(name,*a,**kw)
    monkeypatch.setattr(builtins,'__import__',blocked)
    rows=audio_region_query(store_root=str(moved),hypotheses=wrapped,view='annotations')['items']
    assert rows[-3]['original_projection']['source_frame']==701
    assert rows[-2]['original_projection']['end_frame_exclusive']==16701
    assert rows[-1]['original_projection']['lattice_origin']=={'local_estimate_seconds':.1,'original_offset_seconds_q':{'n':701,'d':8000}}
    assert rows[-1]['local']['annotation']==corrections[-1]['annotation']
    page=query({**args,'store_root':str(moved)},wrapped,view='annotations',limit=1,max_bytes=4096)
    assert len(canonical_bytes(page))<=4096 and page['next_cursor']
    with pytest.raises(PocketError):query({**args,'store_root':str(moved)},wrapped,view='summary',cursor=page['next_cursor'])


def test_learned_half_model_frame_exact_projection_and_excluded_endpoint(context):
    args,declaration=context
    def runner(*a,**kw):
        result,binding=fake_runner(*a,**kw)
        if result['analysis']:
            raw=result['analysis'];raw['beat'][1:3]=[1.,1.];raw['beat'][100]=2.;raw['vendor_beats_seconds']=[.03,2.]
            h=hashlib.sha256(struct.pack('<101f',*raw['beat'])).hexdigest();raw['repeat_sha256']['beat']=[h,h]
        return result,binding
    analysis={'kind':'learned_pulse','model':{'kind':'inline','declaration':declaration,'qualification':'synthetic_cpu_v1'},'settings':pulse.SETTINGS}
    with patch.object(pulse,'_runner',side_effect=runner):result=audio_region_hypotheses(**{**args,'analysis':analysis})
    rows=query(args,result['artifacts']['hypotheses'],view='annotations')['items']
    assert len(rows)==1
    assert rows[0]['local']['annotation']['model_frame_q']=={'n':3,'d':2}
    assert rows[0]['local']['annotation']['source_frame_q']=={'n':240,'d':1}
    assert rows[0]['original_projection']['source_frame_q']=={'n':941,'d':1}
    with patch.object(pulse,'_runner',side_effect=AssertionError('query inference')):
        load_audio_region_hypotheses(result['artifacts']['hypotheses'],args['store_root'])


@pytest.mark.parametrize('change',['region_extra','analysis_extra','model_omitted','unknown_tag'])
def test_strict_union_never_publishes_wrapper(context,change):
    args,_=context;args=copy.deepcopy(args)
    if change=='region_extra':args['region']['extra']=True
    elif change=='analysis_extra':args['analysis']['extra']=True
    elif change=='model_omitted':args['analysis']['kind']='learned_pulse'
    else:args['region']['kind']='source_guess'
    with pytest.raises(PocketError):audio_region_hypotheses(**args)
    assert not list(Path(args['store_root']).glob('artifacts/*/record.json'))


def test_inline_replay_detects_source_change_retained_query_still_works(context):
    args,_=context;result=audio_region_hypotheses(**args)
    Path(args['region']['source']['path']).write_bytes(b'changed external source')
    with pytest.raises(PocketError):audio_region_hypotheses(**args)
    assert query(args,result['artifacts']['hypotheses'])['items'][0]['interval']['start_frame']==701


def test_resealed_local_revision_supersession_is_revalidated(context):
    args,_=context;result=audio_region_hypotheses(**args);wrapper=read_record(result['artifacts']['hypotheses'],args['store_root']);parent=wrapper['local_hypotheses']
    row={'correction_id':'span','supersedes':[],'annotation':{'kind':'attack','source_frame':1,'strength_relative':None},
         'support':[{'kind':'annotation_id','reference':read_record(parent,args['store_root'])['annotations'][0]['annotation_id']}],'uncertainty':[]}
    child=audio_hypothesis_correct(store_root=args['store_root'],request_id='correct-forge',parent=parent,expected_revision=parent['sha256'],corrections=[row],attribution=args['attribution'])['artifacts']['hypotheses']
    broken=read_record(child,args['store_root']);broken['annotations'][0]['supersedes']=['unknown']
    wrapper['local_hypotheses']=put_record(broken,args['store_root'])
    with pytest.raises(PocketError):query(args,put_record(wrapper,args['store_root']))
