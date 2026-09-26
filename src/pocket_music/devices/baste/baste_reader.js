/* ES5 for Max's js runtime; also exercised unchanged in Node with a fake factory.
 * The factory's raw LiveAPI object never escapes the read-only facade.
 * No Live objects or observations survive readSession's stack frame: every
 * object the factory built is released once the result is complete.
 */
var BasteReader = (function () {
    "use strict";
    // Live arms a listener on each collection along a LiveAPI object's path and
    // keeps it armed until the path is cleared; freepeer() and garbage collection
    // leave it in place. Each armed listener is notified of later structural
    // edits to the set, so objects left behind slow Live itself down. Assigning
    // an empty path retargets the object to nothing and leaves the set alone.
    // It is the only write this reader makes to a Live object, and its value is
    // always the empty string, never one a caller supplies.
    function release(created) {
        var failures = 0, first = null;
        for (var i = 0; i < created.length; i += 1) {
            try {
                created[i].path = "";
                // Live can ignore a path write without an error, so read it back.
                // LiveAPI's path property can come back quoted; compare inside.
                var left = String(created[i].path).replace(/^"(.*)"$/, "$1");
                if (left !== "") {
                    failures += 1;
                    if (first === null) { first = "path did not clear: " + left; }
                }
            } catch (err) {
                failures += 1;
                if (first === null) { first = String((err && err.message) || err); }
            }
        }
        return {released: created.length - failures, error: first};
    }
    function readSession(factory, clock) {
        var began = clock(), checks = [], created = [], objects = 0, reads = 0, phase = "traversal";
        function build(path, id) {
            var raw = factory(path, id);
            created.push(raw);
            return raw;
        }
        function fail(code, path, property) {
            var err = new Error(code + ": " + path + (property ? " / " + property : ""));
            err.disposition = code;
            throw err;
        }
        function budget() {
            reads += 1;
            if (objects > 30000 || reads > 300000 || clock() - began > 30000) {
                fail("read_limit", "live_set");
            }
        }
        function object(path, expectedId) {
            budget(); objects += 1;
            var raw = build(path, expectedId), id = Number(raw.id);
            if (!id || (expectedId && id !== expectedId)) { fail("path_invalid", path); }
            checks.push({path: path, id: id});
            return {
                path: path, id: id,
                get: function (property) {
                    budget();
                    var result = raw.get(property);
                    // Identity is checked when constructed and again at the end.
                    // Re-reading it after every scalar get adds thousands of
                    // cross-runtime calls without making this read atomic.
                    if (result === null || typeof result === "undefined" ||
                            (Array.isArray(result) && result.length === 0)) {
                        if (!Number(raw.id)) { fail("path_invalid", path); }
                        fail("unsupported_property", path, property);
                    }
                    return Array.isArray(result) && result.length === 1 ? result[0] : result;
                },
                count: function (child) {
                    budget();
                    var count = raw.getcount(child);
                    if (typeof count !== "number" || count < 0 || Math.floor(count) !== count) {
                        fail("path_invalid", path, child);
                    }
                    checks.push({path: path, child: child, count: count});
                    return count;
                }
            };
        }
        function number(obj, prop) {
            var n = obj.get(prop);
            if (typeof n !== "number" || !isFinite(n)) { fail("unsupported_property", obj.path, prop); }
            return n;
        }
        function text(obj, prop) {
            var value = obj.get(prop);
            if (Array.isArray(value)) { return value.join(" "); }
            return String(value);
        }
        function flag(obj, prop) {
            var n = number(obj, prop);
            if (n !== 0 && n !== 1) { fail("unsupported_property", obj.path, prop); }
            return n === 1;
        }
        function childIds(rawIds, path, property) {
            var ids = [];
            if (!Array.isArray(rawIds)) { fail("path_invalid", path, property); }
            for (var n = 0; n < rawIds.length; n += 1) {
                var value = rawIds[n];
                if (value === "id") { n += 1; value = rawIds[n]; }
                if (typeof value !== "number" || value <= 0 || Math.floor(value) !== value) {
                    fail("path_invalid", path, property);
                }
                ids.push(value);
            }
            return ids;
        }
        function parameter(path, id) {
            var p = object(path, id);
            return {path: path, runtime_id: p.id, name: text(p, "name"), value: number(p, "value"),
                display_value: number(p, "display_value"), min: number(p, "min"), max: number(p, "max"),
                automation_state: number(p, "automation_state")};
        }
        function devices(ownerPath, depth) {
            if (depth > 16) { fail("read_limit", ownerPath); }
            var owner = object(ownerPath), count = owner.count("devices"), result = [];
            for (var i = 0; i < count; i += 1) {
                var path = ownerPath + " devices " + i, d = object(path), params = [], chains = [];
                var n = d.count("parameters");
                // Resolve the child list once, then construct fresh parameter
                // proxies by identity. Deep path resolution for every parameter
                // is expensive in Live. The owner list is checked again below.
                var parameterIds = n ? childIds(d.get("parameters"), path, "parameters") : [];
                if (parameterIds.length !== n) { fail("path_invalid", path, "parameters"); }
                for (var j = 0; j < n; j += 1) {
                    params.push(parameter(path + " parameters " + j, parameterIds[j]));
                }
                if (flag(d, "can_have_chains")) {
                    var chainCount = d.count("chains");
                    for (var k = 0; k < chainCount; k += 1) {
                        var chainPath = path + " chains " + k, chain = object(chainPath);
                        chains.push({path: chainPath, runtime_id: chain.id, name: text(chain, "name"),
                            devices: devices(chainPath, depth + 1)});
                    }
                }
                result.push({path: path, runtime_id: d.id, name: text(d, "name"),
                    class_name: text(d, "class_name"), enabled: flag(d, "is_active"),
                    parameters: params, chains: chains});
            }
            return result;
        }
        function clip(path, view, trackPath, slot) {
            var c = object(path), audio = flag(c, "is_audio_clip");
            var warped = audio ? flag(c, "warping") : null;
            var start = view === "arrangement" ? number(c, "start_time") : null;
            var end = view === "arrangement" ? number(c, "end_time") : null;
            return {id: c.id, path: path, view: view, track: trackPath, slot: slot, present: true,
                name: text(c, "name"), clip_type: audio ? "audio" : "midi",
                start_beats: start, end_beats: end, length_beats: start === null ? null : end - start,
                loop_enabled: flag(c, "looping"), warp_on: warped,
                warp_mode: audio ? number(c, "warp_mode") : null,
                markers: {start: number(c, "start_marker"), end: number(c, "end_marker"),
                    loop_start: number(c, "loop_start"), loop_end: number(c, "loop_end"),
                    unit: audio && !warped ? "source_seconds" : "clip_beats"},
                source_start_seconds: null, source_end_seconds: null,
                source_identity: "not_established_by_live_observation"};
        }
        function track(path, kind) {
            var t = object(path), session = [], arrangement = [];
            if (kind === "track") {
                var count = t.count("clip_slots");
                for (var i = 0; i < count; i += 1) {
                    var slotPath = path + " clip_slots " + i, s = object(slotPath);
                    session.push(flag(s, "has_clip") ? clip(slotPath + " clip", "session", path, i) :
                        {view: "session", track: path, slot: i, present: false});
                }
                count = t.count("arrangement_clips");
                for (var j = 0; j < count; j += 1) {
                    arrangement.push(clip(path + " arrangement_clips " + j, "arrangement", path, null));
                }
            }
            return {path: path, runtime_id: t.id, name: text(t, "name"), kind: kind,
                session_clips: session, arrangement_clips: arrangement, devices: devices(path, 0)};
        }
        var result, released, releaseBegan;
        try {
            var song = object("live_set"), tracks = [], returns = [];
            var count = song.count("tracks"), returnCount = song.count("return_tracks");
            for (var i = 0; i < count; i += 1) { tracks.push(track("live_set tracks " + i, "track")); }
            for (var j = 0; j < returnCount; j += 1) { returns.push(track("live_set return_tracks " + j, "return")); }
            var main = track("live_set master_track", "main");
            // Re-resolve owners and their child-ID lists. Constructing a second
            // proxy for every parameter is expensive in the real host; a parent's
            // get(children) checks every child's identity/order in one read.
            // This still makes no atomic-state claim for moving scalar values.
            phase = "identity_validation";
            var known = {}, owners = {};
            for (var k = 0; k < checks.length; k += 1) {
                var check = checks[k];
                if (check.id) {
                    if (known[check.path] && known[check.path] !== check.id) { fail("path_invalid", check.path); }
                    known[check.path] = check.id;
                    if (check.path === "live_set") { continue; }
                    var parts = check.path.split(" "), tail = parts.pop(), index = 0, child;
                    if (/^\d+$/.test(tail)) { index = Number(tail); child = parts.pop(); }
                    else { child = tail; }
                    var parent = parts.join(" ");
                    if (!owners[parent]) { owners[parent] = {}; }
                    if (!owners[parent][child]) { owners[parent][child] = {ids: []}; }
                    owners[parent][child].ids[index] = check.id;
                } else {
                    if (!owners[check.path]) { owners[check.path] = {}; }
                    if (!owners[check.path][check.child]) { owners[check.path][check.child] = {ids: []}; }
                    var entry = owners[check.path][check.child];
                    if (typeof entry.count !== "undefined" && entry.count !== check.count) {
                        fail("path_invalid", check.path, check.child);
                    }
                    entry.count = check.count;
                }
            }
            for (var ownerPath in owners) {
                budget();
                var current = build(ownerPath);
                if (Number(current.id) !== known[ownerPath]) { fail("path_invalid", ownerPath); }
                for (var property in owners[ownerPath]) {
                    var expected = owners[ownerPath][property];
                    budget();
                    if (typeof expected.count !== "undefined" && current.getcount(property) !== expected.count) {
                        fail("path_invalid", ownerPath, property);
                    }
                    if (expected.count === 0) { continue; }
                    budget();
                    var ids = childIds(current.get(property), ownerPath, property);
                    if (ids.length !== expected.ids.length) { fail("path_invalid", ownerPath, property); }
                    for (var m = 0; m < ids.length; m += 1) {
                        if (ids[m] !== expected.ids[m]) { fail("path_invalid", ownerPath, property); }
                    }
                }
            }
            result = {disposition: "ok", observation: {tracks: tracks, return_tracks: returns, main_track: main,
                objects_read: objects, property_reads: reads}, read_elapsed_ms: clock() - began};
        } catch (err) {
            result = {disposition: err.disposition || "path_invalid", observation: null,
                error: String(err.message || err) + " (" + phase + "; objects=" + objects + "; reads=" + reads + ")",
                read_elapsed_ms: clock() - began};
        } finally {
            // The result, identity validation included, is complete before any
            // object is released, on success and failure alike. Nothing reads a
            // raw object after this point.
            releaseBegan = clock();
            released = release(created);
        }
        result.live_objects_created = created.length;
        result.live_objects_released = released.released;
        result.release_elapsed_ms = clock() - releaseBegan;
        result.release_error = released.error;
        return result;
    }
    return {readSession: readSession};
}());
if (typeof module !== "undefined") { module.exports = BasteReader; }
