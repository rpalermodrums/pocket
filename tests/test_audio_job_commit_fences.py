"""Final commit fences after the last optional-model cooperative callback."""
import copy

import pytest
from test_audio_pulse_jobs_qa import job, reseal, status  # noqa: F401

from pocket_music import audio_hypothesis_jobs as jobs
from pocket_music.artifact_store import put_record, read_record
from pocket_music.errors import PocketError


@pytest.mark.parametrize('changed', ['attribution', 'model'])
def test_valid_resealed_input_after_primitive_return_cannot_commit(job, monkeypatch, changed):  # noqa: F811
    import pocket_music.audio_pulse_hypotheses as pulse
    _, _, _, captured = job
    real = pulse.audio_pulse_hypotheses
    def primitive(**kwargs):
        result = real(**kwargs)
        def change(row):
            if changed == 'attribution':
                row['arguments']['attribution']['actor'] = 'different-agent'
            else:
                row['arguments']['model']['declaration']['weights']['sha256'] = 'f' * 64
        reseal(job, change)
        return result
    monkeypatch.setattr(pulse, 'audio_pulse_hypotheses', primitive)
    with pytest.raises(PocketError, match='inputs changed before commit'):
        jobs._run_worker(**captured)
    assert status(job)['state'] == 'failed'
    assert status(job)['result'] is None


def test_semantically_valid_other_attribution_result_refuses_before_completed(job, monkeypatch):  # noqa: F811
    import pocket_music.audio_pulse_hypotheses as pulse
    _, _, _, captured = job
    real = pulse.audio_pulse_hypotheses
    def primitive(**kwargs):
        result = copy.deepcopy(real(**kwargs))
        record = read_record(result['artifacts']['hypotheses'], kwargs['store_root'])
        record['request_attribution']['actor'] = 'different-agent'
        result['artifacts']['hypotheses'] = put_record(record, kwargs['store_root'])
        return result
    monkeypatch.setattr(pulse, 'audio_pulse_hypotheses', primitive)
    with pytest.raises(PocketError, match='differs from job inputs'):
        jobs._run_worker(**captured)
    assert status(job)['state'] == 'failed'
    assert status(job)['result'] is None
