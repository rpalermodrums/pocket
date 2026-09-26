# SPDX-License-Identifier: AGPL-3.0-only
"""Generated-media integration tests; no production paths or recordings."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import soundfile as sf
from test_thread import Fixture, value

from pocket_music.errors import PocketError
from pocket_music.thread import inspect_set
from pocket_music.thread_queries import (
    export_thread,
    find_clips,
    inspect_set_summary,
    parse_set_time,
    query_set_region,
)
from pocket_music.source_frames import source_frame_interval
from pocket_music.timing import TempoMap


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.f = Fixture(self.directory)
        sf.write(self.directory / 'audio.wav', np.ones((160000, 2)) * .01, 8000, subtype='FLOAT')
        self.track = self.f.track()
        self.clip = self.f.clip(self.track)
        self.cache = self.directory / 'cache'

    def summary(self):
        self.path = self.f.save()
        self.before = self.path.read_bytes()
        return inspect_set_summary(self.path, cache_dir=self.cache)

    def test_compact_snapshot_compatible_full_map_and_separate_process(self):
        summary = self.summary()
        self.assertEqual(summary['schema'], 'pocket.set-summary/v1')
        self.assertNotIn('tracks', summary)
        self.assertLess(len(json.dumps(summary).encode()), 4000)
        handle = summary['handle']
        output = subprocess.check_output([sys.executable, '-c',
            ('import json,sys; from pocket_music.thread_queries import find_clips; '
             'print(json.dumps(find_clips(json.loads(sys.argv[1]),"Fixture")))'), json.dumps(handle)],
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        found = json.loads(output)
        self.assertEqual(found['clips'][0]['id'], 'track:10/clip:0')
        dest = self.directory / 'full.json'
        exported = export_thread(handle, dest)
        self.assertEqual(json.loads(dest.read_text())['schema'], 'pocket.set-map/v1')
        self.assertEqual(exported['bytes'], dest.stat().st_size)
        with self.assertRaises(PocketError):
            export_thread(handle, dest)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(inspect_set(self.path)['clips'][0]['loop']['LoopStart'], -.5)

    def test_warp_region_frames_and_negative_bracket(self):
        handle = self.summary()['handle']
        result = query_set_region(handle, '0:02', 2)
        c = result['clips'][0]
        self.assertEqual(c['intersection']['start_beat'], 4)
        self.assertEqual(c['source_interval']['start_frame'], 0)
        self.assertEqual(c['source_interval']['end_frame_exclusive'], 16000)
        self.assertEqual(result['controls'][0]['automation'][0]['events'][0]['time'], -63072000)
        self.assertFalse(result['truncation']['any'])
        self.assertEqual(result['coverage']['native_loaded_media'], 'not_verified')

    def test_changing_tempo_natural_intersection(self):
        self.clip.find('IsWarped').set('Value', 'false')
        self.clip.find('Loop/LoopStart').set('Value', '3')
        ET.SubElement(self.f.tempo_events, 'FloatEvent', Id='2', Time='8', Value='60')
        tempo = TempoMap([{'beat': -63072000, 'bpm': 120}, {'beat': 0, 'bpm': 120}, {'beat': 8, 'bpm': 60}])
        start = tempo.beat_to_seconds(5)
        result = query_set_region(self.summary()['handle'], start, .5)
        region = result['clips'][0]['source_interval']
        expected = 3 + tempo.between(4, 5)
        self.assertAlmostEqual(region['raw_start_seconds'], expected)
        self.assertAlmostEqual(region['raw_end_seconds'] - region['raw_start_seconds'], .5)
        self.assertEqual(region['start_frame'], math.ceil(math.nextafter(expected * 8000, -math.inf)))

    def test_layers_midi_duplicate_local_ids_and_pagination(self):
        self.f.clip(self.track, start=6, end=10)
        midi = self.f.track('11', 'MidiTrack')
        self.f.clip(midi, start=4, end=12, midi=True)
        handle = self.summary()['handle']
        found = find_clips(handle, 'fixture', limit=1)
        self.assertEqual(found['total_matches'], 3)
        self.assertEqual(found['next_offset'], 1)
        self.assertIn('@0', found['clips'][0]['id'])
        result = query_set_region(handle, 3, 1)
        self.assertEqual(len(result['clips']), 3)
        self.assertEqual(result['clips'][-1]['type'], 'audio')
        notes = next(c for c in result['clips'] if c['type'] == 'midi')['midi']
        self.assertEqual(notes['note_count_in_clip'], 1)
        self.assertEqual(result['coverage']['midi_arrangement_instances'], 1)
        limited = query_set_region(handle, 3, 1, max_clips=1)
        self.assertTrue(limited['truncation']['any'])
        self.assertEqual(limited['next_clip_offset'], 1)

    def test_loop_groove_relative_and_transposed_natural_are_unknown(self):
        cases = [('Loop/LoopOn', 'true'), ('Loop/StartRelative', '.5'), ('PitchCoarse', '1')]
        for path, val in cases:
            with self.subTest(path=path):
                old = self.clip.find(path).get('Value')
                self.clip.find(path).set('Value', val)
                if path == 'PitchCoarse':
                    self.clip.find('IsWarped').set('Value', 'false')
                result = query_set_region(self.summary()['handle'], 2, 1)
                self.assertEqual(result['clips'][0]['source_interval']['status'], 'unknown')
                self.assertEqual(result['coverage']['audio_mapping']['unknown'], 1)
                self.assertIn('unsupported_audio_mapping', [w['code'] for w in result['warnings']])
                self.clip.find(path).set('Value', old)
        self.clip.find('IsWarped').set('Value', 'true')
        value(ET.SubElement(self.clip, 'GrooveSettings'), 'GrooveId', 1)
        self.assertEqual(query_set_region(self.summary()['handle'], 2, 1)['clips'][0]['source_interval']['status'], 'unknown')

    def test_curved_tempo_seconds_query_unknown(self):
        self.f.tempo_events[1].set('CurveControl1X', '.3')
        result = query_set_region(self.summary()['handle'], 2, 1)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['clips'], [])
        self.assertIn('Curved', result['reason'])

    def test_missing_active_media_keeps_coordinates_not_native_proof(self):
        (self.directory / 'audio.wav').unlink()
        summary = self.summary()
        result = query_set_region(summary['handle'], 2, 1)
        self.assertEqual(result['clips'][0]['source_interval']['status'], 'unknown')
        self.assertEqual(result['coverage']['runtime_filesystem_references']['missing'], 1)
        self.assertEqual(result['clips'][0]['source']['native_loaded_media'], 'not_verified')
        sf.write(self.directory / 'audio.wav', np.zeros((100, 2)), 8000)
        with self.assertRaisesRegex(PocketError, 'Stale set handle: dependency changed'):
            find_clips(summary['handle'])

    def test_stale_set_content_and_replaced_set_rejected(self):
        handle = self.summary()['handle']
        before = self.path.stat()
        self.path.write_bytes(self.before[:-1] + bytes([self.before[-1] ^ 1]))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(PocketError, 'Stale set handle'):
            query_set_region(handle, 2, 1)
        self.path.write_bytes(self.before)
        handle = inspect_set_summary(self.path, cache_dir=self.cache)['handle']
        replacement = self.directory / 'replace.als'
        replacement.write_bytes(self.before)
        os.replace(replacement, self.path)
        with self.assertRaisesRegex(PocketError, 'Stale set handle'):
            find_clips(handle)

    def test_source_in_place_change_with_restored_mtime_rejected(self):
        handle = self.summary()['handle']
        audio = self.directory / 'audio.wav'
        st = audio.stat()
        with audio.open('r+b') as stream:
            stream.seek(-4, 2)
            stream.write(b'\0\0\0\0')
        os.utime(audio, ns=(st.st_atime_ns, st.st_mtime_ns))
        with self.assertRaisesRegex(PocketError, 'dependency changed'):
            query_set_region(handle, 2, 1)

    def test_symlink_retarget_is_stale(self):
        audio = self.directory / 'audio.wav'
        target = self.directory / 'target.wav'
        audio.rename(target)
        audio.symlink_to(target)
        handle = self.summary()['handle']
        second = self.directory / 'second.wav'
        second.write_bytes(target.read_bytes())
        audio.unlink()
        audio.symlink_to(second)
        with self.assertRaisesRegex(PocketError, 'dependency changed'):
            find_clips(handle)

    def test_relative_absence_is_visible_and_new_candidate_is_stale(self):
        (self.directory / 'Trial').mkdir()
        original = self.f.save()
        relocated = self.directory / 'Trial' / 'relocated.als'
        relocated.write_bytes(original.read_bytes())
        summary = inspect_set_summary(relocated, cache_dir=self.cache)
        self.assertEqual(summary['coverage']['relative_reference_absence'], 1)
        sf.write(self.directory / 'Trial' / 'audio.wav', np.zeros((100, 2)), 8000)
        with self.assertRaisesRegex(PocketError, 'dependency changed'):
            find_clips(summary['handle'])

    def test_oversize_single_clip_has_no_nonadvancing_cursor(self):
        self.clip.find('Name').set('Value', 'Large fixture name ' * 1000)
        with self.assertRaisesRegex(PocketError, 'Insufficient max_bytes'):
            query_set_region(self.summary()['handle'], 2, 1, max_bytes=2000)

    def test_cache_corruption_and_malformed_handle_rejected(self):
        handle = self.summary()['handle']
        with self.assertRaises(PocketError):
            find_clips({'schema': 'pocket.set-handle/v1'})
        Path(handle['cache_path']).write_bytes(b'wrong')
        with self.assertRaisesRegex(PocketError, 'cache identity mismatch'):
            find_clips(handle)

    def test_unresolved_controls_rollup_and_event_truncation(self):
        second = self.f.track('11')
        self.f.clip(second)
        second.find('DeviceChain/Mixer/Volume/AutomationTarget').set('Id', '1001')
        events = self.track.find('AutomationEnvelopes/Envelopes/AutomationEnvelope/Automation/Events')
        for i in range(5, 12):
            ET.SubElement(events, 'FloatEvent', Id=str(i), Time=str(i), Value='.5')
        result = query_set_region(self.summary()['handle'], 2, 4, max_events_per_lane=3)
        self.assertEqual(result['coverage']['controls']['ambiguous'], 1)
        self.assertEqual(result['coverage']['controls']['unresolved'], 1)
        self.assertTrue(result['truncation']['any'])
        self.assertGreater(result['truncation']['event_lanes_truncated'], 0)
        self.assertIn('automation_target_ambiguous', [w['code'] for w in result['warnings']])

    def test_budget_is_honest_and_arguments_are_strict(self):
        for i in range(11, 24):
            self.f.clip(self.f.track(str(i)))
        handle = self.summary()['handle']
        result = query_set_region(handle, 2, 2, max_bytes=4000)
        self.assertLessEqual(len(json.dumps(result, separators=(',', ':'), ensure_ascii=False).encode()), 4000)
        self.assertTrue(result['truncation']['any'])
        for start, duration in [(-1, 1), ('1:99', 1), (float('nan'), 1), (True, 1), (0, 0), (0, 601)]:
            with self.assertRaises(PocketError):
                query_set_region(handle, start, duration)
        with self.assertRaises(PocketError):
            query_set_region(handle, 0, max_clips=True)
        with self.assertRaises(PocketError):
            find_clips(handle, limit=True)
        self.assertEqual(parse_set_time('1:02:03.5'), 3723.5)


class FrameTests(unittest.TestCase):
    def test_native_source_zero_and_eof_precision_only(self):
        region = source_frame_interval(-.00153 / 48000, 1 + .05 / 48000, 48000, 48000)
        self.assertEqual(region['status'], 'valid')
        self.assertEqual((region['start_frame'], region['end_frame_exclusive']), (0, 48000))
        self.assertEqual(len(region['adjustments']), 2)
        for a, b in [(-.101 / 48000, .5), (.5, 1 + .101 / 48000), (-1, .5)]:
            self.assertEqual(source_frame_interval(a, b, 48000, 48000)['status'], 'out_of_bounds')

    def test_inward_only_and_empty_regions(self):
        region = source_frame_interval(.2 / 8000, 4.9 / 8000, 8000, 8000)
        self.assertEqual((region['start_frame'], region['end_frame_exclusive']), (1, 4))
        self.assertEqual(region['adjustments'], [])
        self.assertEqual(source_frame_interval(.2 / 8000, .9 / 8000, 8000, 8000)['status'], 'empty')
        with self.assertRaises(PocketError):
            source_frame_interval(1, 0, 8000, 8000)


class SecondsHandoffTests(unittest.TestCase):
    def test_large_frame_boundaries_have_verified_analyzer_arguments(self):
        for rate in (8000, 44100, 48000, 96000):
            for first, last in ((0, 17), (2184864, 3720865), (14157170, 14710551), (238517591, 240053590)):
                region = source_frame_interval(first / rate, last / rate, rate, 300000000)
                self.assertEqual(region['analyze_region_frame_args'], {'start_frame': region['start_frame'], 'frames': region['frames']})
                args = region['analyze_region_args']
                self.assertIsNotNone(args)
                start = math.ceil(math.nextafter(args['start_seconds'] * rate, -math.inf))
                end = math.floor(math.nextafter((args['start_seconds'] + args['duration_seconds']) * rate, math.inf))
                self.assertEqual((start, end), (region['start_frame'], region['end_frame_exclusive']))
