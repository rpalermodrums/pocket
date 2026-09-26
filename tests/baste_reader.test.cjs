// SPDX-License-Identifier: AGPL-3.0-only
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readerPath = path.resolve(__dirname, "../src/pocket_music/devices/baste/baste_reader.js");
const devicePath = readerPath.replace("baste_reader", "baste_device");
const {readSession} = require(readerPath);

// `host` models the Live side. `releaseWrites` admits the device's release,
// clearing the path, and no other write; without it every write fails, so the
// reader tests also prove the reader writes nothing. Once the path is cleared,
// `keepsPath` leaves it reading the old path, `idAfterRelease` sets what `id`
// reads (0 unless given) and `quoted` quotes paths as Max's LiveAPI can.
// `refuses` makes the write itself throw.
function fixture(trackCount = 2, deviceCount = 2, parameterCount = 3, host = {}) {
    const data = new Map();
    function add(p, props = {}, counts = {}) { data.set(p, {id: data.size + 1, props, counts}); }
    function device(p, rack = false) {
        add(p, {name: "Device <text>", class_name: rack ? "AudioEffectGroupDevice" : "Eq8",
            is_active: 1, can_have_chains: rack ? 1 : 0}, {parameters: parameterCount, chains: rack ? 1 : 0});
        for (const i of Array.from({length: parameterCount}, (_, i) => i)) {
            add(p + " parameters " + i, {name: "Frequency", value: 0.5, display_value: 1000,
                min: 0, max: 1, is_enabled: 1, state: 0, automation_state: i % 3});
        }
        if (rack) { add(p + " chains 0", {name: "Rack chain"}, {devices: 1}); device(p + " chains 0 devices 0"); }
    }
    function track(p, clips) {
        add(p, {name: p}, {devices: deviceCount, clip_slots: clips ? 2 : 0, arrangement_clips: clips ? 1 : 0});
        for (const i of Array.from({length: deviceCount}, (_, i) => i)) device(p + " devices " + i, i === 0);
        if (clips) {
            add(p + " clip_slots 0", {has_clip: 1});
            add(p + " clip_slots 1", {has_clip: 0});
            for (const suffix of [" clip_slots 0 clip", " arrangement_clips 0"]) {
                add(p + suffix, {name: "Generated clip", is_audio_clip: 1, warping: 1, warp_mode: 0,
                    looping: 0, start_time: 8, end_time: 24, start_marker: 0, end_marker: 16,
                    loop_start: 0, loop_end: 16});
            }
        }
    }
    add("live_set", {}, {tracks: trackCount, return_tracks: 1});
    for (const i of Array.from({length: trackCount}, (_, i) => i)) track("live_set tracks " + i, true);
    track("live_set return_tracks 0", false); track("live_set master_track", false);
    const byId = new Map([...data].map(([p, row]) => [row.id, {p, row}]));
    // Every object built is recorded, so tests can prove each one is handed back
    // exactly once, after the read's last use of any object, and never used again.
    const built = [], entries = new Map(), clock = {tick: 0};
    const markReleased = entry => { entry.released = true; entry.releasedAt = ++clock.tick; };
    const factory = (requestedPath, id) => {
        const indexed = byId.get(/^id \d+$/.test(requestedPath) ? Number(requestedPath.slice(3)) : id);
        const p = indexed ? indexed.p : requestedPath;
        const row = indexed ? indexed.row : data.get(p);
        const entry = {path: p, released: false, releasedAt: 0, lastRead: 0, writes: []};
        const quote = text => host.quoted ? '"' + text + '"' : text;
        // A capability trap makes mutation/property access fail the unchanged
        // reader tests, even if someone adds it under a conditional branch.
        const api = new Proxy({}, {
            set(_, key, value) {
                if (!host.releaseWrites || key !== "path") throw new Error("Forbidden Live property write " + String(key));
                if (value !== "") throw new Error("Forbidden Live retarget");
                if (entry.released) throw new Error("Live object released twice: " + p);
                if (host.refuses) throw new Error("Live refused to clear " + p);
                entry.writes.push(key); markReleased(entry);
                return true;
            },
            defineProperty() { throw new Error("Forbidden Live property definition"); },
            deleteProperty() { throw new Error("Forbidden Live property deletion"); },
            get(_, key) {
            if (!["id", "path", "children", "get", "getcount"].includes(key)) throw new Error("Forbidden Live capability " + key);
            if (entry.released) {
                // A released object names nothing. Only the device's read-back may
                // look at it; any other use means it was released too early.
                if (key === "path") return quote(host.keepsPath ? p : "");
                if (key === "id") return Object.hasOwn(host, "idAfterRelease") ? host.idAfterRelease : 0;
                throw new Error("Released Live object used: " + p + " / " + key);
            }
            if (key === "path") return quote(p);
            entry.lastRead = ++clock.tick;
            if (key === "id") return row ? row.id : 0;
            if (key === "children") return row ? Object.keys(row.counts) : [];
            if (key === "get") return prop => {
                if (Object.hasOwn(row.props, prop)) return [row.props[prop]];
                if (Object.hasOwn(row.counts, prop)) {
                    return Array.from({length: row.counts[prop]}, (_, i) =>
                        ["id", data.get(p + " " + prop + " " + i)?.id || 0]).flat();
                }
                const child = data.get(p + " " + prop);
                return child ? ["id", child.id] : [];
            };
            return child => row.counts[child];
        }});
        built.push(entry); entries.set(api, entry);
        return api;
    };
    // The reader tests' release hook hands an object back with no Live semantics.
    const release = api => {
        const entry = entries.get(api);
        if (!entry) throw new Error("Released an object this factory did not build");
        if (entry.released) throw new Error("Live object released twice: " + entry.path);
        markReleased(entry);
    };
    return {data, factory, release, built};
}
// Every object built for one read was handed back, only after the result was
// complete, and the record says so.
function assertReleased(built, result) {
    assert.deepEqual(built.filter(o => !o.released).map(o => o.path), []);
    const lastRead = built.reduce((n, o) => Math.max(n, o.lastRead), 0);
    assert.ok(built.every(o => o.releasedAt > lastRead), "released before the result was complete");
    assert.equal(result.live_objects_created, built.length);
    assert.equal(result.live_objects_released, built.length);
    assert.equal(result.release_error, null);
    assert.ok(result.release_elapsed_ms >= 0);
}
function read(f, factory = f.factory, clock = Date.now, release = f.release) {
    const first = f.built.length, result = readSession(factory, clock, release);
    assertReleased(f.built.slice(first), result);
    return result;
}

test("both clip views, absent slots, nested devices and raw/display values", () => {
    const f = fixture(), before = JSON.stringify([...f.data]);
    const result = read(f);
    assert.equal(result.disposition, "ok");
    const t = result.observation.tracks[0];
    assert.equal(t.session_clips[0].view, "session");
    assert.equal(t.session_clips[0].start_beats, null);
    assert.equal(t.session_clips[1].present, false);
    assert.equal(t.arrangement_clips[0].start_beats, 8);
    assert.equal(t.arrangement_clips[0].length_beats, 16);
    assert.equal(t.devices[0].parameters[0].value, 0.5);
    assert.equal(t.devices[0].parameters[0].display_value, 1000);
    assert.equal(t.devices[0].chains[0].devices.length, 1);
    assert.equal(JSON.stringify([...f.data]), before);
});
test("reads changed unsaved state afresh; no persistent identity handle", () => {
    const f = fixture(), first = read(f);
    f.data.get("live_set tracks 0").props.name = "Changed since Save";
    const built = f.built.length, second = read(f);
    // The second read builds its own objects; nothing from the first is reused.
    assert.equal(f.built.length, 2 * built);
    assert.notEqual(first.observation.tracks[0].name, second.observation.tracks[0].name);
    assert.equal(second.observation.tracks[0].name, "Changed since Save");
    assert.equal(second.handle, undefined); assert.equal(second.observed_revision, undefined);
});
test("empty tracks is a successful reachable song, not a missing device", () => {
    const f = fixture(0, 0, 0), result = read(f);
    assert.equal(result.disposition, "ok"); assert.deepEqual(result.observation.tracks, []);
});
test("deleted/reordered object mid-read fails with no partial observation", () => {
    const f = fixture(), seen = new Map();
    const result = read(f, p => {
        seen.set(p, (seen.get(p) || 0) + 1);
        if (p === "live_set tracks 0 devices 1" && seen.get(p) === 2) f.data.get(p).id += 1000;
        return f.factory(p);
    });
    assert.equal(result.disposition, "path_invalid"); assert.equal(result.observation, null);
});
test("unsupported parameter is explicit, not invented", () => {
    const f = fixture(); delete f.data.get("live_set tracks 0 devices 0 parameters 0").props.display_value;
    const result = read(f);
    assert.equal(result.disposition, "unsupported_property"); assert.match(result.error, /display_value/);
});
test("same-count parameter reorder during a read invalidates the entire observation", () => {
    const f = fixture(), seen = {n: 0};
    const result = read(f, (p, id) => {
        if (p === "live_set tracks 0 devices 0" && ++seen.n === 2) {
            const a = f.data.get(p + " parameters 0"), b = f.data.get(p + " parameters 1");
            f.data.set(p + " parameters 0", b); f.data.set(p + " parameters 1", a);
        }
        return f.factory(p, id);
    });
    assert.equal(result.disposition, "path_invalid"); assert.equal(result.observation, null);
    assert.match(result.error, /identity_validation/);
});
test("MIDI/unwarped markers retain distinct units", () => {
    const f = fixture();
    f.data.get("live_set tracks 0 arrangement_clips 0").props.warping = 0;
    f.data.get("live_set tracks 0 clip_slots 0 clip").props.is_audio_clip = 0;
    const t = read(f).observation.tracks[0];
    assert.equal(t.arrangement_clips[0].markers.unit, "source_seconds");
    assert.equal(t.session_clips[0].warp_on, null);
});
test("budget failure is bounded and does not expose partial success", () => {
    const f = fixture(), ticks = {n: 0};
    const result = read(f, f.factory, () => (ticks.n += 31000));
    assert.equal(result.disposition, "read_limit"); assert.equal(result.observation, null);
    assert.deepEqual(f.built, []);
});
test("every Live object is released once, after its last read, on success and every failure path", () => {
    const cases = {
        ok: f => [f.factory, Date.now, "ok"],
        "object vanishes during traversal": f => {
            f.data.delete("live_set tracks 1 devices 0");
            return [f.factory, Date.now, "path_invalid"];
        },
        "owner changes before identity validation": f => {
            const seen = new Map();
            return [(p, id) => {
                seen.set(p, (seen.get(p) || 0) + 1);
                if (p === "live_set tracks 1" && seen.get(p) === 2) f.data.get(p).id += 1000;
                return f.factory(p, id);
            }, Date.now, "path_invalid"];
        },
        "unsupported property": f => {
            delete f.data.get("live_set tracks 1 devices 1 parameters 2").props.min;
            return [f.factory, Date.now, "unsupported_property"];
        },
        "time budget runs out mid-read": f => {
            let calls = 0;
            return [f.factory, () => (++calls > 40 ? 31000 * calls : 0), "read_limit"];
        },
        "Live refuses to build an object": f => {
            let builds = 0;
            return [(p, id) => {
                if (++builds === 12) throw new Error("LiveAPI construction failed");
                return f.factory(p, id);
            }, Date.now, "path_invalid"];
        },
        "Live raises during a get": f => {
            Object.defineProperty(f.data.get("live_set tracks 0 clip_slots 0 clip").props, "looping",
                {enumerable: true, get() { throw new Error("host error"); }});
            return [f.factory, Date.now, "path_invalid"];
        },
    };
    for (const [name, arrange] of Object.entries(cases)) {
        const f = fixture(), [factory, clock, disposition] = arrange(f);
        const result = read(f, factory, clock);
        assert.equal(result.disposition, disposition, name);
        if (disposition !== "ok") assert.equal(result.observation, null, name);
        assert.ok(result.live_objects_created > 0, name);
    }
});
test("a failed release does not skip the others or hide the complete read", () => {
    const f = fixture();
    let handed = 0;
    const result = readSession(f.factory, Date.now, api => {
        if (++handed === 3) throw new Error("host refused");
        f.release(api);
    });
    assert.equal(result.disposition, "ok"); assert.ok(result.observation.tracks.length);
    assert.equal(handed, f.built.length); assert.equal(f.built.filter(o => !o.released).length, 1);
    assert.equal(result.live_objects_created, f.built.length);
    assert.equal(result.live_objects_released, f.built.length - 1);
    assert.equal(result.release_error, "1 of " + f.built.length + " Live objects were not released: host refused");
});
test("a read needs a release hook before it builds anything", () => {
    const f = fixture();
    assert.throws(() => readSession(f.factory, Date.now), TypeError);
    assert.deepEqual(f.built, []);
});
test("production reader exposes no mutation operation and builds no eval code", () => {
    const api = fixture().factory("live_set");
    assert.throws(() => api.set("tempo", 120), /Forbidden Live capability/);
    assert.throws(() => { api.id = 0; }, /Forbidden Live property write/);
    assert.throws(() => { api.path = ""; }, /Forbidden Live property write/);
    assert.throws(() => Object.defineProperty(api, "path", {value: "anything"}), /Forbidden/);
    assert.throws(() => { delete api.id; }, /Forbidden/);
    // The device's release clears the path once; it can't retarget, and nothing
    // may read the object afterwards.
    const device = fixture(1, 1, 1, {releaseWrites: true}).factory("live_set");
    assert.throws(() => { device.path = "live_set tracks 1"; }, /Forbidden Live retarget/);
    assert.throws(() => { device.mode = 1; }, /Forbidden Live property write/);
    device.path = "";
    assert.equal(device.path, ""); assert.equal(device.id, 0);
    assert.throws(() => device.get("name"), /Released Live object used/);
    assert.throws(() => { device.path = ""; }, /released twice/);
    assert.deepEqual(Object.keys(require(readerPath)), ["readSession"]);
    for (const file of [readerPath, devicePath]) {
        const code = fs.readFileSync(file, "utf8");
        assert.doesNotMatch(code, /\.(?:call|set|goto)\s*\(/);
        assert.doesNotMatch(code, /\[\s*["'](?:call|set|goto)["']\s*\]/);
        assert.doesNotMatch(code, /\beval\s*\(|new Function/);
        assert.doesNotMatch(code, /\.(?:mode|id|property|unquotedpath)\s*=(?!=)/);
        assert.doesNotMatch(code, /\[\s*["'](?:path|mode|id|property)["']\s*\]\s*=(?!=)/);
    }
    // Releasing belongs to the device layer, never the reader, and its only write
    // to a Live object is the empty path.
    assert.doesNotMatch(fs.readFileSync(readerPath, "utf8"), /\.path\s*=(?!=)/);
    const writes = fs.readFileSync(devicePath, "utf8").match(/\.path\s*=(?!=)[^;\n]*/g) || [];
    assert.deepEqual(writes, ['.path = ""']);
});
function loadDevice(f) {
    const messages = [], dictionaries = new Map(), posts = [];
    const context = {BasteReader: {readSession}, include() {}, LiveAPI: function (_, p) { return f.factory(p); },
        outlet: (...args) => messages.push(args), post: text => posts.push(text), Dict: function (name) {
            this.name = name;
            // Reproduce Max's observed boolean-to-atom coercion at the wire boundary.
            this.parse = s => dictionaries.set(name, JSON.parse(s, (_, v) => typeof v === "boolean" ? Number(v) : v));
            this.freepeer = () => dictionaries.delete(name);
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(devicePath, "utf8"), context);
    const result = id => JSON.parse(dictionaries.get("pocket_baste_" + id).json_chunks.join(""));
    return {context, messages, dictionaries, posts, result};
}
// The device cleared every object's path, and nothing else, after the result.
function assertDeviceReleasedAll(f, result) {
    assert.ok(f.built.length > 0, "the read built no Live objects");
    assert.deepEqual(f.built.filter(o => o.writes.join() !== "path").map(o => o.path), []);
    assertReleased(f.built, result);
}
test("Max entry point waits for live.thisdevice, releases replies and returns a fresh read", () => {
    const f = fixture(2, 2, 3, {releaseWrites: true});
    const unicodeName = "🪡🎛️".repeat(3000);
    f.data.get("live_set tracks 0").props.name = unicodeName;
    const {context, messages, dictionaries, posts, result} = loadDevice(f);
    context.probe(); assert.equal(messages.length, 0);
    context.observe("a"); assert.equal(result("a").disposition, "device_not_loaded");
    assert.deepEqual(f.built, []);
    context.release("a"); context.bang(); context.probe(); assert.deepEqual(messages.at(-1), [0, "ready"]);
    context.observe("b"); assert.equal(result("b").disposition, "ok");
    assertDeviceReleasedAll(f, result("b")); assert.deepEqual(posts, []);
    assert.equal(result("b").observation.tracks[0].session_clips[1].present, false);
    assert.equal(result("b").observation.tracks[0].devices[0].enabled, true);
    assert.ok(dictionaries.get("pocket_baste_b").json_chunks.length > 1);
    assert.equal(result("b").observation.tracks[0].name, unicodeName);
    for (const chunk of dictionaries.get("pocket_baste_b").json_chunks) {
        const last = chunk.charCodeAt(chunk.length - 1);
        assert.ok(chunk.length <= 4096 && !(last >= 0xD800 && last <= 0xDBFF));
    }
    context.release("b"); assert.equal(dictionaries.size, 0);
});
test("Max entry point clears the path of every object, even on failure", () => {
    const f = fixture(2, 2, 3, {releaseWrites: true});
    delete f.data.get("live_set tracks 1 devices 0 parameters 1").props.max;
    const {context, posts, result} = loadDevice(f);
    context.bang(); context.observe("c");
    assert.equal(result("c").disposition, "unsupported_property"); assert.equal(result("c").observation, null);
    assertDeviceReleasedAll(f, result("c")); assert.deepEqual(posts, []);
});
test("Max entry point accepts only an empty path and a documented no-object id after release", () => {
    // Max documents `id` as a number and, in Max 8, as a string; "id 0" names no
    // object. LiveAPI's path can come back quoted.
    for (const host of [{idAfterRelease: 0}, {idAfterRelease: "0"}, {idAfterRelease: "id 0"}, {quoted: true}]) {
        const f = fixture(1, 1, 1, {releaseWrites: true, ...host});
        const {context, posts, result} = loadDevice(f);
        context.bang(); context.observe("e");
        assert.equal(result("e").disposition, "ok"); assertDeviceReleasedAll(f, result("e"));
        assert.deepEqual(posts, [], JSON.stringify(host));
    }
});
test("Max entry point reports an unconfirmed release in the reply and the Max window", () => {
    // Number("id 5") is NaN, so a numeric test would miss the string forms.
    const cases = [[{keepsPath: true}, /path still reads "live_set[^"]*" after clearing it/],
        [{keepsPath: true, quoted: true}, /path still reads "live_set[^"]*" after clearing it/],
        [{refuses: true}, /Live refused to clear live_set/],
        ...["id 5", "5", 5, "", undefined].map(id => [{idAfterRelease: id}, /id still reads ".*" after clearing its path/])];
    for (const [host, reason] of cases) {
        const f = fixture(1, 1, 1, {releaseWrites: true, ...host}), label = JSON.stringify(host);
        const {context, posts, result} = loadDevice(f);
        context.bang(); context.observe("d");
        // The read was complete before release, so it stands; the shortfall is
        // reported beside it rather than hidden or turned into another result.
        const reply = result("d"), n = f.built.length;
        assert.equal(reply.disposition, "ok", label); assert.equal(reply.observation.tracks.length, 1, label);
        assert.equal(reply.live_objects_created, n, label); assert.equal(reply.live_objects_released, 0, label);
        assert.ok(reply.release_error.startsWith(n + " of " + n + " Live objects were not released: "), label);
        assert.match(reply.release_error, reason, label);
        assert.deepEqual(posts, ["Baste: " + reply.release_error + ". Reload the device if Live edits slow down.\n"]);
    }
});
test("representative generated read has an explicit latency budget", () => {
    const f = fixture(24, 6, 16), start = performance.now(), result = read(f);
    const elapsed = performance.now() - start;
    assert.equal(result.disposition, "ok"); assert.ok(elapsed < 1000);
    console.log(JSON.stringify({generated_read_ms: elapsed, objects: result.observation.objects_read,
        released: result.live_objects_released, release_ms: result.release_elapsed_ms}));
});
