/* Exact ordinary-note experiment profile. No arbitrary native call path. */
var NativeMidiWriter = (function () {
    function fail(message) { throw new Error(message); }
    function keys(value, fields) {
        if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join('|') !== fields.slice().sort().join('|')) { fail('Invalid write fields'); }
    }
    function integer(value, low, high) {
        if (typeof value !== 'number' || !isFinite(value) || Math.floor(value) !== value || value < low || value > high) { fail('Invalid integer note value'); }
    }
    function canonical(value) {
        if (Array.isArray(value)) { return '[' + value.map(canonical).join(',') + ']'; }
        if (value && typeof value === 'object') { return '{' + Object.keys(value).sort().map(function (key) { return JSON.stringify(key) + ':' + canonical(value[key]); }).join(',') + '}'; }
        return JSON.stringify(value);
    }
    function state(value) {
        return {host: value.host, topology: value.topology, target: value.target, notes: value.notes, guards: value.guards};
    }
    function flag(core, scope, name, expected) {
        var observed = core.guards[scope][name];
        if (!observed || observed.status !== 'available' || observed.value !== expected) { fail('Unsupported native guard: ' + scope + '.' + name); }
    }
    function guards(core, request) {
        if (core.host.runtime_version !== '12.4.5' || core.host.file_path !== request.saved_als_path) { fail('Unqualified host or saved path'); }
        if (core.target.device_track_runtime_id !== core.target.track_runtime_id) { fail('Writer must be loaded on the explicitly owned target track'); }
        flag(core, 'song', 'tempo', 120); flag(core, 'song', 'signature_numerator', 4); flag(core, 'song', 'signature_denominator', 4);
        if (core.guards.song.session_automation_record.status !== 'available' || [0,1].indexOf(core.guards.song.session_automation_record.value) < 0) { fail('Unknown automation arm state'); }
        ['is_playing', 'record_mode', 'arrangement_overdub', 'session_record'].forEach(function (name) { flag(core, 'song', name, 0); });
        ['is_frozen', 'is_grouped', 'is_foldable', 'mute', 'solo', 'arm'].forEach(function (name) { flag(core, 'track', name, 0); });
        ['has_envelopes', 'has_groove', 'muted', 'is_overdubbing', 'is_playing', 'is_recording', 'is_triggered', 'looping'].forEach(function (name) { flag(core, 'clip', name, 0); });
        ['is_arrangement_clip', 'is_midi_clip'].forEach(function (name) { flag(core, 'clip', name, 1); });
        ['loop_start', 'start_marker'].forEach(function (name) { flag(core, 'clip', name, 0); });
        // is_session_clip/is_take_lane_clip are unknown on the qualified build.
        // Positive primary namespace + saved XML ancestry are separately bound.
        core.notes.forEach(function (note) {
            if (note.mute !== 0 || note.probability !== 1 || note.velocity_deviation !== 0) { fail('Enriched notes outside write profile'); }
            integer(note.velocity, 1, 127); integer(note.release_velocity, 0, 127);
        });
    }
    function plan(request, before) {
        guards(before, request);
        if (canonical(state(request.expected)) !== canonical(state(before))) { fail('stale_state: complete native note/topology/guard snapshot differs'); }
        var edit = request.edit, length = before.guards.clip.end_marker.value, dictionary, method;
        if (edit.kind === 'insert_empty') {
            keys(edit, ['kind', 'notes']);
            if (before.notes.length !== 0 || !Array.isArray(edit.notes) || edit.notes.length < 1 || edit.notes.length > 3) { fail('Insertion requires a saved empty clip and1–3 notes'); }
            var notes = edit.notes.map(function (note) {
                keys(note, ['pitch', 'start_time', 'duration', 'velocity', 'release_velocity']);
                integer(note.pitch, 0, 127); integer(note.velocity, 1, 127); integer(note.release_velocity, 0, 127);
                if (typeof note.start_time !== 'number' || typeof note.duration !== 'number' || !isFinite(note.start_time) || !isFinite(note.duration) || note.start_time < 0 || note.duration <= 0 || note.start_time + note.duration > length || Math.floor(note.start_time * 1048576) !== note.start_time * 1048576 || Math.floor(note.duration * 1048576) !== note.duration * 1048576) { fail('Unsupported exact dyadic note time'); }
                return {pitch: note.pitch, start_time: note.start_time, duration: note.duration, velocity: note.velocity, release_velocity: note.release_velocity, mute: 0, probability: 1, velocity_deviation: 0};
            });
            notes.forEach(function (note, index) { notes.slice(index + 1).forEach(function (other) { if (note.start_time < other.start_time + other.duration && other.start_time < note.start_time + note.duration) { fail('Temporal overlaps are outside profile'); } }); });
            method = 'add_new_notes'; dictionary = {notes: notes};
        } else if (edit.kind === 'set_velocity') {
            keys(edit, ['kind', 'note_id', 'velocity']);
            integer(edit.note_id, 1, 2147483647); integer(edit.velocity, 1, 127);
            var matches = before.notes.filter(function (note) { return note.note_id === edit.note_id; });
            if (matches.length !== 1) { fail('Selected inserted note no longer exists'); }
            var note = JSON.parse(JSON.stringify(matches[0])); note.velocity = edit.velocity;
            method = 'apply_note_modifications'; dictionary = {notes: [note]};
        } else { fail('Unsupported native edit'); }
        return {method: method, dictionary: dictionary};
    }
    function verify(request, before, after) {
        guards(after, request);
        if (canonical({host: before.host, topology: before.topology, target: before.target, guards: before.guards}) !== canonical({host: after.host, topology: after.topology, target: after.target, guards: after.guards})) { fail('Native context changed during note dispatch'); }
        if (request.edit.kind === 'insert_empty') {
            if (after.notes.length !== request.edit.notes.length) { fail('Inserted note count differs'); }
            var expected = request.edit.notes.map(function (note) { return canonical({pitch: note.pitch, start_time: note.start_time, duration: note.duration, velocity: note.velocity, release_velocity: note.release_velocity, mute: 0, probability: 1, velocity_deviation: 0}); }).sort();
            var actual = after.notes.map(function (note) { var copy = JSON.parse(JSON.stringify(note)); delete copy.note_id; return canonical(copy); }).sort();
            if (canonical(expected) !== canonical(actual)) { fail('Inserted note readback differs'); }
        } else {
            var expectedNotes = before.notes.map(function (note) { var copy = JSON.parse(JSON.stringify(note)); if (copy.note_id === request.edit.note_id) { copy.velocity = request.edit.velocity; } return copy; });
            if (canonical(expectedNotes) !== canonical(after.notes)) { fail('Velocity readback changed other note fields'); }
        }
    }
    return {canonical: canonical, state: state, plan: plan, verify: verify};
}());
if (typeof module !== 'undefined') { module.exports = NativeMidiWriter; }
