# Pipette: keep a saved trial as a new project

> **In brief.** Like a lab pipette, Pipette moves exactly one measured thing and
> nothing else. When you've decided to keep a [Stitch](stitch.md) trial,
> Pipette carries it into a brand-new, self-contained Live project. It re-checks
> everything first and records where the project came from. Your original set
> and the trial are left untouched.
>
> **You need** a sealed native Stitch trial and the render you chose to keep.
> Someone has to decide to keep it, and Pipette records who did. An agent's
> technical keep stays labeled as one.

Pipette promotes an explicitly kept Stitch native candidate into a new collected
Ableton project, preserving its parent and trial. It requires a selected render
attachment with usable expected signal. It cannot promote an unsaved Baste
observation or decide that a transition sounds good.

## Inputs and matching interfaces

Start with a sealed `pocket.native-trial/v2` candidate from `stitch_prepare_native`
and a completed float-WAV attachment from `stitch_attach_render`. Retain the exact
trial manifest, candidate and selected attachment hashes from those responses.
Choose one attachment deliberately; Pipette never scans for the newest render.

```python
from pathlib import Path
from pocket_music import promote_trial, validate_promotion

# trial and attached are the actual Stitch provider responses from this experiment.
result = promote_trial(
    trial_dir=trial["trial_dir"],
    output_dir="/path/to/new/Kept Project",
    attachment_relative_path=str(
        Path(attached["attachment_dir"]).relative_to(trial["trial_dir"])
    ),
    expected_trial_manifest_sha256=trial["manifest_sha256"],
    expected_candidate_sha256=trial["candidate_sha256"],
    expected_attachment_sha256=attached["receipt_sha256"],
    decision={
        "action": "keep",
        "actor": "Operator name",
        "actor_kind": "human",  # use agent when it is the agent's decision
        "reason": "The explicitly approved scope of this keep decision",
    },
)
checked = validate_promotion(
    result["promotion_dir"], expected_lineage_sha256=result["lineage_sha256"]
)
```

MCP `pipette` accepts those same named inputs. `pipette_validate` takes
`promotion_dir` and `expected_lineage_sha256`. Both return one compact JSON text
record. The CLI uses a JSON object containing the same inputs, except that the
promotion destination belongs in `--output`:

```sh
pocket pipette promote --spec keep-spec.json --output '/path/to/new/Kept Project'
pocket pipette validate --spec validate-spec.json
```

`validate-spec.json` contains `promotion_dir` and `expected_lineage_sha256`.
An existing destination, including an empty directory, is rejected. A failed call
does not authorize retrying over a partial directory; inspect it and choose a new
destination. Successful promotion returns `child_als`, its hash, the lineage path
and hash, and a normal Thread summary/handle.

## What must pass before promotion

1. The expected trial/candidate/attachment hashes match their seals and current
   bytes. The selected attachment is contained under that trial's `renders/`.
2. Stitch validates the collected candidate's active media and supported Max
   files. The attachment references that same trial, candidate and export range.
3. Current render bytes and decoded header match the selected attachment. Its
   artifact verification succeeded and its signal is usable for the declared
   expectation: `usable_signal` for music, or `intentional_silence` with the same
   explicit preparation note. Unexpected silence, near silence, non-finite audio
   and sample overload are rejected. Promotion does not rerender or normalize.
4. The saved parent still exists at its recorded path and has its original hash.
   An explicit `keep` supplies a nonempty actor/reason and `human` or `agent`
   attribution. A changed or relocated parent requires deliberate reconciliation,
   not silently adopting a newer baseline.
5. Evidence and media are copied into staging, checked again, and parent bytes and
   mtime are verified unchanged. Thread reads the child and resolves all active
   references unambiguously through its ordinary saved-map/handle path.

`ready_to_compare` and original native loading/export reports are retained but
are not recast as independently measured facts. A usable artifact with an explicit
agent keep can test promotion mechanics without pretending a human heard it.

## Child and lineage

The new project contains:

- `promoted.als`, `Samples/`, any supported `Devices/`, and an Ableton project marker.
- `evidence/approved-candidate.als`, the exact approved candidate bytes.
- `evidence/native-trial.json` and its seal; selected `attachment.json` and seal;
  the exact selected `render.wav`.
- `lineage.json` and its SHA-256 seal (`pocket.promotion/v1`).

An unchanged copy of the candidate would retain absolute hints to the old trial,
making the two existing media locations ambiguous to Thread. Pipette therefore
rewrites **only** the child's collected absolute dependency hints. Reversing those
declared rewrites must reproduce the approved candidate's parsed XML exactly.
Arrangement, clips, devices, automation and all other saved XML remain unchanged;
media and evidence are byte copies. The candidate and child have distinct hashes.

Lineage records parent → trial → candidate → child, both project hashes, selected
attachment/render hashes, export range, timestamp, attributed keep context and
the exact reference-hint changes. The complete record is sealed in staging;
dependencies and seal publish before the openable child. Partial publication is
rolled back on an ordinary error. Checksums establish identity, not authenticity,
an OS transaction, or protection from a malicious concurrent writer/power failure.

`validate_promotion` rechecks the seal and every retained artifact, then creates
and queries a normal Thread handle. It never disables Thread's stale-file checks.
Two explicit calls can create independent children of one parent. Neither call
updates or selects a global baseline.

## Relocation, editing and native verification

Move the **whole** child project and run validation again. The original trial or
parent need not remain available for child validation. Copying a child while the
original absolute-hint targets still exist can produce Thread's intentional
ambiguity error; move it, or deliberately rebind a separate project, rather than
weakening the saved-map checks. The lineage retains original provenance paths.

Before using the child musically, open it in Live and inspect missing/external
media and device availability. Pipette's return says `native_loading: unverified`;
filesystem success is not a native check. Its collection scope is Stitch v2's
active audio, supported Max patches and adjacent same-stem `.maxpat` files.
Dependencies hidden inside a patch, third-party plugin installation and audible
DSP behavior remain separate checks.

Treat the sealed child as an immutable baseline. Save later edits to a separate
working copy; overwriting the child correctly invalidates its lineage and saved
Thread handles. Never label Live's normalization or other edits as the same hash.
Private paths, recordings, operator observations and listener notes stay outside
the repository.

See [Stitch](stitch.md) and [Thread](thread.md). Generated promotion tests run with
`python -m pytest -q tests/test_pipette.py tests/test_baste_pipette_interfaces.py`.
