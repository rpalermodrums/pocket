"""Bounded capability discovery. Descriptors never dispatch an operation."""
from __future__ import annotations

import importlib.util
import os

from .artifact_store import digest, receipt
from .errors import PocketError

# Public name, provider module, domain, operation, read-only, summary.
PUBLIC_CAPABILITIES = (
    ('request_status', 'artifact_store', 'artifact', 'status', True, 'Inspect request journals and retained artifact integrity without redispatching.'),
    ('material_import', 'material', 'material', 'import', False, 'Retain source bytes and project supported MIDI material.'),
    ('material_query', 'material', 'material', 'query', True, 'Bounded summaries, exact selections and semantic differences.'),
    ('material_sequence', 'material_sequence', 'material', 'sequence', False, 'Construct declared whole-clip note occurrences with retained parents and explicit clock/placement.'),
    ('material_structure', 'material_structure', 'material', 'structure', False, 'Attributed phrase and motif relationships bound to exact material revisions.'),
    ('midi_analyze', 'midi_analysis', 'midi', 'analyze', True, 'Symbolic statistics, declared-voice intervals and role gate relationships.'),
    ('midi_generate', 'midi_generate', 'midi', 'generate', False, 'Deterministic explicit patterns, alternatives and no addition.'),
    ('midi_develop', 'midi_develop', 'midi', 'develop', False, 'Bounded explicit motif repetitions and seeded endpoint alternatives through public providers.'),
    ('midi_arrangement_develop', 'arrangement_develop', 'midi', 'arrangement_develop', False, 'Develop explicit whole-clip sections from an attributed graph, retaining exact locks and unchanged alternatives.'),
    ('midi_arrangement_query', 'arrangement_develop', 'midi', 'arrangement_query', True, 'Query revision-bound sections and sparse changes with verified source and identity proofs.'),
    ('midi_timing_alternatives', 'midi_timing_alternatives', 'midi', 'timing_alternatives', False, 'Compose explicitly matched authored audio evidence and declared clocks through exact public note shifts; retain unchanged music.'),
    ('midi_timing_query', 'midi_timing_alternatives', 'midi', 'timing_query', True, 'Query bounded timing proofs after isolated public-edit replay without source audio or a model runtime.'),
    ('midi_lifecycle', 'midi_lifecycle', 'midi', 'lifecycle', False, 'Declared symbolic note/sustain/tail reservations; no receiver or allocation qualification.'),
    ('midi_transform', 'midi_edit', 'midi', 'transform', False, 'Literal notes, split/merge, selected edits, explicit grid/groove timing, dynamics and attributed revoicing with exact locks.'),
    ('midi_export', 'midi_io', 'midi', 'export', False, 'SMF derivative with explicit fidelity, raw-event policy and optional exact CC1/CC11 steps.'),
    ('midi_expression_plan', 'midi_expression', 'expression', 'plan', False, 'Declared member-channel allocation and exact expression bytes; receiver setup remains unverified.'),
    ('audio_hypotheses', 'audio_hypotheses', 'audio', 'hypotheses', False, 'Retain exact local source frames and competing deterministic Peek evidence without transcription.'),
    ('audio_region_capture', 'audio_regions', 'audio', 'region_capture', False, 'Stream-verify a declared long PCM source and retain exact bounded crop bytes with original-frame mapping.'),
    ('audio_region_hypotheses', 'audio_region_analysis', 'audio', 'region_hypotheses', False, 'Compose explicit region capture and public analysis, retaining local evidence and exact original-frame mapping.'),
    ('audio_region_query', 'audio_region_analysis', 'audio', 'region_query', True, 'Read bounded local hypotheses and original-frame projections without reanalysis or an optional model runtime.'),
    ('audio_region_correct', 'audio_region_corrections', 'audio', 'region_correct', False, 'Compose attributed corrections in explicitly declared crop-local or original-recording coordinates, preserving raw evidence.'),
    ('audio_region_submit', 'audio_hypothesis_jobs', 'audio', 'region_submit', False, 'Establish durable worker ownership before long-source capture and run the same public mapped analysis.'),
    ('audio_model_inspect', 'audio_pulse_hypotheses', 'audio', 'model_inspect', False, 'Inspect an explicit optional local model runtime and checkpoint; optional bounded synthetic qualification, no downloads.'),
    ('audio_pulse_hypotheses', 'audio_pulse_hypotheses', 'audio', 'pulse_hypotheses', False, 'Retain uncertain learned pulses with exact source frames and explicit qualified local model provenance.'),
    ('audio_pulse_submit', 'audio_hypothesis_jobs', 'audio', 'pulse_submit', False, 'Submit the same optional learned-pulse primitive with inherited execution ownership and cooperative cancellation.'),
    ('audio_note_model_inspect', 'audio_note_hypotheses', 'audio', 'note_model_inspect', False, 'Inspect an explicit optional known-weight ONNX CPU runtime; bounded synthetic qualification, no downloads.'),
    ('audio_note_hypotheses', 'audio_note_hypotheses', 'audio', 'note_hypotheses', False, 'Retain uncertain note candidates with complete window tensors, source-frame envelopes and explicit model provenance.'),
    ('audio_note_submit', 'audio_note_jobs', 'audio', 'note_submit', False, 'Run the same optional note primitive with inherited execution ownership and revision-checked cancellation.'),
    ('audio_hypothesis_correct', 'audio_hypotheses', 'audio', 'correct', False, 'Attributed immutable corrections retaining original hypotheses and uncertainty.'),
    ('audio_hypothesis_query', 'audio_hypotheses', 'audio', 'query', True, 'Bounded revision-bound audio evidence, annotation and correction retrieval.'),
    ('audio_hypothesis_submit', 'audio_hypothesis_jobs', 'audio', 'submit', False, 'Snapshot local audio and submit the same primitive to an optional owned POSIX worker.'),
    ('job_status', 'audio_hypothesis_jobs', 'job', 'status', False, 'Inspect an analysis job and reconcile a released execution lease without retry.'),
    ('job_cancel', 'audio_hypothesis_jobs', 'job', 'cancel', False, 'Request cancellation at the next analysis boundary using an exact job revision.'),
    ('curve_transform', 'curves', 'expression', 'transform', False, 'File-only control curves; native execution separately gated.'),
    ('musical_time', 'time_maps', 'time', 'map', False, 'Exact declared step-tempo clocks, meter displays and independent local cycles.'),
    ('instrument_inspect', 'instruments.core', 'instrument', 'inspect', False, 'Bounded installation and attributed state inspection.'),
    ('instrument_parameters', 'instruments.core', 'instrument', 'parameter_read', True, 'Query captured exposed descriptors without loading a patch.'),
    ('preset_catalog', 'instruments.core', 'instrument', 'catalog', False, 'Hash and query opaque local presets without loading them.'),
    ('sound_plan', 'instruments.core', 'instrument', 'plan', False, 'Bounded sound alternatives requiring no MIDI or DAW.'),
    ('serum_inspect', 'instruments.serum', 'serum', 'inspect', False, 'Serum product/build/format inventory and attributed state; native verification remains separate.'),
    ('serum_presets', 'instruments.serum', 'serum', 'catalog', False, 'Opaque Serum catalog; extensions alone do not prove compatibility.'),
    ('serum_plan', 'instruments.serum', 'serum', 'plan', False, 'Serum-specific planning with unavailable routes declared.'),
    ('build_native_midi_device', 'native_midi', 'native', 'build_device', False, 'Build the separate read-only Max device without running a host.'),
    ('build_native_midi_writer', 'native_midi', 'native', 'build_writer_device', False, 'Build the separate guarded writer package; building does not authorize or execute edits.'),
    ('native_midi_read', 'native_midi', 'native', 'note_read', False, 'Bounded native notes with explicit target, retained evidence and optional saved binding.'),
    ('native_midi_status', 'native_midi', 'native', 'request_status', False, 'Inspect a native request journal without dispatching or implying current state.'),
    ('native_midi_write', 'native_midi', 'native', 'note_write', False, 'Insert up to three ordinary notes or change one proven note velocity in a supervised isolated stock fixture.'),
    ('candidate_prepare', 'native_candidates', 'candidate', 'prepare', False, 'Copy protected parent and dependencies into an isolated workspace.'),
    ('candidate_inspect', 'native_candidates', 'candidate', 'inspect', True, 'Inspect revision-bound workspace and source integrity.'),
    ('candidate_cancel', 'native_candidates', 'candidate', 'cancel', False, 'Cancel an isolated workspace without deleting its evidence.'),
    ('candidate_native_abandon', 'native_candidates', 'candidate', 'abandon_native', False, 'Permanently abandon an unknown native attempt after explicit supervised closure and fresh evidence.'),
    ('candidate_native_reconcile', 'native_candidates', 'candidate', 'reconcile_native', False, 'Reconcile a validated terminal with its exact pending workspace and release only its matching lease; never redispatch.'),
    ('candidate_seal', 'native_candidates', 'candidate', 'seal', False, 'Verify source preservation and attributed native save/reopen evidence.'),
    ('validate_candidate', 'native_candidates', 'candidate', 'validate', True, 'Recheck sealed v3 identities and preservation evidence.'),
    ('audition_plan', 'auditions', 'audition', 'plan', False, 'Bind candidates and an unchanged baseline to explicit render settings.'),
    ('attach_candidate_render', 'auditions', 'audition', 'attach_render', False, 'Verify decoded audio and bind attributed render evidence to a candidate.'),
    ('audition_feedback', 'auditions', 'audition', 'feedback', False, 'Record attributed, interval-scoped listening feedback separately from measurements.'),
    ('audition_feedback_query', 'feedback_query', 'audition', 'feedback_query', True, 'Retrieve explicitly supplied feedback with exact render, interval and actor identity; no preference inference.'),
    ('promote_candidate', 'auditions', 'candidate', 'promote', False, 'Copy an explicitly kept candidate and seal its collected dependency lineage.'),
    ('validate_candidate_promotion', 'auditions', 'candidate', 'validate_promotion', True, 'Recheck relocated v3 promotion bytes, dependencies and evidence.'),
)

_UNAVAILABLE = (
    ('native_midi_cancel', 'native', 'cancel_write', 'Prepared-only cancellation implemented in the adapter; actual native cancellation qualification pending'),
    ('instrument_parameter_set', 'instrument', 'parameter_write', 'E3 native parameter identity/write probe'),
    ('instrument_state_capture', 'instrument', 'state_capture', 'Qualified non-destructive native capture route'),
    ('instrument_state_restore', 'instrument', 'state_restore', 'E3/E6 exact state restore probe'),
    ('preset_load', 'instrument', 'preset_load', 'Licensed installation and E3/E6'),
    ('preset_save', 'instrument', 'preset_save', 'Licensed installation and E3/E6'),
    ('native_curve_read', 'native', 'curve_read', 'E4 native curve inspection probe'),
    ('native_curve_write', 'native', 'curve_write', 'E4 editable curve persistence and ownership'),
    ('gesture_capture_start', 'expression', 'capture_start', 'E4/E5 owned recording and clock calibration'),
    ('gesture_capture_stop', 'expression', 'capture_stop', 'E4/E5 owned recording and recovery'),
    ('native_save_checkpoint', 'native', 'save', 'Qualified automatic save/reopen route; supervised evidence supported'),
    ('native_render', 'native', 'render', 'Qualified automatic export route; supervised rendering supported'),
    ('candidate_apply', 'candidate', 'apply', 'Qualified individually callable native primitives'),
    ('audio_transcribe', 'audio', 'transcribe', 'General transcription and musician-owned E8 acceptance; narrow optional note hypotheses are separately callable'),
    ('performance_launch', 'performance', 'launch', 'E9 scheduling, takeover, panic and 30-minute soak'),
)

# These are semantic artifact families, distinct from the common handle envelope.
# An empty return list means the operation returns ordinary versioned data only.
_HANDLE_FAMILIES = {
    'request_status': ([], []),
    'material_import': ([], ['pocket.material/v1']),
    'material_query': (['pocket.material/v1'], []),
    'material_sequence': (['pocket.material/v1'], ['pocket.material/v1', 'pocket.material-sequence/v1']),
    'material_structure': (['pocket.material/v1', 'pocket.material-structure/v1'], ['pocket.material-structure/v1']),
    'midi_analyze': (['pocket.material/v1'], []),
    'midi_generate': ([], ['pocket.material/v1']),
    'midi_develop': (['pocket.material/v1'], ['pocket.material/v1', 'pocket.motif-development/v1']),
    'midi_arrangement_develop': (['pocket.material-structure/v1'],
                               ['pocket.material/v1', 'pocket.material-structure/v1', 'pocket.arrangement-development/v1']),
    'midi_arrangement_query': (['pocket.arrangement-development/v1'], ['pocket.arrangement-development/v1']),
    'midi_timing_alternatives': (['pocket.material/v1', 'pocket.audio-region-hypotheses/v1', 'pocket.time-map/v1'],
                               ['pocket.midi-timing-alternatives/v1', 'pocket.material/v1']),
    'midi_timing_query': (['pocket.midi-timing-alternatives/v1'],
                          ['pocket.midi-timing-alternatives/v1', 'pocket.material/v1', 'pocket.edit/v1']),
    'midi_lifecycle': (['pocket.material/v1'], ['pocket.midi-lifecycle/v1']),
    'midi_transform': (['pocket.material/v1'], ['pocket.material/v1', 'pocket.edit/v1']),
    'midi_export': (['pocket.material/v1', 'pocket.curve/v1', 'pocket.midi-expression-plan/v1'],
                    ['pocket.smf-derivative/v1', 'pocket.midi-export/v1', 'pocket.material/v1']),
    'midi_expression_plan': (['pocket.material/v1', 'pocket.midi-lifecycle/v1'], ['pocket.midi-expression-plan/v1']),
    'audio_hypotheses': ([], ['pocket.audio-hypotheses/v1']),
    'audio_region_capture': ([], ['pocket.audio-region-capture/v1']),
    'audio_region_hypotheses': (['pocket.audio-region-capture/v1', 'pocket.audio-model/v1', 'pocket.audio-note-model/v1'], ['pocket.audio-region-hypotheses/v1']),
    'audio_region_query': (['pocket.audio-region-hypotheses/v1'],
                           ['pocket.audio-region-hypotheses/v1', 'pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'audio_region_correct': (['pocket.audio-region-hypotheses/v1'],
                             ['pocket.audio-region-hypotheses/v1', 'pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'audio_region_submit': (['pocket.audio-region-capture/v1', 'pocket.audio-model/v1', 'pocket.audio-note-model/v1'], ['pocket.audio-region-capture/v1']),
    'audio_model_inspect': ([], ['pocket.audio-model/v1']),
    'audio_pulse_hypotheses': (['pocket.audio-model/v1'], ['pocket.audio-model-hypotheses/v1']),
    'audio_pulse_submit': (['pocket.audio-model/v1'], ['pocket.audio-source-bytes/v1']),
    'audio_note_model_inspect': ([], ['pocket.audio-note-model/v1']),
    'audio_note_hypotheses': (['pocket.audio-note-model/v1'], ['pocket.audio-note-hypotheses/v1']),
    'audio_note_submit': (['pocket.audio-note-model/v1'], ['pocket.audio-source-bytes/v1']),
    'audio_hypothesis_correct': (['pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1'],
                               ['pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'audio_hypothesis_query': (['pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1'],
                             ['pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'audio_hypothesis_submit': ([], ['pocket.audio-source-bytes/v1']),
    'job_status': ([], ['pocket.audio-source-bytes/v1', 'pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1',
                       'pocket.audio-region-capture/v1', 'pocket.audio-region-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'job_cancel': ([], ['pocket.audio-source-bytes/v1', 'pocket.audio-hypotheses/v1', 'pocket.audio-model-hypotheses/v1',
                       'pocket.audio-region-capture/v1', 'pocket.audio-region-hypotheses/v1', 'pocket.audio-note-hypotheses/v1']),
    'curve_transform': (['pocket.curve/v1', 'pocket.context/v1'], ['pocket.curve/v1', 'pocket.curve-edit/v1']),
    'musical_time': (['pocket.time-map/v1'], ['pocket.time-map/v1']),
    'instrument_inspect': ([], ['pocket.instrument-state/v1', 'pocket.instrument-inventory/v1', 'pocket.environment/v1']),
    'instrument_parameters': (['pocket.instrument-state/v1'], ['pocket.instrument-state/v1']),
    'preset_catalog': (['pocket.preset-catalog/v1'], ['pocket.preset-catalog/v1']),
    'sound_plan': (['pocket.instrument-state/v1', 'pocket.material/v1', 'pocket.context/v1'], ['pocket.sound-plan/v1']),
    'serum_inspect': ([], ['pocket.instrument-state/v1', 'pocket.instrument-inventory/v1', 'pocket.environment/v1']),
    'serum_presets': (['pocket.preset-catalog/v1'], ['pocket.preset-catalog/v1']),
    'serum_plan': (['pocket.instrument-state/v1', 'pocket.material/v1', 'pocket.context/v1'], ['pocket.sound-plan/v1']),
    'build_native_midi_device': ([], ['pocket.native-midi-device/v1']),
    'build_native_midi_writer': ([], ['pocket.native-midi-device/v1']),
    'native_midi_read': (['pocket.native-midi-observation/v1'], ['pocket.native-midi-observation/v1']),
    'native_midi_status': ([], ['pocket.native-midi-bridge-journal/v1', 'pocket.native-midi-write-journal/v1',
                               'pocket.native-midi-terminal/v1']),
    'native_midi_write': (['pocket.native-midi-device/v1', 'pocket.native-midi-observation/v1',
                           'pocket.native-midi-terminal/v1'],
                          ['pocket.native-midi-terminal/v1', 'pocket.native-midi-observation/v1',
                           'pocket.native-midi-write-journal/v1', 'pocket.native-pending/v1']),
    'candidate_prepare': (['pocket.material/v1'], ['pocket.candidate-preparation/v1', 'pocket.saved-set/v1']),
    'candidate_inspect': ([], ['pocket.candidate-preparation/v1', 'pocket.native-pending/v1',
                              'pocket.native-trial/v3', 'pocket.native-midi-terminal/v1',
                              'pocket.native-abandonment/v1']),
    'candidate_cancel': ([], ['pocket.candidate-preparation/v1']),
    'candidate_native_abandon': (['pocket.native-pending/v1', 'pocket.native-midi-observation/v1'],
                                 ['pocket.native-abandonment/v1', 'pocket.native-pending/v1']),
    'candidate_native_reconcile': (['pocket.native-midi-terminal/v1'],
                                   ['pocket.native-midi-terminal/v1', 'pocket.native-pending/v1']),
    'candidate_seal': (['pocket.instrument-state/v1', 'pocket.material/v1', 'pocket.native-midi-observation/v1'],
                       ['pocket.native-trial/v3', 'pocket.saved-set/v1', 'pocket.candidate-material-amendment/v1']),
    'validate_candidate': (['pocket.native-trial/v3'], ['pocket.native-trial/v3', 'pocket.saved-set/v1']),
    'audition_plan': (['pocket.native-trial/v3'], ['pocket.audition-plan/v1']),
    'attach_candidate_render': (['pocket.native-trial/v3', 'pocket.audition-plan/v1'],
                                ['pocket.render-attachment/v3', 'pocket.render-audio/v1']),
    'audition_feedback': (['pocket.render-attachment/v3'], ['pocket.audition-feedback/v1']),
    'audition_feedback_query': (['pocket.audition-feedback/v1'], ['pocket.audition-feedback/v1', 'pocket.native-trial/v3', 'pocket.render-attachment/v3']),
    'promote_candidate': (['pocket.native-trial/v3', 'pocket.render-attachment/v3', 'pocket.audition-feedback/v1'],
                          ['pocket.promotion/v2']),
    'validate_candidate_promotion': ([], []),
}


def capabilities_list(domain: str | None = None, operation: str | None = None,
                      host_profile: dict | None = None, limit: int = 12,
                      cursor: str | None = None) -> dict:
    """Discover callable tools and explicit closed gates without starting a host.

    Host profiles are supplied context, never authority to enable an unverified
    native route. Availability of file tools is independent of Live or Serum.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise PocketError('limit must be 1–50')
    if any(x is not None and (not isinstance(x, str) or len(x) > 80) for x in (domain, operation)):
        raise PocketError('Filters must be bounded strings')
    if host_profile is not None and not isinstance(host_profile, dict):
        raise PocketError('host_profile must be a profile record')
    mido = importlib.util.find_spec('mido') is not None
    rows = []
    for name, module, area, op, read_only, summary in PUBLIC_CAPABILITIES:
        status = 'available'
        prerequisites = []
        profile = None
        side_effects = [] if read_only else ['new_local_artifacts']
        if name == 'midi_timing_query':
            side_effects = ['temporary_local_proof_replay; caller_store_unchanged']
        if name in ('audio_region_hypotheses', 'audio_region_submit'):
            side_effects += ['conditional_owned_local_model_process']
        if name in ('audio_model_inspect', 'audio_pulse_hypotheses', 'audio_pulse_submit'):
            prerequisites = ['Explicit local executable/checkpoint SHA256 declarations; optional BeatThis CPU runtime; no downloads or DAW']
            if name != 'audio_model_inspect':
                status = 'requires_optional_setup'
                profile = 'beat_this_cpu_v1/synthetic_cpu_v1'
                prerequisites += ['Validated synthetic qualification and 2–20 second mono/stereo PCM16 WAV crop at 8000/44100/48000 Hz; accuracy unestablished']
            side_effects += ['owned_local_model_process']
        if name in ('audio_note_model_inspect', 'audio_note_hypotheses', 'audio_note_submit'):
            status = 'requires_optional_setup'
            profile = 'basic_pitch_onnx_cpu_v1/synthetic_onnx_cpu_v1'
            prerequisites = ['Explicit local executable/known ONNX checkpoint SHA256 and isolated ONNX CPU/soxr runtime; no downloads or DAW',
                             'Two-case silence/tone synthetic check covers finite/repeatable execution, not musical accuracy or E8 acceptance']
            if name != 'audio_note_model_inspect':
                prerequisites += ['Qualified inspected model or explicit inline qualification; 2–20 second mono/stereo PCM16 or IEEE FLOAT32 WAV at 22050/44100/48000 Hz']
            side_effects += ['owned_local_model_process']
        if name in ('audio_hypothesis_submit', 'audio_pulse_submit', 'audio_note_submit', 'audio_region_submit', 'job_status', 'job_cancel'):
            if name not in ('audio_pulse_submit', 'audio_note_submit'):
                prerequisites = ['POSIX OS execution leases; local Python audio environment; retained status needs no model or DAW']
            else:
                prerequisites += ['POSIX OS execution leases']
            if os.name != 'posix':
                status = 'unsupported'
            side_effects += ['local_job_journal']
            if name in ('audio_hypothesis_submit', 'audio_pulse_submit', 'audio_note_submit', 'audio_region_submit'):
                side_effects += ['owned_local_background_process']
        if name == 'native_midi_read':
            status = 'requires_native_setup'
            prerequisites = ['Loaded Pocket Native MIDI Max device for fresh observations; retained artifact queries need no host']
        if name == 'candidate_native_abandon':
            prerequisites = ['Explicit closure of the old native session, unchanged quarantined files and a fresh observation of a different stopped project']
            side_effects += ['permanent_workspace_abandonment', 'matching_host_lease_release']
        if name == 'candidate_native_reconcile':
            prerequisites = ['Validated terminal for the exact pending workspace; unknown outcomes cannot be reconciled as success']
            side_effects += ['workspace_revision_update', 'matching_host_lease_release']
        if name == 'native_midi_write':
            status = 'requires_native_setup'
            profile = 'live-12.4.5-supervised-ordinary-notes/v1'
            prerequisites = ['Exact bundled writer package, isolated prepared workspace, bound native observation, stopped 120 BPM/4/4 stock Operator fixture and explicit session supervision']
            side_effects += ['native_owned_clip_notes', 'workspace_revision_update', 'durable_host_lease']
        if name == 'midi_export' and not mido:
            status, prerequisites = 'unsupported', ['Install the optional Pocket midi extra (file I/O only)']
        rows.append({'capability_id': f'pocket.{name}/v1', 'public_tool': name,
                     'provider': f'pocket_music.{module}.{name}', 'domain': area, 'operation': op,
                     'input_schema': f'pocket.{name}-input/v1',
                     'output_schema': {'validate_candidate_promotion': 'pocket.promotion-validation/v2',
                                       'midi_timing_query': 'pocket.midi-timing-query/v1'}.get(
                                           name, 'pocket.operation-receipt/v1'),
                     'accepted_handles': (['pocket.artifact-handle/v1'] if _HANDLE_FAMILIES[name][0] or name == 'promote_candidate' else [])
                                         + (['pocket.set-handle/v1'] if name == 'material_import' else []),
                     'accepted_artifact_schemas': list(_HANDLE_FAMILIES[name][0]),
                     'returned_artifact_schemas': list(_HANDLE_FAMILIES[name][1]),
                     'additional_versioned_handles': (['pocket.set-handle/v1'] if name == 'material_import' else
                         ['pocket.job-handle/v1'] if name in ('audio_hypothesis_submit', 'audio_pulse_submit', 'audio_note_submit', 'audio_region_submit', 'job_status', 'job_cancel') else []),
                     'artifact_schema_scope': 'Direct public inputs and primary outputs; nested evidence retains its own schemas',
                     'additional_artifact_schemas': 'Any validated artifact family for related_artifacts' if name == 'promote_candidate' else None,
                     'summary': summary, 'side_effects': side_effects,
                     'required_profile': profile, 'implementation_status': 'implemented',
                     'status': status, 'prerequisites': prerequisites,
                     'conditional_dependencies': (['Optional midi extra for SMF sources'] if name == 'material_import' else
                         ['learned_pulse requires explicit qualified local BeatThis; learned_notes requires explicit qualified known-weight ONNX CPU runtime; Peek needs no model']
                         if name in ('audio_region_hypotheses', 'audio_region_submit') else []),
                     'native_verified': False})
    for name, area, op, gate in _UNAVAILABLE:
        rows.append({'capability_id': f'pocket.{name}/v1', 'public_tool': None,
                     'proposed_tool': name, 'domain': area, 'operation': op,
                     'input_schema': None, 'output_schema': None, 'accepted_handles': [],
                     'accepted_artifact_schemas': [], 'returned_artifact_schemas': [],
                     'additional_versioned_handles': [], 'additional_artifact_schemas': None,
                     'artifact_schema_scope': 'Unqualified; no callable contract advertised',
                     'summary': gate, 'side_effects': [], 'required_profile': gate,
                     'implementation_status': 'implemented_unqualified' if name == 'native_midi_cancel' else 'not_implemented',
                     'status': 'documented_unprobed',
                     'native_verified': False})
    rows = [row for row in rows if (domain is None or row['domain'] == domain)
            and (operation is None or row['operation'] == operation)]
    revision = digest({'rows': rows, 'host_profile': host_profile})
    start = 0
    if cursor is not None:
        try:
            identity, raw_start = cursor.split(':')
            start = int(raw_start)
        except (ValueError, AttributeError) as error:
            raise PocketError('Invalid capability cursor') from error
        if identity != revision or not 0 <= start <= len(rows):
            raise PocketError('Stale or mismatched capability cursor')
    end = min(start + limit, len(rows))
    return receipt(capabilities=rows[start:end], total=len(rows), omitted=len(rows) - (end-start),
                   next_cursor=f'{revision}:{end}' if end < len(rows) else None,
                   catalog_sha256=revision, host_profile_sha256=digest(host_profile) if host_profile else None,
                   coverage={'runtime_native_support': 'unverified', 'discovery_executes_tools': False})
