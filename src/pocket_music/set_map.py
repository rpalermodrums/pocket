"""Read saved Live 12 intent without changing projects or executing devices."""
from __future__ import annotations

from collections import Counter, defaultdict
import gzip
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import zlib

from .errors import PocketError
from .timing import TempoMap, finite, warp_coordinate

SCHEMA = 'pocket.set-map/v1'
STOCK_STRUCTURAL = frozenset({'Eq8', 'StereoGain', 'Operator', 'Drift', 'Reverb',
                              'Delay', 'AutoFilter', 'Compressor2', 'Limiter',
                              'Gate', 'Saturator', 'AudioEffectGroupDevice',
                              'InstrumentGroupDevice', 'MidiEffectGroupDevice'})
MAX_DEVICES = frozenset({'MxDeviceAudioEffect', 'MxDeviceInstrument', 'MxDeviceMidiEffect'})
WARP_MODES = {0: 'beats', 1: 'tones', 2: 'texture', 3: 'repitch', 4: 'complex', 6: 'complex_pro'}


def _value(node: ET.Element | None, path: str, default=None):
    child = node.find(path) if node is not None else None
    return child.get('Value', default) if child is not None else default


def _scalar(text):
    if text is None:
        return None
    if text in ('true', 'false'):
        return text == 'true'
    try:
        result = float(text)
        return result if math.isfinite(result) else text
    except (TypeError, ValueError):
        return text


def _number(node, path, default=None):
    value = _value(node, path, default)
    return None if value is None else finite(value, path)


def _tree(node):
    if node is None:
        return None
    return {'tag': node.tag, 'attributes': dict(node.attrib),
            'children': [_tree(child) for child in node]}


def _paths(root):
    result = {}
    def walk(node, path):
        result[node] = path
        counts = Counter(child.tag for child in node)
        seen = Counter()
        for child in node:
            ordinal = seen[child.tag]
            seen[child.tag] += 1
            part = child.tag + (f'[{ordinal}]' if counts[child.tag] > 1 else '')
            walk(child, path + '/' + part)
    walk(root, root.tag)
    return result


def _ancestors(node, parents):
    result = []
    while node in parents:
        node = parents[node]
        result.append(node)
    return result


def _warning(code, message, **context):
    return {'code': code, 'message': message, **context}


def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _project_root(path):
    return next((p for p in path.parent.parents if (p / 'Ableton Project Info').is_dir()),
                path.parent) if not (path.parent / 'Ableton Project Info').is_dir() else path.parent


def _dependency(ref, key, parents, paths, set_path, project_root, hash_sources):
    ancestors = _ancestors(ref, parents)
    tags = [n.tag for n in ancestors]
    provenance = any(t in tags for t in ('LastPresetRef', 'SourceContext', 'OriginalFileRef'))
    if provenance:
        kind, required = 'dormant_provenance', False
    elif 'MxPatchRef' in tags:
        kind, required = 'max_patch', True
    elif 'SampleRef' in tags:
        kind, required = 'audio_source', True
    else:
        kind, required = 'unclassified_file_reference', None
    saved = {child.tag: child.get('Value') for child in ref if 'Value' in child.attrib}
    absolute, relative = saved.get('Path', ''), saved.get('RelativePath', '')
    relative_type = saved.get('RelativePathType')
    candidates = []
    # Native type 3 is project-relative. Type 1 is also emitted by the existing
    # offline builder; inspect both set-directory and project-root candidates.
    if relative and relative_type in ('1', '3'):
        for base in (project_root, set_path.parent):
            candidate = (base / relative).resolve()
            if str(candidate) not in [c['path'] for c in candidates]:
                candidates.append({'origin': 'relative', 'path': str(candidate), 'exists': candidate.is_file()})
    if absolute and Path(absolute).is_absolute():
        candidate = Path(absolute).resolve()
        if str(candidate) not in [c['path'] for c in candidates]:
            candidates.append({'origin': 'absolute', 'path': str(candidate), 'exists': candidate.is_file()})
    existing = [c for c in candidates if c['exists']]
    # A conflicting live absolute and relative target is not silently resolved.
    identities = {(Path(c['path']).stat().st_dev, Path(c['path']).stat().st_ino) for c in existing}
    conflict = len(identities) > 1
    resolved = existing[0]['path'] if existing and not conflict else None
    result = {'id': key, 'xml_path': paths[ref], 'kind': kind,
              'runtime_dependency': required, 'saved_reference': saved,
              'candidates': candidates, 'resolved_path': resolved,
              'status': 'ambiguous' if conflict else ('present' if existing else 'missing'),
              'header': None, 'sha256': None}
    if resolved and kind == 'audio_source':
        source_stamp = _stamp(Path(resolved))
        try:
            import soundfile as sf
            info = sf.info(resolved)
            result['header'] = {'frames': info.frames, 'sample_rate': info.samplerate,
                                'channels': info.channels, 'duration_seconds': info.duration,
                                'format': info.format, 'subtype': info.subtype}
        except (RuntimeError, OSError) as error:
            result['header_error'] = str(error)
    if resolved and required and hash_sources:
        from .assets import sha256_file
        result['sha256'] = sha256_file(resolved)
    if resolved and kind == 'audio_source' and _stamp(Path(resolved)) != source_stamp:
        raise PocketError('Audio source changed while reading its header/identity')
    result['asset_id'] = 'sha256:' + result['sha256'] if result['sha256'] else None
    return result


def _targets(root, paths):
    targets = defaultdict(list)
    for node in root.iter():
        for target in node.findall('AutomationTarget'):
            key = target.get('Id')
            targets[key].append({'parameter_path': paths[node],
                                 'manual': _scalar(_value(node, 'Manual')),
                                 'target_id': key})
    return dict(targets)


def _envelope(node, targets, paths):
    key = _value(node, 'EnvelopeTarget/PointeeId')
    events = []
    for event in node.findall('Automation/Events/*'):
        events.append({'type': event.tag, 'time': _scalar(event.get('Time')),
                       'value': _scalar(event.get('Value')),
                       'attributes': dict(event.attrib),
                       'children': [_tree(child) for child in event]})
    matches = targets.get(key, [])
    return {'native_id': node.get('Id'), 'xml_path': paths[node],
            'target_id': key, 'target_status': 'resolved' if len(matches) == 1 else
            ('ambiguous' if matches else 'unresolved'), 'targets': matches,
            'events': events, 'negative_events_preserved': True}


def _tempo(song, targets, paths):
    main = song.find('MainTrack')
    if main is None:
        main = song.find('MasterTrack')
    parameter = main.find('DeviceChain/Mixer/Tempo') if main is not None else None
    manual = _number(parameter, 'Manual')
    target = parameter.find('AutomationTarget') if parameter is not None else None
    key = target.get('Id') if target is not None else None
    lanes = [_envelope(e, targets, paths) for e in main.findall('AutomationEnvelopes/Envelopes/AutomationEnvelope')
             if _value(e, 'EnvelopeTarget/PointeeId') == key] if main is not None else []
    result = {'status': 'supported', 'manual_bpm': manual, 'target_id': key,
              'envelopes': lanes, 'points': [], 'interpolation': 'linear BPM in arrangement beats',
              'reason': None}
    try:
        if len(lanes) > 1:
            raise PocketError('Multiple tempo envelopes target the same parameter')
        events = lanes[0]['events'] if lanes else []
        if lanes and lanes[0]['target_status'] != 'resolved':
            raise PocketError('Tempo target is ambiguous or unresolved')
        for event in events:
            if event['type'] != 'FloatEvent' or event['children'] or set(event['attributes']) - {'Id', 'Time', 'Value'}:
                raise PocketError('Curved or unsupported tempo event shape; raw events retained')
        points = [{'beat': finite(e['time'], 'tempo event time'),
                   'bpm': finite(e['value'], 'tempo event BPM')} for e in events]
        if not points:
            points = [{'beat': 0.0, 'bpm': finite(manual, 'manual tempo')}]
        if (points[0]['beat'] <= -63072000 and not any(p['beat'] == 0 for p in points)
                and len(points) > 1 and points[1]['beat'] > 0
                and points[0]['bpm'] != points[1]['bpm']):
            raise PocketError('Initial sentinel and positive tempo event lack a verified zero anchor')
        TempoMap(points)
        result['points'] = points
    except PocketError as error:
        result.update(status='unsupported', reason=str(error))
    return result


def _devices(track, paths):
    devices = []
    for container in track.iter('Devices'):
        for device in container:
            tag = device.tag
            status = 'stock_parameters_only' if tag in STOCK_STRUCTURAL else (
                'external_max_dsp_not_evaluated' if tag in MAX_DEVICES else 'unsupported_dsp')
            parameters = []
            for node in device.iter():
                if node.find('Manual') is not None:
                    parameters.append({'path': paths[node], 'manual': _scalar(_value(node, 'Manual')),
                                       'automation_targets': [x.get('Id') for x in node.findall('AutomationTarget')]})
            # Direct scalar settings capture EQ type/bands, mode, Q, device on/off,
            # and other configuration without embedding opaque plugin state blobs.
            scalars = [{'path': paths[n], 'value': _scalar(n.get('Value'))} for n in device.iter()
                       if 'Value' in n.attrib and len(n.get('Value', '')) <= 256
                       and not any(t in paths[n] for t in ('LastPresetRef', 'SourceContext', '/FileRef/'))]
            devices.append({'id': paths[device], 'native_id': device.get('Id'), 'class': tag,
                            'on': _scalar(_value(device, 'On/Manual')), 'coverage': status,
                            'parameters': parameters, 'saved_scalar_settings': scalars,
                            'dsp_rendered': False})
    return devices


def _clip(node, track_id, clip_id, scope, paths, deps_by_element, targets):
    loop = {n.tag: _scalar(n.get('Value')) for n in node.findall('Loop/*')}
    audio = node.tag == 'AudioClip'
    sample = node.find('SampleRef')
    ref = sample.find('FileRef') if sample is not None else None
    dep = deps_by_element.get(ref)
    markers = [{'native_id': m.get('Id'), 'source_seconds': finite(m.get('SecTime'), 'warp seconds'),
                'beat': finite(m.get('BeatTime'), 'warp beat')} for m in node.findall('WarpMarkers/WarpMarker')]
    warped = _scalar(_value(node, 'IsWarped')) if audio else None
    result = {'id': clip_id, 'track_id': track_id, 'native_clip_id': node.get('Id'),
              'name': _value(node, 'Name', ''), 'type': 'audio' if audio else 'midi',
              'scope': scope, 'xml_path': paths[node], 'disabled': _scalar(_value(node, 'Disabled', 'false')),
              'start_beat': _number(node, 'CurrentStart'), 'end_beat': _number(node, 'CurrentEnd'),
              'saved_time_attribute': _scalar(node.get('Time')), 'loop': loop,
              'loop_coordinate_unit': 'seconds' if audio and warped is False else 'source_pulses',
              'warped': warped, 'warp_mode': _scalar(_value(node, 'WarpMode')),
              'warp_markers': markers, 'warp_markers_active': warped is True,
              'source_dependency_id': dep['id'] if dep else None,
              'source_identity': {'saved_default_frames': _number(sample, 'DefaultDuration'),
                                  'saved_sample_rate': _number(sample, 'DefaultSampleRate'),
                                  'resolved_path': dep['resolved_path'] if dep else None,
                                  'sha256': dep['sha256'] if dep else None,
                                  'asset_id': dep['asset_id'] if dep else None,
                                  'header': dep['header'] if dep else None} if audio else None,
              'sample_gain_amplitude': _number(node, 'SampleVolume') if audio else None,
              'pitch_semitones': _number(node, 'PitchCoarse') if audio else None,
              'pitch_cents': _number(node, 'PitchFine') if audio else None,
              'clip_fade_enabled': _scalar(_value(node, 'Fade')),
              'fades': _tree(node.find('Fades')),
              'clip_envelopes': [_envelope(e, targets, paths) for e in node.findall('Envelopes/Envelopes/AutomationEnvelope')],
              'groove_id': _value(node, 'GrooveSettings/GrooveId'),
              'tempo_leader': _scalar(_value(node, 'IsSongTempoLeader', 'false'))}
    if audio:
        result['warp_mode_name'] = WARP_MODES.get(result['warp_mode'], 'unknown')
    else:
        # Preserve notes/velocity/probability/release IDs and expression structures.
        result['notes'] = _tree(node.find('Notes'))
        result['note_count'] = len(node.findall('.//MidiNoteEvent'))
    return result


def _mapping_reason(set_map, clip):
    if clip['type'] != 'audio':
        return 'MIDI clip timing is inventoried; no audio-source mapping exists'
    if clip['scope'] != 'arrangement':
        return 'Session clips have no fixed Arrangement placement'
    if clip['loop'].get('LoopOn') is not False:
        return 'Looping or unknown loop state is unsupported for source mapping'
    if clip['loop'].get('StartRelative') != 0:
        return 'Nonzero or unknown StartRelative semantics are not verified'
    if clip['tempo_leader']:
        return 'Clip tempo-leader interaction is not evaluated'
    if clip['groove_id'] not in (None, '-1'):
        return 'Active groove timing is not evaluated'
    if clip['start_beat'] is None or clip['end_beat'] is None or clip['end_beat'] <= clip['start_beat']:
        return 'Missing or invalid Arrangement bounds'
    if clip['loop'].get('LoopStart') is None:
        return 'Missing source start'
    if clip['warped'] is True:
        try:
            warp_coordinate(clip['warp_markers'], 0)
        except PocketError as error:
            return str(error)
    elif clip['warped'] is False:
        if set_map['tempo']['status'] != 'supported':
            return 'Natural source mapping requires supported host tempo'
        if clip['pitch_semitones'] not in (0, None) or clip['pitch_cents'] not in (0, None):
            return 'Unwarped transposition playback-rate semantics are not implemented'
    else:
        return 'Unknown Warp state'
    return None


def _find_clip(set_map, clip_id):
    if isinstance(clip_id, dict):
        matches = [c for c in set_map['clips'] if str(c['native_track_id']) == str(clip_id.get('track_id'))
                   and str(c['native_clip_id']) == str(clip_id.get('clip_id'))]
    else:
        matches = [c for c in set_map['clips'] if c['id'] == clip_id]
    if len(matches) != 1:
        raise PocketError(f'Clip identifier resolves to {len(matches)} instances; use its canonical scoped id')
    return matches[0]


def source_position(set_map: dict, clip_id: str | dict, arrangement_beat: float) -> dict:
    """Map one in-clip Arrangement beat to original seconds, or explicit unknown.

    Includes the exact endpoint for inspection/inversion. This is a saved
    coordinate model, not a sample-exact promise about rendered warp DSP.
    """
    clip = _find_clip(set_map, clip_id)
    beat = finite(arrangement_beat, 'arrangement beat')
    reason = _mapping_reason(set_map, clip)
    if reason:
        return {'status': 'unknown', 'clip_id': clip['id'], 'reason': reason}
    if not clip['start_beat'] <= beat <= clip['end_beat']:
        raise PocketError('Arrangement beat lies outside the clip instance')
    tempo = TempoMap(set_map['tempo']['points']) if set_map['tempo']['status'] == 'supported' else None
    pulse = clip['loop']['LoopStart'] + beat - clip['start_beat'] if clip['warped'] else None
    seconds = warp_coordinate(clip['warp_markers'], pulse, inverse=True) if clip['warped'] else (
        clip['loop']['LoopStart'] + tempo.between(clip['start_beat'], beat))
    header = clip['source_identity']['header']
    inside_source = None if not header else 0 <= seconds <= header['duration_seconds']
    return {'status': 'mapped', 'clip_id': clip['id'], 'arrangement_beat': beat,
            'arrangement_seconds': tempo.beat_to_seconds(beat) if tempo else None,
            'source_seconds': seconds, 'source_pulse': pulse,
            'source_frame_float': seconds * header['sample_rate'] if header else None,
            'within_decoded_source_bounds': inside_source,
            'basis': 'saved warp map' if clip['warped'] else 'Warp-off source clock plus integrated host tempo',
            'dsp_sample_exact': False}


def arrangement_position(set_map: dict, clip_id: str | dict, source_seconds: float) -> dict:
    """Inverse supported source mapping; reject out-of-instance source positions."""
    clip = _find_clip(set_map, clip_id)
    seconds = finite(source_seconds, 'source seconds')
    reason = _mapping_reason(set_map, clip)
    if reason:
        return {'status': 'unknown', 'clip_id': clip['id'], 'reason': reason}
    if clip['warped']:
        beat = clip['start_beat'] + warp_coordinate(clip['warp_markers'], seconds) - clip['loop']['LoopStart']
    else:
        tempo = TempoMap(set_map['tempo']['points'])
        beat = tempo.seconds_to_beat(tempo.beat_to_seconds(clip['start_beat']) + seconds - clip['loop']['LoopStart'])
    if beat < clip['start_beat'] - 1e-9 or beat > clip['end_beat'] + 1e-9:
        raise PocketError('Source position lies outside the selected clip instance')
    return source_position(set_map, clip['id'], min(clip['end_beat'], max(clip['start_beat'], beat)))


def inspect_set(path: str | Path, *, hash_sources: bool = False) -> dict:
    """Inspect one saved gzip/plain-XML Live set. Never modifies files or Live.

    Runtime output contains local paths. Keep those inspection artifacts outside
    a public repository. Source hashing is opt-in; audio headers are read only.
    """
    set_path = Path(path).expanduser().resolve()
    try:
        original_stamp = _stamp(set_path)
        payload = set_path.read_bytes()
        xml = gzip.decompress(payload) if payload[:2] == b'\x1f\x8b' else payload
        if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
            raise PocketError('XML entity declarations are not supported')
        root = ET.fromstring(xml)
    except (OSError, EOFError, zlib.error, ET.ParseError) as error:
        raise PocketError(f'Cannot read saved Ableton set: {error}') from error
    song = root.find('LiveSet')
    if root.tag != 'Ableton' or song is None:
        raise PocketError('Expected an Ableton document containing LiveSet')
    parents = {child: parent for parent in root.iter() for child in parent}
    paths = _paths(root)
    project_root = _project_root(set_path)
    targets = _targets(root, paths)
    dependencies = [_dependency(ref, f'dep:{i}', parents, paths, set_path, project_root, hash_sources)
                    for i, ref in enumerate(root.iter('FileRef'))]
    deps_by_element = dict(zip(root.iter('FileRef'), dependencies))
    warnings = []
    for dep in dependencies:
        if dep['runtime_dependency'] is True and dep['status'] != 'present':
            warnings.append(_warning('runtime_dependency_' + dep['status'],
                                     'Potential loaded media/device dependency needs resolution', dependency_id=dep['id']))
        elif dep['runtime_dependency'] is None:
            warnings.append(_warning('unclassified_dependency', 'File reference is retained without claiming load behavior', dependency_id=dep['id']))
        if dep.get('header_error'):
            warnings.append(_warning('unreadable_audio_header', 'Audio reference exists but its decoded header could not be read', dependency_id=dep['id']))
    tracks = list(song.findall('Tracks/*')) + [n for n in song if n.tag in ('MainTrack', 'MasterTrack', 'PreHearTrack')]
    native_track_counts = Counter(t.get('Id', t.tag) for t in tracks)
    result_tracks, clips = [], []
    for index, track in enumerate(tracks):
        native_id = track.get('Id', track.tag)
        track_id = f'track:{native_id}' + (f'@{index}' if native_track_counts[native_id] > 1 else '')
        track_clips = [c for c in track.iter() if c.tag in ('AudioClip', 'MidiClip')]
        local_counts = Counter(c.get('Id') for c in track_clips)
        canonical_ids = []
        for clip_index, node in enumerate(track_clips):
            clip_id = f'{track_id}/clip:{node.get("Id")}' + (f'@{clip_index}' if local_counts[node.get('Id')] > 1 else '')
            ancestors = _ancestors(node, parents)
            tags = {n.tag for n in ancestors}
            scope = 'arrangement' if {'ArrangerAutomation', 'MainSequencer'} <= tags else (
                'freeze_cache' if 'FreezeSequencer' in tags else
                ('session' if 'ClipSlot' in tags else 'unknown'))
            clip = _clip(node, track_id, clip_id, scope, paths, deps_by_element, targets)
            clip['native_track_id'] = native_id
            clips.append(clip)
            canonical_ids.append(clip_id)
        if any(n > 1 for n in local_counts.values()):
            warnings.append(_warning('duplicate_local_clip_id', 'Use canonical instance IDs, not the repeated local ID', track_id=track_id))
        devices = _devices(track, paths)
        for d in devices:
            if d['coverage'] in ('unsupported_dsp', 'external_max_dsp_not_evaluated'):
                warnings.append(_warning(d['coverage'], 'Saved device state does not establish its audible processing', device_id=d['id']))
        lanes = [_envelope(e, targets, paths) for e in track.findall('AutomationEnvelopes/Envelopes/AutomationEnvelope')]
        result_tracks.append({'id': track_id, 'native_id': native_id, 'type': track.tag, 'index': index,
                              'name': _value(track, 'Name/EffectiveName', _value(track, 'Name/UserName', '')),
                              'clip_ids': canonical_ids, 'devices': devices, 'automation': lanes,
                              'mixer': _tree(track.find('DeviceChain/Mixer')),
                              'routing': [_tree(track.find('DeviceChain/' + tag)) for tag in
                                          ('AudioInputRouting', 'AudioOutputRouting', 'MidiInputRouting', 'MidiOutputRouting')],
                              'frozen': _scalar(_value(track, 'Freeze'))})
    tempo = _tempo(song, targets, paths)
    if tempo['status'] != 'supported':
        warnings.append(_warning('unsupported_tempo', tempo['reason']))
    result = {'schema': SCHEMA, 'path': str(set_path), 'filename': set_path.name,
              'sha256': hashlib.sha256(payload).hexdigest(), 'creator': root.get('Creator'),
              'format_attributes': dict(root.attrib), 'project_root': str(project_root),
              'tracks': result_tracks, 'clips': clips, 'dependencies': dependencies,
              'automation_targets': targets, 'tempo': tempo,
              'transport': _tree(song.find('Transport')),
              'locators': [_tree(n) for n in song.findall('Locators/Locators/Locator')],
              'warnings': warnings, 'read_only': True,
              'scope': 'Saved arrangement and device intent; no live unsaved state, audio rendering, audibility or musical bar-one inference.'}
    for clip in clips:
        reason = _mapping_reason(result, clip)
        clip['mapping'] = {'status': 'unknown' if reason else 'supported', 'reason': reason}
        if reason:
            continue
        start = source_position(result, clip['id'], clip['start_beat'])
        end = source_position(result, clip['id'], clip['end_beat'])
        saved_end = clip['loop'].get('LoopEnd')
        saved_end_seconds = warp_coordinate(clip['warp_markers'], saved_end, inverse=True) if (
            clip['warped'] and saved_end is not None) else saved_end
        clip['source_cuts'] = {'start_seconds': start['source_seconds'],
                               'implied_end_seconds': end['source_seconds'],
                               'saved_loop_end_seconds': saved_end_seconds,
                               'implied_minus_saved_end_seconds': None if saved_end_seconds is None else end['source_seconds'] - saved_end_seconds,
                               'start_arrangement_seconds': start['arrangement_seconds'],
                               'end_arrangement_seconds': end['arrangement_seconds'],
                               'note': 'Saved markers and arrangement-implied end are separate observations, not proof of omitted rendered samples.'}
    result['summary'] = {'audio_tracks': sum(t['type'] == 'AudioTrack' for t in result_tracks),
                         'midi_tracks': sum(t['type'] == 'MidiTrack' for t in result_tracks),
                         'arrangement_audio_clips': sum(c['type'] == 'audio' and c['scope'] == 'arrangement' for c in clips),
                         'arrangement_midi_clips': sum(c['type'] == 'midi' and c['scope'] == 'arrangement' for c in clips),
                         'source_hashing_requested': hash_sources, 'warning_count': len(warnings)}
    try:
        if _stamp(set_path) != original_stamp:
            raise PocketError('Saved set changed during inspection; retry after saving completes')
    except OSError as error:
        raise PocketError(f'Saved set became unavailable during inspection: {error}') from error
    result['source_stability'] = 'Set device/inode/size/mtime unchanged during inspection; set hash binds the read bytes.'
    return result
