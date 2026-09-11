# Baste and Pipette acceptance record

Development-machine checks on 2026-09-11, starting from Pocket 0.3 (`a4c59af`).
Implementation and [planning](baste-pipette-plan.md) cover Baste/Pipette from the
three reviewed product specifications. Tension, Bobbin, Selvedge and Sley remain
outside this cycle. All source projects and production renders are preserved;
private audio, paths and detailed operator evidence are outside this repository.

## Automated and package checks

- Baseline before implementation: 371 tests and three subtests passed.
- Integrated suite: 423 tests and three subtests passed on Python 3.14; the final
  full run, including the 35-second default timeout, completed in 51.86 seconds.
- Thirteen Node tests cover the unchanged Max reader, capability restrictions,
  Session/Arrangement clips, nested devices, missing/changed objects, fresh reads,
  same-count parameter reordering, budgets, dictionary release, Node for Max
  startup, busy requests and late replies.
  A generated 24-track, six-device-per-track fixture read 3,295 objects in about
  23 ms in Node. That number is not a native Live latency estimate.
- Actual CLI and MCP stdio calls build the device, read an isolated simulated
  transport twice, promote generated evidence, validate it, and query the returned
  handle through ordinary Thread. Invalid promotion inputs create no child.
- Promotion tests cover parent/trial bytes and mtime, every stale evidence layer,
  signal rejection, matched intentional silence, explicit attribution, relocation,
  two independent child trials, source changes during copy, partial publication
  rollback and changed-child rejection through both Pipette and Thread.
- Required repository lint and expanded lint on the new Python code pass.
- A 0.4.0 wheel was built with isolated build dependencies and installed outside
  the checkout. All four editable device sources were present; installed imports,
  device construction and generated promotion/readback passed. The initial
  non-isolated build attempt lacked setuptools in the development environment;
  the declared isolated build path succeeded.

CI now runs the Node tests explicitly alongside the existing Python matrix.
These are local test results; no remote CI run or Git publication is claimed.

## Native Baste observations

The generated `.amxd` successfully loaded in an isolated copy of a real production
project under Live 12.4.5 on macOS. Its editable presentation appeared in Live and
the real Node for Max bridge answered three complete fresh reads after the final
changes. The second and third returned a deliberate **unsaved** track rename made
through Live's Rename command; the first returned its saved name:

| Observation | Result |
|---|---|
| Tracks | 20 regular, two returns and Main |
| Arrangement / populated Session clips | 20 / one |
| Devices / parameters | 44 / 1,984 |
| Objects / counted reads | 2,256 / 15,192 |
| Device elapsed, three reads | 19,539 / 16,425 / 21,312 ms |
| Round trip, three reads | 19,682.9 / 16,585.6 / 21,475.2 ms |
| Reader budget / client deadline | 30 / 35 seconds |
| Saved-file preservation | Fixture and production `.als` bytes and mtime unchanged |

These reads used the real LiveAPI, not a fake adapter. The GUI comparison verified
20 track names, two returns and Main; the Session clip's enabled loop and Warp Pro,
start 1.1.1, end 5.1.1 and four-bar loop length matched its 0–16 beat markers.
EQ Eight's 1,000 Hz frequency, 0 dB gain and 100% Scale, and Utility's 100% Width
and 0 dB Output matched the returned values. Public flags remained actual JSON
booleans. The fixture's saved name, bytes and mtime remained unchanged after the
unsaved rename and observations. The test rename was discarded without saving.

Before a ready device was loaded, and after opening a set without Baste, real
requests distinguished running Live from `device_not_loaded`.
The first startup attempt exposed Node for Max's loader behavior: it does not
necessarily set `require.main` to the script. Startup now recognizes `MAX_ENV`,
with an executable regression test.

Review of an initial native response also exposed Max Dict's boolean-to-numeric-atom
conversion. The wire format now carries serialized JSON in bounded Unicode-safe
string chunks, preserving the public field types. A regression reproduces the
native coercion, checks boolean types and retains long Unicode names exactly.
The bridge also discards replies whose deadline expires during dictionary transfer.

Earlier native reads hit a 15-second limit and returned no partial observation.
Those failed reads were retained as failures, not used to pass an acceptance gate.
The reader now removes redundant scalar identity reads, constructs fresh parameter
proxies from this request's child IDs, and validates topology through owner child-ID
lists. Regression tests exercise same-count parameter reordering. The original
10-second performance target was **not reliably met**: the final tested budget is
30 seconds, and the default client timeout is 35 seconds to allow the full reader
budget on larger sets. These three successful repeats are
bounded acceptance evidence, not a long-running soak test or a real-time guarantee.

## Real saved-trial promotion

A preserved Stitch v2 trial had a selected 32-second, 48 kHz stereo float native
render with `usable_signal`, zero non-finite samples, no sample overload, about
−5.08 dBFS sample peak and −19.03 dBFS RMS. Its sealed receipt contained earlier
attributed loading/export reports. Those reports remain reports, not new native
observations by Pipette.

An explicitly attributed **agent technical keep** promoted that trial into an
isolated child; no new musical approval was invented. All 35 artifacts verified.
Thread resolved all 27 active references, mapped 26 audio clips and resolved 54
controls. The exact approved candidate was retained as evidence, while only the
child's 27 collected absolute hints were rewritten. XML rollback/readback verified
every other saved field, and parent/trial bytes and mtime remained unchanged.

The entire child project was physically moved. Validation again checked all 35
artifacts and ordinary Thread readback successfully, independently of its original
location. Dormant provenance references remain reported separately from active
media, following Thread's existing contract.

The relocated child opened in Live 12.4.5. Its native file-management report showed
no missing files and no external files. It remained stopped, with no saved changes;
all 35 artifacts and the original child hash verified again afterward. This is an
independent operator observation. `native_loading` remains `unverified` in the
provider output and sealed lineage, rather than being retroactively rewritten.
This promotion used an existing saved trial; no retrospective correlation to a
new Baste observation is claimed.

Native checks waited for the parallel production task to release Live. Its
preserved export snapshot was restored afterward, verified stopped, with its
original bytes and mtime unchanged. Private native observations and the final
installed-wheel check are retained outside the repository.

## Limits that remain by design

Baste is a sequential observation with momentary IDs, not an atomic snapshot,
saved revision, continuous monitor or writer. Native full-set reads can be
expensive. Pipette promotes only saved Stitch v2 candidates, preserves original
operator/measurement/decision distinctions and does not render, audition or merge
unsaved edits. Its Max dependency scope is exactly Stitch's existing limited
collection scope. Moving a whole child is supported; copying it while the original
absolute targets remain can intentionally trigger Thread's ambiguity guard.

Further edits to a sealed child invalidate its hash; save working changes
separately. Neither successful tests nor a usable render establish good musical
flow. No version-control write, repository visibility change or publication was
performed in this cycle.
