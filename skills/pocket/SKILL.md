---
name: pocket
description: Inspect exact recordings and Ableton projects, then make bounded transition experiments with scoped listening feedback.
---

# Pocket workflow

Use this skill for Track Map, Set Map and Transition Lab. Read the provider documentation when an operation is unfamiliar.

1. Identify the exact saved project and recording version. Preserve the baseline. Start with MCP `inspect_set` or CLI `set-map`; retain the summary's immutable handle. The library's `inspect_set` still returns a full map. Keep caches and full exports outside Git.
2. Use `find_clips` and `query_set_region` for a timestamp and bounded duration. Inspect relevant controls, supported source mappings, omissions and pagination. Reinspect when a handle is stale; never silently reuse the old map. Export heavy raw state explicitly when needed. Source frames, source seconds, arrangement seconds and arrangement beats are distinct clocks.
3. For each valid audio interval, pass its `analyze_region_frame_args` directly to `analyze_region` with the exact source path. In the CLI use `track-map --start-frame N --frames N`; do not convert back through decimal seconds or mix addressing modes. An unknown or out-of-bounds mapping must stay unknown until verified. Review competing acoustic phases, `crop_stability` and `phase_count_continuity` alongside tempo drift and local tonal evidence. Stable tempo or stable tested crops cannot establish musical beat one or an integer count. A score never authorizes a clip move.
4. Test a small musical hypothesis. Change one aspect where possible: media position, fine timing, tonal overlap or the phrase handoff approach. Name every additional change. Use identical monitoring gain unless level is explicitly the experiment.
5. Compare exact excerpts from the relevant renders. No hidden fades, normalization, resampling or crop widening. Keep output and parent hashes with the source windows.
6. Record the listener's correction about the specific variant, interval and aspect. Use `query_feedback` (CLI `lab feedback-list --spec ...`) to retrieve claims by exact output hash, local frame interval and scope. Preserve the original ranges, text and conflicting claims; an overlap match does not extend approval. Approval of bar position does not approve timing, harmony or the full mix.

The native trial adapter prepares only its declared limited edit in a new destination. Version 2 collects active audio and supported Max files and records their hashes and reference rewrites. Run `validate_native_trial` after relocation. Filesystem readiness cannot prove Live loaded the media: explicitly inspect Live's missing/external-file state before exporting. Check exact range and settings, wait for completion, then attach the actual float WAV with a timestamped observation bound to the candidate hash. Never fabricate these observations. The provider labels them as operator reports.

`similar_phase_at_tested_midpoints` means only that the crop fits agreed at those instants. It does not check their boundary drift. Full Track Maps can be much larger than timestamp queries; store them locally and bring only the relevant fields into a decision.

Read artifact validity, signal disposition, native loading, export observation and `ready_to_compare` separately. Silent or near-silent expected music, non-finite samples and sample overload cannot become ready through a successful export alone. Intentional silence requires an explicit expectation and note at preparation. Readiness still carries no listening judgment. Do not overwrite an immutable candidate when Live wants to normalize it—save a separate file and preserve the chain of evidence. Re-preparing from a relocated source with stale absolute references currently requires deliberate relinking; old v1 native trials must remain preserved and be freshly prepared as v2.

Prefer existing local recordings. Model downloads, new acquisition, general project editing and public sharing are outside the initial three-tool workflow. Keep generated outputs, recordings, personal notes, credentials and machine paths outside Git. Publishing the repository remains a separate owner decision.
