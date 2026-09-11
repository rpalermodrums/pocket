# Baste and Pipette implementation plan

## Selection and source brief

This cycle selects Baste and Pipette from the six proposed tools. Baste establishes
the missing live read path before Tension can safely write parameters. Pipette
closes the saved trial lineage before Selvedge can offer promotion. Sley and Bobbin
remain separate cycles; neither is a hidden dependency of these two tools.

Reviewed specifications: [Baste/Pipette](https://docs.google.com/document/d/1L77LUCTzPtGAUTND93isSw-IJaMo9mQK_p_yliVYvc8/edit),
[Selvedge/Sley](https://docs.google.com/document/d/1jziVaymOs7PTdBfZfVjq3Q14Mgq85VdMM4MaqCva1sI/edit),
and [Tension/Bobbin](https://docs.google.com/document/d/1ujQ2u7rhpOCE9Sq323u7sFQ12Wlh6dqtCHSg6jXMvnE/edit).
The implementation starts from Pocket 0.3 on `a4c59af`. Existing serialized
identifiers and compatibility aliases stay valid. The owner handles all Git writes.

## Baste design

- A real Max for Live audio-effect device passes audio straight through. A
  JavaScript reader accesses Live through a capability-limited wrapper exposing
  only `get`, `getcount`, children and momentary object identity. No `call`, `set`
  or `goto`, no playhead control, no saved-set writes.
- Read tracks, return tracks, Main, Session slots, Arrangement clips, devices,
  nested rack chains and exposed parameters. Preserve native names and distinguish
  source markers from Arrangement beats. Do not infer recording identity or plugin
  internals. Unsupported properties and changed topology cannot become an empty
  successful observation.
- Node for Max transports fresh requests over an authenticated loopback endpoint.
  Its discovery file contains connection information only, never cached session
  observations. Request nonces bind replies to one request. Multiple active devices
  are ambiguous, rather than arbitrarily choosing one.
- Python exposes `observe_live`; CLI and MCP call it unchanged. Every response
  carries observation start/end and elapsed time. Runtime object IDs are confined
  to that observation and cannot be supplied as a later handle. A sequential read
  is not an atomic snapshot or a durability guarantee.
- Return distinct stopped-Live, missing-device and invalid-path dispositions.
  Timeouts, unsupported properties and resource limits are explicit additional
  failures. The first platform acceptance is macOS with Live 12 Suite.

## Pipette design

- Explicit inputs select one sealed native trial and one sealed render attachment,
  with expected trial/candidate/attachment hashes and an attributed keep reason.
  No directory scan picks the newest or most favorable render automatically.
- Revalidate the trial seal, current candidate, collected dependencies, attachment
  seal, candidate/trial/range binding, current render bytes, and signal disposition.
  Accept usable music or explicitly matched intentional silence. Preserve native
  operator reports as reports; do not manufacture listening approval.
- Create a fresh project directory with exact collected dependencies. Keep the
  original parent and trial untouched, including mtime. Preserve an exact copy of
  the approved candidate as evidence. Rebind only the child's collected absolute
  dependency hints to the new project. Prove that reverting those hints reproduces
  the approved XML exactly. Record both candidate and child hashes.
- Seal parent → trial → child lineage, selected attachment/render identity,
  export range and attributed reason before returning the child. Two children
  from one parent remain independent. Staging failures never return a usable path.
- Read the child with Thread and exercise its normal saved-handle validation.
  Do not special-case Pipette files or imply that Thread establishes native sound.

## Implementation and acceptance order

1. Baste reader, real device build and replaceable transport. Generated FakeLiveAPI
   tests exercise multiple tracks, both clip views, racks, parameters, missing
   objects, unsupported fields and bounded reads. Assert the reader cannot access
   mutation methods. Test loopback/authentication/nonces and failure distinctions.
2. Real Live acceptance in an isolated local project: GUI comparison, a change
   since Save, repeated fresh observations, no read-induced project changes and a
   measured latency budget (initial target: full representative read within 10
   seconds; report actual count and latency). Native testing found that target
   unreliable for 1,984 parameters on this machine. The accepted reader budget
   is 30 seconds, with a 35-second client deadline for large sets; final repeated
   native reads took 16.4–21.3 seconds. Keep the missed target visible as a
   performance limit. Preserve the user's loaded production set.
3. Pipette implementation with generated trial/render fixtures. Test byte/mtime
   preservation, stale/altered evidence, rejected signal dispositions, intentional
   silence, path escape, relocation, independent branches and interrupted publish.
4. CLI and actual stdio MCP discovery/calls for both providers, including invalid
   requests creating no output. Package the editable device sources and builder.
5. Real existing native trial promotion and Thread readback; open the relocated
   child in Live, check collected media, and retain observations outside Git.
6. Full regression suite and lint; installation/package checks; update README,
   skill, provider guides and evaluation with measured outcomes and honest limits.

## Evidence rules

Generated fixtures are public-test material, never proof of real Live execution.
Private project paths, audio, observations and listening notes remain outside the
repository. Baste does not preserve unsaved edits by saving them. Pipette promotes
only saved Stitch candidates, not mutable Baste observations. Neither tool decides
whether a transition or set sounds good.
