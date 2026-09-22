/* Shared ES5 reader/validators. No mutating LiveAPI method exists in this reader module. */
var NativeMidiCore = (function () {
    var NOTE_FIELDS = ["note_id", "pitch", "start_time", "duration", "velocity", "mute", "probability", "velocity_deviation", "release_velocity"];
    function fail(message) { throw new Error(message); }
    function number(value, minimum, maximum, integer) {
        if (typeof value !== "number" || !isFinite(value) || value < minimum || value > maximum || (integer && Math.floor(value) !== value)) { fail("Invalid finite number"); }
        return value;
    }
    function keys(value, names) {
        if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join("|") !== names.slice().sort().join("|")) { fail("Unexpected record fields"); }
    }
    function text(value, maximum) { if (typeof value !== "string" || value.length > maximum) { fail("Invalid bounded text"); } return value; }
    function target(value) {
        keys(value, ["location", "track_index", "clip_index"]);
        if (value.location !== "arrangement") { fail("Only explicit arrangement targets are implemented"); }
        number(value.track_index, 0, 255, true); number(value.clip_index, 0, 255, true);
        return value;
    }
    function notes(value) {
        if (!Array.isArray(value) || value.length > 2000) { fail("Native note read exceeds2000-note bound"); }
        var ids = {};
        value.forEach(function (note) {
            keys(note, NOTE_FIELDS); number(note.note_id, 1, 2147483647, true);
            if (ids[note.note_id]) { fail("Duplicate native note ID"); } ids[note.note_id] = true;
            number(note.pitch, 0, 127, true); number(note.start_time, -1000000000, 1000000000, false);
            number(note.duration, Number.MIN_VALUE, 1000000000, false); number(note.velocity, 0, 127, false);
            number(note.release_velocity, 0, 127, false); number(note.mute, 0, 1, true);
            number(note.probability, 0, 1, false); number(note.velocity_deviation, -127, 127, false);
        });
        return value.slice().sort(function (a, b) { return a.note_id - b.note_id; });
    }
    function read(factory, requested) {
        target(requested);
        var identities = {}, visits = 0, started = Date.now();
        function object(path) {
            visits += 1;
            if (visits > 4096 || Date.now() - started > 10000) { fail("Native topology exceeds bounded read budget"); }
            var item = factory(path); number(Number(item.id), 1, 2147483647, true); return item;
        }
        function scalar(item, property) {
            var raw = item.get(property);
            return Array.isArray(raw) && raw.length === 1 ? raw[0] : raw;
        }
        function boundedCount(item, property, maximum) { return number(item.getcount(property), 0, maximum, true); }
        function identity(item, kind) {
            var id = Number(item.id);
            if (identities[id]) { fail("Ambiguous duplicate runtime identity: " + kind); }
            identities[id] = kind; return id;
        }
        function flag(item, property, kind) {
            try {
                var raw = item.get(property), value = Array.isArray(raw) && raw.length === 1 ? raw[0] : raw;
                var valid = kind === "bool" ? typeof value === "number" && (value === 0 || value === 1) : kind === "string" ? typeof value === "string" && value.length <= 4096 : typeof value === "number" && isFinite(value);
                if (valid) { return {status: "available", value: value, raw: raw}; }
                return {status: "unsupported", raw_type: typeof raw, raw_text: String(raw).slice(0, 4096)};
            } catch (errorValue) { return {status: "unsupported", error: String(errorValue).slice(0, 1024)}; }
        }
        function flags(item, booleans, numbers, strings) {
            var result = {};
            booleans.forEach(function (property) { result[property] = flag(item, property, "bool"); });
            numbers.forEach(function (property) { result[property] = flag(item, property, "number"); });
            strings.forEach(function (property) { result[property] = flag(item, property, "string"); });
            return result;
        }
        function clipState(path, index) {
            var clip = object(path), result = {index: index, runtime_id: identity(clip, path), name: text(scalar(clip, "name"), 1024)};
            ["is_midi_clip", "looping"].forEach(function (property) { result[property] = number(scalar(clip, property), 0, 1, true); });
            ["start_time", "end_time", "loop_start", "loop_end", "start_marker", "end_marker"].forEach(function (property) { result[property] = number(scalar(clip, property), -1000000000, 1000000000, false); });
            return result;
        }
        function trackState(path, index, isReturn) {
            var track = object(path), result = {index: index, runtime_id: identity(track, path), name: text(scalar(track, "name"), 1024), devices: [], arrangement_clips: []};
            var count = boundedCount(track, "devices", 64);
            for (var d = 0; d < count; d += 1) {
                var devicePath = path + " devices " + d, device = object(devicePath);
                result.devices.push({index: d, runtime_id: identity(device, devicePath), name: text(scalar(device, "name"), 1024), class_name: text(scalar(device, "class_name"), 256)});
            }
            if (!isReturn) {
                var clipCount = boundedCount(track, "arrangement_clips", 256);
                for (var c = 0; c < clipCount; c += 1) { result.arrangement_clips.push(clipState(path + " arrangement_clips " + c, c)); }
            }
            return result;
        }
        var song = object("live_set"), app = object("live_app");
        function topologySnapshot() {
        identities = {};
        var topology = {song: {runtime_id: identity(song, "live_set")}, tracks: [], return_tracks: []};
        ["tracks", "return_tracks"].forEach(function (collection) {
            var count = boundedCount(song, collection, collection === "tracks" ? 256 : 64);
            for (var i = 0; i < count; i += 1) { topology[collection].push(trackState("live_set " + collection + " " + i, i, collection === "return_tracks")); }
        });
        return topology;
        }
        var topology = topologySnapshot();
        if (!topology.tracks[requested.track_index] || !topology.tracks[requested.track_index].arrangement_clips[requested.clip_index]) { fail("Explicit target is absent"); }
        var trackPath = "live_set tracks " + requested.track_index, clipPath = trackPath + " arrangement_clips " + requested.clip_index;
        var track = object(trackPath), clip = object(clipPath), clipSnapshot = topology.tracks[requested.track_index].arrangement_clips[requested.clip_index];
        if (Number(track.id) !== topology.tracks[requested.track_index].runtime_id || Number(clip.id) !== clipSnapshot.runtime_id || clipSnapshot.is_midi_clip !== 1) { fail("Target changed or is not a MIDI clip"); }
        var raw = clip.call("get_all_notes_extended");
        if (typeof raw !== "string" || raw.length > 1048576) { fail("Unsupported or oversized native note transport"); }
        var parsed = JSON.parse(raw); keys(parsed, ["notes"]);
        var guards = {
            song: flags(song, ["is_playing", "record_mode", "arrangement_overdub", "session_record", "session_automation_record"], ["tempo", "signature_numerator", "signature_denominator"], ["file_path"]),
            track: flags(track, ["is_frozen", "is_grouped", "is_foldable", "mute", "solo", "arm"], [], []),
            clip: flags(clip, ["has_envelopes", "has_groove", "muted", "is_arrangement_clip", "is_session_clip", "is_take_lane_clip", "is_midi_clip", "is_overdubbing", "is_playing", "is_recording", "is_triggered", "looping"], ["loop_start", "loop_end", "start_marker", "end_marker", "start_time", "end_time"], [])
        };
        var version = app.call("get_version_string");
        if (typeof version !== "string" || version.length > 100) { fail("Unsupported runtime version transport"); }
        if (JSON.stringify(topology) !== JSON.stringify(topologySnapshot())) { fail("stale_state: topology changed during native read"); }
        return {host: {runtime_version: version, file_path: guards.song.file_path.status === "available" ? guards.song.file_path.value : null}, topology: topology,
            target: {location: "arrangement", track_index: requested.track_index, clip_index: requested.clip_index, track_runtime_id: Number(track.id), clip_runtime_id: Number(clip.id), path: clipPath, device_track_runtime_id: Number(object("this_device canonical_parent").id)},
            notes: notes(parsed.notes), guards: guards, raw_notes_json: raw};
    }
    return {target: target, notes: notes, read: read};
}());
if (typeof module !== "undefined") { module.exports = NativeMidiCore; }
