# Explicit audio evidence and MIDI timing

With Pocket installed and its virtual environment active, run:

```sh
python examples/midi-audio-timing/example.py
```

This creates synthetic audio and a new temporary artifact store. Supplied notes
replace generation. An attributed correction identifies one fixture attack;
the caller declares its unwarped source-to-host clock and chooses one exact note.
Full-strength and half-strength alternatives move it by 1 and 1/2 quarter-note.
Pitch, velocities and the other note stay locked. Both `unchanged` and
`no_addition` refer to the original music.

The example verifies each result against the same directly called public editor,
checks source bytes/stamps, and writes its complete timing request and report.
The saved `timing-input.json` can be passed to
`pocket midi-timing-alternatives --spec <path>`; MCP exposes the same function
as `midi_timing_alternatives`. `midi_timing_query` provides bounded proof pages.

No model, Serum, Live or listening is required. This is an engineering fixture:
it establishes neither kick identity nor a useful musical change. A declared
pulse point must be supplied explicitly; floating model pulse origins do not
become exact beat grids. Sequential shifts conservatively refuse intermediate
overlaps, even if a simultaneous move might fit.
