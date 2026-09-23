"""Independent sample oracle and retained evidence for explicit join processing."""
import copy
import io
import shutil

import numpy as np
import pytest
import soundfile as sf
from test_musical_context import fixture

from pocket_music.artifact_store import put_bytes, put_record, read_bytes, read_record
from pocket_music.errors import PocketError
from pocket_music.practice_audio import practice_compare, practice_feedback, practice_query, practice_render
from pocket_music.practice_envelopes import practice_compare_processed, practice_envelope
from pocket_music.practice_feedback_query import practice_feedback_query


def envelope_fixture(tmp_path):
    store, context, definition, source, _ = fixture(tmp_path)
    render = practice_render(store, 'raw', context, ['first', 'again'])['artifacts']['render']
    spec = {'store_root': store, 'request_id': 'envelope', 'render': render,
            'joins': [{'boundary_frame': 8000, 'fade_out_frames': 3, 'fade_in_frames': 3, 'curve': 'linear'}],
            'attribution': definition['attribution']}
    return spec, source


def samples(handle, store):
    record = read_record(handle, store)
    return sf.read(io.BytesIO(read_bytes(record['audio'], store)), dtype='float64', always_2d=True)[0]


def test_join_independent_oracle_and_relocation(tmp_path):
    args, source = envelope_fixture(tmp_path)
    store = args['store_root']
    before = samples(args['render'], store)
    raw = read_record(args['render'], store)
    raw_bytes = read_bytes(raw['audio'], store)
    result = practice_envelope(**args)
    assert practice_envelope(**args) == result
    handle = result['artifacts']['render']
    expected = before.copy()
    expected[7997:8003] *= np.array([1, .5, 0, 0, .5, 1])[:, None]
    assert np.array_equal(samples(handle, store), expected)
    record = practice_query(store, handle)['summary']
    assert record['input_signal'] == raw['signal']
    assert record['listening'] == 'not_reviewed' and record['musical_verdict'] is None
    assert practice_query(store, handle, section='mappings')['rows'] == raw['mappings']
    assert read_bytes(raw['audio'], store) == raw_bytes
    moved = tmp_path/'moved'
    shutil.copytree(store, moved)
    shutil.rmtree(store)
    source.unlink()
    assert practice_query(str(moved), handle)['summary'] == record


@pytest.mark.parametrize('joins,match', [
    ([], 'bound'),
    ([{'boundary_frame': 7999, 'fade_out_frames': 3, 'fade_in_frames': 3, 'curve': 'linear'}], 'actual occurrence'),
    ([{'boundary_frame': 8000, 'fade_out_frames': 0, 'fade_in_frames': 3, 'curve': 'linear'}], 'bounded integer'),
    ([{'boundary_frame': True, 'fade_out_frames': 3, 'fade_in_frames': 3, 'curve': 'linear'}], 'bounded integer'),
    ([{'boundary_frame': 8000, 'fade_out_frames': 2001, 'fade_in_frames': 3, 'curve': 'linear'}], 'bounded integer'),
    ([{'boundary_frame': 8000, 'fade_out_frames': 3, 'fade_in_frames': 3, 'curve': 'auto'}], 'linear'),
    ([{'boundary_frame': 8000, 'fade_out_frames': 3, 'fade_in_frames': 3, 'curve': 'linear'}]*2, 'nonoverlapping'),
])
def test_invalid_explicit_joins(tmp_path, joins, match):
    args, _ = envelope_fixture(tmp_path)
    with pytest.raises(PocketError, match=match):
        practice_envelope(**{**args, 'joins': joins})


def test_single_frame_sides_zero_and_no_chained_or_implicit_processing(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    args['joins'][0].update(fade_out_frames=1, fade_in_frames=1)
    handle = practice_envelope(**args)['artifacts']['render']
    expected = samples(args['render'], args['store_root'])
    expected[7999:8001] = 0
    assert np.array_equal(samples(handle, args['store_root']), expected)
    with pytest.raises(PocketError):
        practice_envelope(**{**args, 'request_id': 'no-chain', 'render': handle})
    with pytest.raises(PocketError):
        practice_compare(args['store_root'], 'old', args['render'], [handle], 'No silent profile upgrade')


def test_rehashed_audio_and_provenance_cannot_pass(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    store = args['store_root']
    handle = practice_envelope(**args)['artifacts']['render']
    original = read_record(handle, store)
    bad = copy.deepcopy(original)
    bad['joins'][0]['fade_out_frames'] = 5
    with pytest.raises(PocketError, match='samples differ'):
        practice_query(store, put_record(bad, store))
    bad = copy.deepcopy(original)
    bad['input_signal']['frames'] -= 1
    with pytest.raises(PocketError, match='provenance'):
        practice_query(store, put_record(bad, store))
    bad = copy.deepcopy(original)
    changed = samples(handle, store)
    changed[100] += .01
    output = io.BytesIO()
    sf.write(output, changed, 8000, format='WAV', subtype='DOUBLE')
    bad['audio'] = put_bytes(output.getvalue(), store, 'tampered.wav', 'pocket.render-audio/v1')
    with pytest.raises(PocketError, match='samples differ'):
        practice_query(store, put_record(bad, store))


def test_processed_comparison_exact_parent_and_feedback(tmp_path):
    args, _ = envelope_fixture(tmp_path)
    store = args['store_root']
    handle = practice_envelope(**args)['artifacts']['render']
    compared = practice_compare_processed(store, 'compare', args['render'], [handle], 'Synthetic join only')['artifacts']['comparison']
    summary = practice_query(store, compared)['summary']
    assert summary['signal_ready'] and summary['musical_verdict'] is None
    feedback = practice_feedback(store, 'report', compared, handle, [7997, 8003], 'Fixture', 'agent',
                                 'Numerical oracle, no musical verdict')['artifacts']['feedback']
    page = practice_feedback_query(store, [feedback], render=handle, interval_frames=[7999, 8001])
    assert page['items'][0]['interval_frames'] == [7997, 8003]
    assert practice_query(store, feedback)['coverage']['profile'] == 'linear-loop-join-envelope/v1'
    baseline_feedback = practice_feedback(store, 'raw-report', compared, args['render'], [7997, 8003],
        'Fixture', 'agent', 'Raw baseline inside processed comparison')['artifacts']['feedback']
    assert practice_query(store, baseline_feedback)['coverage']['profile'] == 'exact-pcm-occurrences/v1'
    raw = read_record(args['render'], store)
    other = practice_render(store, 'other', raw['context'], ['again', 'alternative'])['artifacts']['render']
    with pytest.raises(PocketError, match='exact baseline'):
        practice_compare_processed(store, 'wrong-parent', other, [handle], 'Wrong source')


def test_multi_profile_practice_discovery():
    from pocket_music.capabilities import capabilities_list

    rows = capabilities_list(domain='practice', limit=50)['capabilities']
    for name in ('practice_query', 'practice_feedback'):
        row = next(row for row in rows if row['public_tool'] == name)
        assert row['required_profile'] is None
        assert 'pocket.practice-render/v1' in row['accepted_artifact_schemas']
        assert 'pocket.practice-envelope/v1' in row['accepted_artifact_schemas']
        assert 'linear-loop-join-envelope/v1' in row['prerequisites'][0]


def test_overload_outside_join_is_retained(tmp_path):
    from pocket_music.assets import sha256_file
    from pocket_music.audio_regions import audio_region_capture
    from pocket_music.musical_context import context_create
    store, _, definition, _, _ = fixture(tmp_path)
    source = tmp_path/'overloaded.wav'
    original = np.full(32000, .1)
    original[1720] = 1.25
    sf.write(source, original, 8000, subtype='FLOAT')
    region = audio_region_capture(store_root=store, request_id='overload-capture', source={'path': str(source),
        'expected_sha256': sha256_file(source), 'start_frame': 700, 'frames': 20000,
        'source_origin': 'independently_acquired'})['artifacts']['region']
    definition['sources'][0]['region'] = region
    context = context_create(store, 'overload-context', definition)['artifacts']['context']
    raw = practice_render(store, 'overload-raw', context, ['first', 'again'])['artifacts']['render']
    result = practice_envelope(store, 'overload-envelope', raw,
        [{'boundary_frame': 8000, 'fade_out_frames': 40, 'fade_in_frames': 40, 'curve': 'linear'}], definition['attribution'])
    record = practice_query(store, result['artifacts']['render'])['summary']
    assert not record['input_signal']['usable_for_expectation']
    assert not record['signal']['usable_for_expectation']
    assert samples(result['artifacts']['render'], store)[20, 0] == 1.25
    compared = practice_compare_processed(store, 'overload-compare', raw, [result['artifacts']['render']], 'No hidden normalization')
    assert not compared['coverage']['signal_ready']


def test_short_occurrences_refuse_out_of_bounds_and_overlapping_windows(tmp_path):
    from test_musical_context import q

    from pocket_music.musical_context import context_create
    store, _, definition, _, _ = fixture(tmp_path)
    for i, row in enumerate(definition['occurrences']):
        row['source_span_frames'] = [1700, 1702]
        row['timeline_span_qn'] = [q(i, 2000), q(i+1, 2000)]
    context = context_create(store, 'short-context', definition)['artifacts']['context']
    raw = practice_render(store, 'short-render', context, ['first', 'again', 'alternative'])['artifacts']['render']
    args = {'store_root': store, 'request_id': 'bad', 'render': raw, 'attribution': definition['attribution']}
    with pytest.raises(PocketError, match='in bounds'):
        practice_envelope(**args, joins=[{'boundary_frame': 2, 'fade_out_frames': 3, 'fade_in_frames': 1, 'curve': 'linear'}])
    with pytest.raises(PocketError, match='nonoverlapping'):
        practice_envelope(**{**args, 'request_id': 'overlap'}, joins=[
            {'boundary_frame': 2, 'fade_out_frames': 1, 'fade_in_frames': 2, 'curve': 'linear'},
            {'boundary_frame': 4, 'fade_out_frames': 1, 'fade_in_frames': 1, 'curve': 'linear'}])
