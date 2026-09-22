# Independent MIDI tools and an optional layer recipe

These examples use synthetic material. They do not contain music, presets, native projects, real listening notes or machine-specific evidence. No Live or Serum installation is needed for the file examples. The stock sound and curve outputs are plans, not applied sounds or recorded performances.

## Run the standalone examples

Use Pocket's installed CLI with its optional `midi` extra for the later SMF export. The MCP interface uses the optional `agent` extra. Start from the repository root, copy the input examples into a disposable local directory, and keep generated artifacts there:

```sh
demo="$(mktemp -d)"
cp examples/midi-workflow/*.json "$demo/"
cd "$demo"
pocket capabilities-list --spec discovery.json > discovery-result.json
pocket midi-generate --spec generate.json > generated.json
pocket material-import --spec external-import.json > imported.json
pocket material-structure --spec structure-create.json > structure.json
pocket sound-plan --spec stock-sound.json > sound.json
pocket curve-transform --spec curve-create.json > curve.json
pocket musical-time --spec time-create.json > time.json
```

Each operation is independent. You can run only the sound plan, only the curve example, or only the external import. The external phrase was authored directly as a valid canonical record; it never passes through generation. Its revision is SHA-256 of its canonical UTF-8 JSON, excluding `revision_sha256`, with sorted keys, compact separators, `ensure_ascii=False` and nonfinite numbers disallowed.

`structure-create.json` independently organizes that external phrase into two named spans with an attributed call/response hypothesis. Query its returned `artifacts.structure` with `material_structure(operation="query", section="members", node_ids=["entrance"], ...)` to retrieve the first span's two note identities. Use those IDs with `material_query` on the same material revision, then pass its selection to `midi_transform`. Relations retain the actor's uncertainty and do not infer whether the phrase works musically. The structure contains no native arrangement instructions.

The generated material spans 32 quarter notes. A repeats the literal four-qn cell; B changes only final attacks in later cells within ±1/8 qn. Both leave the first eight qn empty and stop new attacks before qn 28. The result also retains an empty no-addition material. Pitch 60 is a synthetic test choice, not a key or General MIDI drum assertion. The external phrase has a separate four-qn span, three pitches, rational tuplet positions and release velocity 37.

`stock-sound.json` independently proposes the documented Operator fixture. `curve-create.json` creates an explicit channel-1 CC11 step curve. The curve does not change a MIDI file, move a hardware controller or write native automation. In real use, choose the controller target and receiver from verified information.

To explicitly encode clip-local CC11 steps, use the separate `cc-curve-create.json` input with the imported external phrase. This optional composition calls the same public tools and leaves both masters unchanged:

```python
import json
from pathlib import Path

from pocket_music import curve_transform, midi_export

imported = json.loads(Path("imported.json").read_text())
curve = curve_transform(**json.loads(Path("cc-curve-create.json").read_text()))
encoded = midi_export(
    material=imported["material"],
    store_root="./pocket-midi-demo",
    request_id="example-cc-export-v1",
    output_path="external-with-cc11.mid",
    cc_step_bindings=[{
        "curve": curve["artifacts"]["curve"],
        "clip_id": "clip:external",
        "controller": 11,
        "same_tick_order": "before_existing",
    }],
)
Path("cc-export-result.json").write_text(json.dumps(encoded, indent=2) + "\n")
```

The new file contains CC11 values 64, 80 and 64 at quarter notes 0, 2 and 4. Existing notes, attack/release values and their order remain intact. The export sidecar retains the complete curve and source identities; the curve needs no private insertion into the material. This bounded profile supports only CC1/CC11 absolute 7-bit steps, rejects conflicting controller ownership and adds no implicit reset. Native receiver behavior and listening are unverified. The [export contract](../../docs/midi.md#interchange-fidelity) specifies ordering, SMF1 channel ownership and fidelity limits.

`time-create.json` independently creates a declared clock with 90→144→75 BPM steps, a negative pickup domain and an explicit quarter-note/host-second origin. It does not read or change Live's clock. Pass its returned `time_map` to `musical_time(operation="convert")` or `musical_time(operation="query")`; the [public time documentation](../../docs/midi.md) specifies positions, render-frame requirements and bounds.

Reusing a request ID with identical inputs returns the same verified artifact handles. To change an example, choose a new request ID. Export destinations must be new files. An interrupted or failed request retains its journal for inspection; it is not an instruction to blindly repeat a native action.

Use public `request_status(store_root, request_id)` to inspect the journal and completed artifact integrity without redispatching or taking over a lock. Revalidate external saved files through their original provider before continuing.

## Compose the same public calls

Run this Python example from the disposable directory above. It replaces generation with the external material and edits one selected note. All work uses public providers; the snippet only passes their returned handles and selections.

```python
import json
from pathlib import Path

from pocket_music.material import material_import, material_query
from pocket_music.midi_analysis import midi_analyze
from pocket_music.midi_edit import midi_transform
from pocket_music.midi_io import midi_export

arguments = json.loads(Path("external-import.json").read_text())
store = arguments["store_root"]
imported = material_import(**arguments)
original = imported["material"]

queried = material_query(
    material=original,
    store_root=store,
    query="events",
    selection={"note_ids": ["note:external-1"]},
)
edited = midi_transform(
    material=original,
    selection=queried["selection"],
    operations=[{"op": "shift", "delta_qn": {"n": 1, "d": 16}}],
    locks={
        "outside_selection": "all",
        "selected_fields": ["pitch", "velocity", "release_velocity", "duration_qn"],
    },
    store_root=store,
    request_id="example-shift-v1",
)
analysis = midi_analyze(material=edited["material"], store_root=store)
differences = material_query(
    material=original,
    comparison=edited["material"],
    query="diff",
    store_root=store,
)
exported = midi_export(
    material=edited["material"],
    store_root=store,
    request_id="example-export-v1",
    output_path="external-shifted.mid",
)
Path("composed-result.json").write_text(json.dumps({
    "edited": edited,
    "analysis": analysis,
    "differences": differences,
    "exported": exported,
}, indent=2) + "\n")
```

Expected result: one onset changes from 1/3 to 19/48 qn; all pitches, velocities, release velocities, durations and the two unselected notes remain exact. The original four-qn ending remains intact. Default export selects PPQ 336, which represents the example's denominators exactly; no timing approximation is required. The analysis is symbolic and cannot establish whether this change sounds better.

### Explicit timing, dynamics and a later copy

This separate branch uses the same unchanged external material. It requests a half-strength grid adjustment, an exact velocity lookup and a later copy of only the selected note. None of these choices are automatic musical recommendations.

```python
import json
from pathlib import Path

from pocket_music import material_query, midi_analyze, midi_export, midi_transform

original = json.loads(Path("imported.json").read_text())["material"]
store = "./pocket-midi-demo"
selected = material_query(
    material=original, store_root=store, query="events",
    selection={"note_ids": ["note:external-1"]},
)["selection"]
developed = midi_transform(
    material=original, selection=selected, store_root=store,
    request_id="example-selected-development-v1",
    operations=[
        {
            "op": "grid_quantize", "time_space": "clip_qn",
            "grid_qn": {"n": 1, "d": 4}, "phase_qn": 0,
            "strength": {"n": 1, "d": 2},
            "threshold_qn": {"n": 1, "d": 8}, "ties": "earlier",
            "note_off": "follow_onset", "controller_timeline": "preserve_existing",
        },
        {
            "op": "velocity_map", "field": "velocity",
            "mapping": [{"from_value": 80, "to_value": 72}], "unmapped": "reject",
        },
        {"op": "duplicate", "delta_qn": 2, "controller_timeline": "preserve_existing"},
    ],
    locks={"outside_selection": "all", "selected_fields": ["pitch", "release_velocity", "duration_qn"]},
)
relationships = midi_analyze(
    material=developed["material"], store_root=store,
    analyses=["voice_leading", "role_overlap"],
)
exported = midi_export(
    material=developed["material"], store_root=store,
    request_id="example-development-export-v1", output_path="external-developed.mid",
)
deleted = midi_transform(
    material=original, selection=selected, store_root=store,
    request_id="example-selected-deletion-v1",
    operations=[{"op": "delete", "controller_timeline": "preserve_existing"}],
)
Path("development-result.json").write_text(json.dumps({
    "developed": developed, "relationships": relationships,
    "exported": exported, "deleted_alternative": deleted, "unchanged": original,
}, indent=2) + "\n")
```

Expected developed notes: the selected attack moves from 1/3 to 7/24 qn with velocity 72; its new copy starts at 55/24 qn with a distinct ID. Both retain pitch 64, release 37 and a quarter-qn gate. The other two notes stay exact. Export uses exact PPQ 168. The encoded pitch contour is +400, +300, −300 cents within the declared voice. Only one role is declared, so no distinct-role overlap pair is inferred. The deletion is a separate child of the unchanged original, containing its two unselected notes. Neither branch is a musical keep.

The selection remains fixed within each call. Copies are not implicitly targeted by later operations; obtain a fresh query on the child before editing their new identities. See the [edit and relationship contracts](../../docs/midi.md) for expression, tuning, bounds and controller limitations.

To use generation in the same recipe, supply one returned `alternatives` handle to `material_query` and use the generated note IDs from its bounded query. To leave the music alone, retain the original or the generated no-addition choice. Neither path requires a workflow wrapper.

### Construct notes and retain a later phrase occurrence

This branch starts with the unchanged external phrase, authors a literal note in its final empty quarter note, splits that new note into two explicit retriggers, and merges those retriggers in a separate child. It then places the merged phrase twice into a longer declared sequence. The parent and split alternative remain available; no musical preference is asserted.

```python
import json
from pathlib import Path
from pocket_music import material_query, material_sequence, midi_transform, midi_export

store = "./pocket-midi-demo"
original = json.loads(Path("imported.json").read_text())["material"]
empty = material_query(material=original, store_root=store, selection={"note_ids": []})["selection"]
added = midi_transform(
    material=original, selection=empty, store_root=store, request_id="example-literal-add-v1",
    operations=[{"op": "add", "clip_id": "clip:external", "time_space": "clip_qn",
        "controller_timeline": "preserve_existing", "notes": [{
            "onset_qn": 3, "duration_qn": 1,
            "pitch": {"midi_note": 72, "cents_offset": 0, "tuning_ref": "tuning:12tet-a440"},
            "velocity": {"value": 68, "domain": "midi1_7bit"},
            "release_velocity": {"value": 29, "domain": "midi1_7bit"},
            "channel": 1, "mute": False, "voice_id": "voice:answer", "role_ref": "answer",
        }]}],
)
selected = material_query(material=added["material"], store_root=store,
    selection={"note_ids": added["change_summary"]["inserted"]})["selection"]
split = midi_transform(material=added["material"], selection=selected, store_root=store,
    request_id="example-split-v1", operations=[{"op": "split", "offsets_qn": [{"n": 1, "d": 2}],
        "time_space": "note_relative_qn", "controller_timeline": "preserve_existing", "articulation": "retrigger"}])
selected = material_query(material=split["material"], store_root=store,
    selection={"note_ids": split["change_summary"]["inserted"]})["selection"]
merged = midi_transform(material=split["material"], selection=selected, store_root=store,
    request_id="example-merge-v1", operations=[{"op": "merge", "time_space": "clip_qn",
        "controller_timeline": "preserve_existing", "articulation": "remove_retriggers"}])
revision = material_query(material=merged["material"], store_root=store)["selection"]["material_revision"]
sequence = material_sequence(store_root=store, request_id="example-sequence-v1", definition={
    "label": "Phrase, rest, return", "materials": [{"key": "phrase", "material": merged["material"]}],
    "clock": {"schema": "pocket.time-context/v1", "context_id": "clock:example",
              "attribution": "Synthetic example author declares quarter-note placements."},
    "origin": {"space": "phrase_qn", "n": 0, "d": 1}, "length_qn": 12,
    "occurrences": [{"occurrence_id": name, "material_key": "phrase", "material_revision": revision,
        "clip_id": "clip:external", "at_qn": at} for name, at in [("first", 0), ("return", 8)]],
    "controller_policy": "reject_present", "expression_policy": "reject_present",
    "overlap_policy": "reject_same_channel_pitch",
})
exported = midi_export(material=sequence["artifacts"]["material"], store_root=store,
    request_id="example-sequence-export-v1", output_path="phrase-return.mid")
# An explicit harmonic alternative changes only one declared pitch.
sequence_material = sequence["artifacts"]["material"]
sequence_notes = material_query(sequence_material, store, query="events", limit=16)["records"]
target = next(note for note in sequence_notes if note["pitch"]["midi_note"] == 72)
revoiced = midi_transform(material=sequence_material, store_root=store,
    request_id="example-revoice-v1",
    selection=material_query(sequence_material, store, selection={"note_ids": [target["id"]]})["selection"],
    locks={"selected_fields": ["onset", "duration", "velocity", "release_velocity", "voice_id", "role_ref"]},
    operations=[{"op": "revoice", "destinations": [{"note_id": target["id"],
        "pitch": {"midi_note": 74, "cents_offset": 0, "tuning_ref": "tuning:12tet-a440"}}],
        "controller_timeline": "preserve_existing", "pitch_expression": "preserve_relative",
        "hypothesis": {"label": "Supplied upper-note alternative", "actor": "Example author",
            "actor_kind": "agent", "statement": "Try D above the retained source notes.",
            "uncertainty": ["This is an explicit proposal, not a detected key or listening decision."]}}])
Path("construction-result.json").write_text(json.dumps({"unchanged": original, "added": added,
    "split": split, "merged": merged, "sequence": sequence, "exported": exported,
    "revoiced": revoiced}, indent=2) + "\n")
```

The literal gate is `[3,4)` qn. Splitting makes gates `[3,3.5)` and `[3.5,4)`, each with attack 68/release 29 and a fresh identity. Merging restores one gate but retains new identity and derivation. The sequence has eight notes, a four-qn empty interval between its whole-clip occurrences, and an explicit twelve-qn ending. All original onsets/dynamics are preserved within each occurrence. The construction report binds every new note to its exact parent, clip and occurrence; source clip origins are replaced by the declared placements, never assumed to share a clock. Rich controller/expression-bearing sources refuse this initial sequence profile.

### Develop an explicit endpoint and inspect declared release reservations

The next branch retains the original phrase at the first occurrence, then proposes permitted changes to its explicitly chosen last attack at two later occurrences. Empty spans stay empty. The lifecycle call interprets the resulting gates under explicitly declared pedal-off and quarter-note tail assumptions; no MIDI is sent.

```python
import json
from pathlib import Path
from pocket_music import midi_develop, midi_lifecycle
from pocket_music.artifact_store import read_record
from fractions import Fraction

store = "./pocket-midi-demo"
original = json.loads(Path("imported.json").read_text())["material"]
source = read_record(original, store)
endpoint = max(source["notes"], key=lambda note: Fraction(note["onset"]["n"], note["onset"]["d"]))
developed = midi_develop(store_root=store, request_id="example-motif-v1", definition={
    "label": "Retained entrance and two endpoint alternatives",
    "seed": {"material": original, "material_revision": source["revision_sha256"],
             "clip_id": "clip:external"},
    "clock": {"schema": "pocket.time-context/v1", "context_id": "clock:motif-example",
              "attribution": "Example author declares these quarter-note placements."},
    "origin": {"space": "phrase_qn", "n": 0, "d": 1}, "length_qn": 64,
    "occurrences": [{"occurrence_id": name, "at_qn": at}
                    for name, at in [("entrance", 0), ("later", 16), ("return", 32)]],
    "locked_occurrence_id": "entrance", "endpoint_note_id": endpoint["id"],
    "eligible_occurrence_ids": ["later", "return"],
    "variation": {"pitch_offsets_semitones": [0, 2], "timing_offsets_qn": [0, {"n": 1, "d": 8}],
                  "max_changed_notes": 2, "pitch_min": 48, "pitch_max": 84},
    "seeds": {"structure": 7, "pitch": 11, "timing": 13},
})
variant = developed["artifacts"]["alternatives"][1]
variant_record = read_record(variant, store)
lifecycle = midi_lifecycle(material=variant, clip_id=variant_record["clips"][0]["id"],
    initial_state={"active_notes": "none", "sustain": [{"channel": 1, "value": 0}]},
    horizon_qn=64, release_tail_qn={"n": 1, "d": 4},
    equal_time_order="note_off_cc_note_on", source_basis="canonical_notes_retained_cc64",
    store_root=store, request_id="example-lifecycle-v1")
Path("motif-result.json").write_text(json.dumps({"development": developed,
    "lifecycle": lifecycle}, indent=2) + "\n")
```

Both alternatives contain nine notes. Only the two declared later endpoints may change, and the entrance remains exact. The proof separates preserved rhythm, pitch intervals and accents from unassessed perceptual similarity. The no-addition result is a silent destination of the declared length; the unchanged result is the exact original seed. Lifecycle reports symbolic release/tail reservations, not receiver qualification or channel allocation. The model-free branch can be replaced by externally supplied material at every public provider boundary.

## Use the same interfaces directly

The JSON contents are also the MCP argument objects. For example, call MCP tool `material_import` with the contents of `external-import.json`, then call `material_query` with its returned complete handle and the same explicit `store_root`. MCP tool names and Python providers use underscores; CLI commands use hyphens. `capabilities_list` gives bounded discoverability; `tools/list` gives precise MCP input schemas. The server entrypoint is `pocket-mcp`.

To issue the query through CLI after the standalone import above, create the next explicit input from its returned handle:

```python
import json
from pathlib import Path

result = json.loads(Path("imported.json").read_text())
Path("query.json").write_text(json.dumps({
    "store_root": "./pocket-midi-demo",
    "material": result["material"],
    "query": "events",
    "limit": 3,
}) + "\n")
```

```sh
pocket material-query --spec query.json
```

Do not substitute preset names, paths or guessed hashes for artifact handles. Do not copy a query selection to a different material revision. Direct and composed calls have the same validation, journaling and stale-state behavior.

## Continue toward an editable stock layer

This part is a supervised recipe with native acceptance gates. It is not a claim that running the file examples completed a Live project. Use a saved, disposable audio-only source, explicit source hash and confirmed context. The current profile is 120 BPM, 4/4, one unlooped conventional MIDI clip on a new Operator track. The 32-qn generation example fits the first integration fixture; the external four-qn example demonstrates interchange separately.

1. Inspect the saved source with Thread and keep the recording, placement, warp and control state as the protected baseline. Establish safe exclusive use of Live before opening any copies. A running or unsaved musician session takes precedence.
2. Call `candidate_prepare` once with `mode:"clone_only"`, then independently for A and B with `mode:"with_material"`, the corresponding material handle, the same context, and `layer:{"track_name":"Pocket layer A","instrument_device":"Operator"}` (use a distinct B label). Retain all three preparations. Never mutate the source or hard-link writable candidate files.
3. Follow each returned import packet in its own copy. Apply the stock recipe, verify actual controls, set and record one conservative layer gain and hold it fixed across A/B. Save and reopen each candidate. Capture attributed observations and exact saved bytes. `instrument_inspect(scope="supplied_observation")` can retain an explicitly attributed state; it does not independently certify it.
4. Use `candidate_inspect` for the current workspace revision and `candidate_seal` with actual saved/reopened evidence, exact hash and stock recipe observation. Any source-preservation or unknown native normalization failure stops acceptance; a manual import route cannot waive it. `validate_candidate` rechecks a seal. Cancellation is independently callable through `candidate_cancel`.
5. Call `audition_plan` with the unchanged sealed baseline, the A/B seals, an exact span, explicit pre/post-roll and tail, and a concrete listening question such as whether the later phrase entrance feels clearer. Render each variant under the same settings through the supervised route. `attach_candidate_render` binds exact completed audio and validates measured signal; failed attachments are not promotable.
6. Listen to the actual unchanged/A/B renders in context. Record only feedback actually supplied by the listener through `audition_feedback`, bound to the render and frame interval. Retain `no_addition` as an ordinary option. Use a narrow edit if the musician asks for one; preserve earlier versions.
7. `promote_candidate` requires an explicit attributed keep. A human keep needs matching human feedback. Validate the new package with `validate_candidate_promotion` and separately reopen it after relocation in Live before claiming native portability.

Pass optional `related_artifacts` to promotion when the kept package should also retain MIDI derivatives, their fidelity sidecars, the stock preset or comparison attachments. Each must be an actual validated handle; the provider collects their referenced immutable evidence and includes it in lineage. The ordinary promotion call remains valid when this optional list is omitted.

Stitch exposes the same candidate and audition functions; Pipette exposes the same promotion functions. Legacy serialized contracts and guards are preserved. Native parameter writes, preset load/save, MPE, routing and gesture capture are unavailable; native Serum integration is deferred by the user. See the [capability matrix](../../docs/midi-capabilities.md) for all five worked examples and the later-phase gates.

Keep the evidence separate: **artifact integrity; native save/reopen verification; rendered audio and measurements; actual human listening; an attributed musical decision.** A successful render is never a listening verdict.
