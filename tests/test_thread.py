"""Generated, temporary fixtures: no user sets or recordings enter the repo."""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf

from pocket_music.errors import PocketError
from pocket_music.thread import inspect_set, source_position, arrangement_position
from pocket_music.timing import TempoMap, warp_coordinate


def value(parent, tag, v):
    return ET.SubElement(parent, tag, Value=str(v).lower() if isinstance(v, bool) else str(v))


def parameter(parent, tag, manual, target):
    node = ET.SubElement(parent, tag)
    value(node, 'Manual', manual)
    ET.SubElement(node, 'AutomationTarget', Id=str(target))
    return node


def lane(track, target, points):
    holder = track.find('AutomationEnvelopes/Envelopes')
    if holder is None:
        holder = ET.SubElement(ET.SubElement(track, 'AutomationEnvelopes'), 'Envelopes')
    env = ET.SubElement(holder, 'AutomationEnvelope', Id=str(len(holder)))
    value(ET.SubElement(env, 'EnvelopeTarget'), 'PointeeId', target)
    events = ET.SubElement(ET.SubElement(env, 'Automation'), 'Events')
    for i, (time, val) in enumerate(points):
        ET.SubElement(events, 'FloatEvent', Id=str(i), Time=str(time), Value=str(val))
    return events


class Fixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        (self.directory / 'Ableton Project Info').mkdir()
        self.root = ET.Element('Ableton', Creator='Ableton Live 12.4.5', MajorVersion='5')
        self.song = ET.SubElement(self.root, 'LiveSet')
        self.tracks = ET.SubElement(self.song, 'Tracks')
        self.main = ET.SubElement(self.song, 'MainTrack')
        mixer = ET.SubElement(ET.SubElement(self.main, 'DeviceChain'), 'Mixer')
        parameter(mixer, 'Tempo', 120, 900)
        self.tempo_events = lane(self.main, 900, [(-63072000, 120), (0, 120)])

    def track(self, native_id='10', kind='AudioTrack'):
        track = ET.SubElement(self.tracks, kind, Id=native_id)
        value(ET.SubElement(track, 'Name'), 'EffectiveName', 'Synthetic test track')
        chain = ET.SubElement(track, 'DeviceChain')
        mixer = ET.SubElement(chain, 'Mixer')
        target = 1000 + len(self.tracks)
        parameter(mixer, 'Volume', 0.8, target)
        lane(track, target, [(-63072000, .8), (4, .4)])
        seq = ET.SubElement(chain, 'MainSequencer')
        arranger = ET.SubElement(ET.SubElement(seq, 'Sample'), 'ArrangerAutomation')
        ET.SubElement(arranger, 'Events')
        ET.SubElement(ET.SubElement(chain, 'DeviceChain'), 'Devices')
        return track

    def clip(self, track, *, native_id='0', start=4, end=12, source_start=-.5,
             source_end=7.5, warped=True, loop=False, relative=0, name='audio.wav', midi=False):
        events = track.find('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events')
        clip = ET.SubElement(events, 'MidiClip' if midi else 'AudioClip', Id=native_id, Time=str(start))
        for tag, v in [('Name', 'Fixture'), ('CurrentStart', start), ('CurrentEnd', end),
                       ('Disabled', False), ('IsSongTempoLeader', False)]:
            value(clip, tag, v)
        looping = ET.SubElement(clip, 'Loop')
        for tag, v in [('LoopStart', source_start), ('LoopEnd', source_end),
                       ('StartRelative', relative), ('LoopOn', loop), ('OutMarker', source_end),
                       ('HiddenLoopStart', source_start), ('HiddenLoopEnd', source_end)]:
            value(looping, tag, v)
        if midi:
            notes = ET.SubElement(ET.SubElement(ET.SubElement(clip, 'Notes'), 'KeyTracks'), 'KeyTrack', Id='0')
            value(notes, 'MidiKey', 60)
            ET.SubElement(ET.SubElement(notes, 'Notes'), 'MidiNoteEvent', Time='0', Duration='.25',
                          Velocity='70', NoteId='1', Probability='.5')
            return clip
        for tag, v in [('IsWarped', warped), ('WarpMode', 6), ('SampleVolume', .5),
                       ('PitchCoarse', 0), ('PitchFine', 0), ('Fade', False)]:
            value(clip, tag, v)
        fades = ET.SubElement(clip, 'Fades')
        value(fades, 'FadeInLength', .02)
        value(fades, 'FadeOutLength', .04)
        sample = ET.SubElement(clip, 'SampleRef')
        ref = ET.SubElement(sample, 'FileRef')
        value(ref, 'RelativePathType', 3)
        value(ref, 'RelativePath', name)
        value(ref, 'Path', str(self.directory / name))
        value(sample, 'DefaultDuration', 8000 * 20)
        value(sample, 'DefaultSampleRate', 8000)
        markers = ET.SubElement(clip, 'WarpMarkers')
        for i, (seconds, beat) in enumerate([(0, -.5), (2, 3.5), (6, 7.5)]):
            ET.SubElement(markers, 'WarpMarker', Id=str(i), SecTime=str(seconds), BeatTime=str(beat))
        return clip

    def save(self, *, compressed=True):
        path = self.directory / 'fixture.als'
        xml = ET.tostring(self.root)
        path.write_bytes(gzip.compress(xml, mtime=0) if compressed else xml)
        return path


class SetMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.f = Fixture(self.temp.name)

    def test_piecewise_warp_pickup_and_inverse(self):
        track = self.f.track()
        self.f.clip(track)
        path = self.f.save()
        before = path.read_bytes()
        m = inspect_set(path)
        c = m['clips'][0]
        self.assertEqual(c['id'], 'track:10/clip:0')
        self.assertEqual(c['loop']['LoopStart'], -.5)
        self.assertEqual(source_position(m, c['id'], 4)['source_seconds'], 0)
        self.assertEqual(source_position(m, c['id'], 8)['source_seconds'], 2)
        self.assertEqual(source_position(m, c['id'], 10)['source_seconds'], 4)
        self.assertAlmostEqual(arrangement_position(m, c['id'], 4)['arrangement_beat'], 10)
        self.assertEqual(path.read_bytes(), before)
        json.dumps(m, allow_nan=False)

    def test_unwarped_changing_tempo_ignores_dormant_markers(self):
        track = self.f.track()
        self.f.clip(track, start=2, end=8, source_start=10, source_end=20, warped=False)
        ET.SubElement(self.f.tempo_events, 'FloatEvent', Id='2', Time='4', Value='60')
        m = inspect_set(self.f.save())
        c = m['clips'][0]
        expected = 10 + 4 * math.log(1.5) + 4
        self.assertAlmostEqual(source_position(m, c['id'], 8)['source_seconds'], expected)
        self.assertAlmostEqual(arrangement_position(m, c['id'], expected)['arrangement_beat'], 8)
        self.assertEqual(c['source_cuts']['saved_loop_end_seconds'], 20)
        self.assertLess(c['source_cuts']['implied_minus_saved_end_seconds'], 0)
        self.assertFalse(c['warp_markers_active'])

    def test_preserves_negative_automation_and_parameter_association(self):
        self.f.clip(self.f.track())
        m = inspect_set(self.f.save())
        lane_data = m['tracks'][0]['automation'][0]
        self.assertEqual(lane_data['events'][0]['time'], -63072000)
        self.assertEqual(lane_data['target_status'], 'resolved')
        self.assertTrue(lane_data['targets'][0]['parameter_path'].endswith('/Mixer/Volume'))
        self.assertEqual(m['tempo']['envelopes'][0]['events'][0]['time'], -63072000)

    def test_multiple_clips_and_duplicate_local_ids(self):
        t = self.f.track()
        self.f.clip(t)
        self.f.clip(t, start=20, end=28)
        self.f.clip(self.f.track('11'))
        m = inspect_set(self.f.save())
        self.assertEqual(len({c['id'] for c in m['clips']}), 3)
        self.assertEqual(m['clips'][0]['id'], 'track:10/clip:0@0')
        self.assertEqual(m['clips'][2]['id'], 'track:11/clip:0')
        with self.assertRaises(PocketError):
            source_position(m, {'track_id': '10', 'clip_id': '0'}, 4)
        self.assertEqual(source_position(m, {'track_id': '11', 'clip_id': '0'}, 4)['status'], 'mapped')

    def test_missing_active_source_and_dormant_preset_are_distinct(self):
        t = self.f.track()
        self.f.clip(t, name='missing.wav')
        device = ET.SubElement(t.find('DeviceChain/DeviceChain/Devices'), 'Reverb', Id='0')
        preset = ET.SubElement(ET.SubElement(device, 'LastPresetRef'), 'FileRef')
        value(preset, 'RelativePathType', 3)
        value(preset, 'RelativePath', 'old-preset.adv')
        m = inspect_set(self.f.save())
        self.assertEqual([(d['kind'], d['runtime_dependency']) for d in m['dependencies']],
                         [('audio_source', True), ('dormant_provenance', False)])
        missing = [w for w in m['warnings'] if w['code'] == 'runtime_dependency_missing']
        self.assertEqual(len(missing), 1)

    def test_header_and_conflicting_reference(self):
        sf.write(self.f.directory / 'audio.wav', np.zeros((160000, 2)), 8000)
        t = self.f.track()
        c = self.f.clip(t)
        m = inspect_set(self.f.save())
        self.assertEqual(m['dependencies'][0]['header']['frames'], 160000)
        self.assertTrue(source_position(m, m['clips'][0]['id'], 4)['within_decoded_source_bounds'])
        sf.write(self.f.directory / 'other.wav', np.ones((800, 2)) * .1, 8000)
        c.find('SampleRef/FileRef/Path').set('Value', str(self.f.directory / 'other.wav'))
        m = inspect_set(self.f.save())
        self.assertEqual(m['dependencies'][0]['status'], 'ambiguous')
        self.assertIsNone(m['dependencies'][0]['resolved_path'])

    def test_loop_and_nonzero_relative_are_explicit_unknown(self):
        t = self.f.track()
        self.f.clip(t, loop=True)
        self.f.clip(t, native_id='1', relative=.25)
        m = inspect_set(self.f.save())
        for c in m['clips']:
            self.assertEqual(source_position(m, c['id'], 4)['status'], 'unknown')
            self.assertEqual(arrangement_position(m, c['id'], 0)['status'], 'unknown')

    def test_curved_tempo_is_unknown_but_warp_coordinate_still_known(self):
        self.f.tempo_events[1].set('CurveControl1X', '.25')
        t = self.f.track()
        self.f.clip(t)
        self.f.clip(t, native_id='1', warped=False)
        m = inspect_set(self.f.save())
        self.assertEqual(m['tempo']['status'], 'unsupported')
        mapped = source_position(m, m['clips'][0]['id'], 8)
        self.assertEqual(mapped['source_seconds'], 2)
        self.assertIsNone(mapped['arrangement_seconds'])
        self.assertEqual(source_position(m, m['clips'][1]['id'], 8)['status'], 'unknown')

    def test_midi_notes_and_unsupported_instrument_inventory(self):
        t = self.f.track(kind='MidiTrack')
        self.f.clip(t, midi=True)
        device = ET.SubElement(t.find('DeviceChain/DeviceChain/Devices'), 'PluginDevice', Id='0')
        parameter(device, 'On', True, 4000)
        m = inspect_set(self.f.save())
        self.assertEqual(m['summary']['midi_tracks'], 1)
        self.assertEqual(m['clips'][0]['note_count'], 1)
        self.assertEqual(m['tracks'][0]['devices'][0]['coverage'], 'unsupported_dsp')
        self.assertEqual(source_position(m, m['clips'][0]['id'], 4)['status'], 'unknown')

    def test_session_has_no_fixed_arrangement_mapping(self):
        t = self.f.track()
        c = self.f.clip(t)
        holder = t.find('DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events')
        holder.remove(c)
        ET.SubElement(t, 'ClipSlot').append(c)
        m = inspect_set(self.f.save())
        self.assertEqual(m['clips'][0]['scope'], 'session')
        self.assertEqual(source_position(m, m['clips'][0]['id'], 4)['status'], 'unknown')

    def test_out_of_clip_nonfinite_and_bad_xml_fail(self):
        self.f.clip(self.f.track())
        p = self.f.save(compressed=False)
        m = inspect_set(p)
        for b in [3, 13, float('nan'), True]:
            with self.assertRaises(PocketError):
                source_position(m, m['clips'][0]['id'], b)
        with self.assertRaises(PocketError):
            arrangement_position(m, m['clips'][0]['id'], -1)
        p.write_bytes(b'<wrong/>')
        with self.assertRaises(PocketError):
            inspect_set(p)
        p.write_bytes(b'\x1f\x8bnot-a-complete-gzip-stream')
        with self.assertRaises(PocketError):
            inspect_set(p)
        p.write_bytes(b'<!DOCTYPE x [<!ENTITY y "test">]><Ableton><LiveSet/></Ableton>')
        with self.assertRaises(PocketError):
            inspect_set(p)

    def test_set_changed_during_inspection_is_rejected(self):
        self.f.clip(self.f.track())
        path = self.f.save()
        with patch('pocket_music.thread._stamp', side_effect=[(1, 2, 3, 4), (1, 2, 3, 5)]):
            with self.assertRaisesRegex(PocketError, 'changed during inspection'):
                inspect_set(path)

    def test_freeze_cache_is_not_an_extra_arrangement_clip(self):
        t = self.f.track()
        self.f.clip(t)
        t.find('DeviceChain/MainSequencer').tag = 'FreezeSequencer'
        m = inspect_set(self.f.save())
        self.assertEqual(m['summary']['arrangement_audio_clips'], 0)
        self.assertEqual(m['clips'][0]['scope'], 'freeze_cache')
        self.assertEqual(m['clips'][0]['mapping']['status'], 'unknown')

    def test_opt_in_hash_identity_and_unreadable_header(self):
        self.f.clip(self.f.track())
        audio = self.f.directory / 'audio.wav'
        sf.write(audio, np.zeros((800, 2)), 8000)
        m = inspect_set(self.f.save(), hash_sources=True)
        self.assertTrue(m['dependencies'][0]['asset_id'].startswith('sha256:'))
        self.assertEqual(m['clips'][0]['source_identity']['asset_id'], m['dependencies'][0]['asset_id'])
        audio.write_bytes(b'not audio')
        m = inspect_set(self.f.save())
        self.assertIn('unreadable_audio_header', [w['code'] for w in m['warnings']])

    def test_invalid_warp_and_duplicate_tempo_events_are_not_silently_sorted(self):
        c = self.f.clip(self.f.track())
        c.find('WarpMarkers')[1].set('BeatTime', '-1')
        ET.SubElement(self.f.tempo_events, 'FloatEvent', Id='2', Time='0', Value='130')
        m = inspect_set(self.f.save())
        self.assertEqual(m['tempo']['status'], 'unsupported')
        self.assertEqual(m['clips'][0]['mapping']['status'], 'unknown')


class TimingTests(unittest.TestCase):
    def test_nonlinear_seconds_linear_tempo_and_inverse(self):
        t = TempoMap([{'beat': 0, 'bpm': 120}, {'beat': 8, 'bpm': 240}])
        self.assertAlmostEqual(t.beat_to_seconds(8), 4 * math.log(2))
        for beat in (-4, 0, 1.5, 8, 12):
            self.assertAlmostEqual(t.seconds_to_beat(t.beat_to_seconds(beat)), beat)

    def test_nearly_instant_tempo_step_is_preserved(self):
        t = TempoMap([{'beat': 0, 'bpm': 120}, {'beat': 4, 'bpm': 120},
                      {'beat': 4.00000001, 'bpm': 60}])
        self.assertAlmostEqual(t.beat_to_seconds(8), 6, places=7)
        self.assertEqual(len(t.points), 3)

    def test_warp_inverse_extrapolation_and_invalid_map(self):
        markers = [{'source_seconds': 1, 'beat': 0}, {'source_seconds': 3, 'beat': 4}]
        self.assertEqual(warp_coordinate(markers, -2, inverse=True), 0)
        self.assertEqual(warp_coordinate(markers, 4), 6)
        with self.assertRaises(PocketError):
            warp_coordinate([markers[0], markers[0]], 1)


if __name__ == '__main__':
    unittest.main()
