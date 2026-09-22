# Explicit sections over a long declared span

Run `python examples/midi-arrangement/example.py` with Pocket installed. This example needs no DAW, MIDI codec or model. Outputs go into a new temporary directory printed at the end.

The two JSON files are synthetic, externally authored material. Their local note and clip IDs deliberately repeat across parents. The recipe imports them, creates an attributed phrase graph, and places three whole eight-quarter-note sections at positions 0, 4600 and 9224. The destination lasts 9240 quarter notes: 77 minutes at the explicitly declared 120 BPM. Six notes across that span test sparse long-duration indexing; they do not constitute a real set or establish audio alignment.

`midi_arrangement_develop` retains the exact input graph and source materials, literal alternative A, changed alternative B and a silent destination of the same length. The opening is locked. B changes only the explicitly named endpoint in each later section; dynamics, releases, gates and other note fields remain exact. The middle endpoint moves by one quarter of a quarter note and down two semitones. The return endpoint moves up two semitones. Source origins are replaced by the supplied placements; no clock conversion is inferred.

`midi_arrangement_query` retrieves declared sections and sparse changes with bounded, revision-bound pagination. The full artifact retains source identity and the public composition steps. The providers can also be invoked as `pocket midi-arrangement-develop --spec request.json` and `pocket midi-arrangement-query --spec request.json`, or through the identically named MCP tools.

This profile accepts explicitly declared whole clips with ordinary notes. Controllers, note expression, opaque native bindings, source tempo/meter maps and ambiguous same-channel pitch overlaps require other qualified routes. It does not infer motifs, choose a sound, write a native arrangement, establish perceptual similarity or make a listening decision. Keep the supplied material or the silent alternative when that is the better musical choice.
