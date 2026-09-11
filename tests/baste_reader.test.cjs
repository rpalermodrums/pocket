"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readerPath = path.resolve(__dirname, "../src/pocket_music/devices/baste/baste_reader.js");
const {readSession} = require(readerPath);

function fixture(trackCount = 2, deviceCount = 2, parameterCount = 3) {
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
    const factory = (requestedPath, id) => {
        const indexed = byId.get(/^id \d+$/.test(requestedPath) ? Number(requestedPath.slice(3)) : id);
        const p = indexed ? indexed.p : requestedPath;
        const row = indexed ? indexed.row : data.get(p);
        // A capability trap makes mutation/property access fail the unchanged
        // reader tests, even if someone adds it under a conditional branch.
        return new Proxy({}, {set() { throw new Error("Forbidden Live property write"); },
            defineProperty() { throw new Error("Forbidden Live property definition"); },
            deleteProperty() { throw new Error("Forbidden Live property deletion"); },
            get(_, key) {
            if (!["id", "children", "get", "getcount"].includes(key)) throw new Error("Forbidden Live capability " + key);
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
    };
    return {data, factory};
}

test("both clip views, absent slots, nested devices and raw/display values", () => {
    const f = fixture(), before = JSON.stringify([...f.data]);
    const result = readSession(f.factory, Date.now);
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
    const f = fixture(), first = readSession(f.factory, Date.now);
    f.data.get("live_set tracks 0").props.name = "Changed since Save";
    const second = readSession(f.factory, Date.now);
    assert.notEqual(first.observation.tracks[0].name, second.observation.tracks[0].name);
    assert.equal(second.observation.tracks[0].name, "Changed since Save");
    assert.equal(second.handle, undefined); assert.equal(second.observed_revision, undefined);
});
test("empty tracks is a successful reachable song, not a missing device", () => {
    const f = fixture(0, 0, 0), result = readSession(f.factory, Date.now);
    assert.equal(result.disposition, "ok"); assert.deepEqual(result.observation.tracks, []);
});
test("deleted/reordered object mid-read fails with no partial observation", () => {
    const f = fixture(), seen = new Map();
    const result = readSession(p => {
        seen.set(p, (seen.get(p) || 0) + 1);
        if (p === "live_set tracks 0 devices 1" && seen.get(p) === 2) f.data.get(p).id += 1000;
        return f.factory(p);
    }, Date.now);
    assert.equal(result.disposition, "path_invalid"); assert.equal(result.observation, null);
});
test("unsupported parameter is explicit, not invented", () => {
    const f = fixture(); delete f.data.get("live_set tracks 0 devices 0 parameters 0").props.display_value;
    const result = readSession(f.factory, Date.now);
    assert.equal(result.disposition, "unsupported_property"); assert.match(result.error, /display_value/);
});
test("same-count parameter reorder during a read invalidates the entire observation", () => {
    const f = fixture(), seen = {n: 0};
    const result = readSession((p, id) => {
        if (p === "live_set tracks 0 devices 0" && ++seen.n === 2) {
            const a = f.data.get(p + " parameters 0"), b = f.data.get(p + " parameters 1");
            f.data.set(p + " parameters 0", b); f.data.set(p + " parameters 1", a);
        }
        return f.factory(p, id);
    }, Date.now);
    assert.equal(result.disposition, "path_invalid"); assert.equal(result.observation, null);
});
test("MIDI/unwarped markers retain distinct units", () => {
    const f = fixture();
    f.data.get("live_set tracks 0 arrangement_clips 0").props.warping = 0;
    f.data.get("live_set tracks 0 clip_slots 0 clip").props.is_audio_clip = 0;
    const t = readSession(f.factory, Date.now).observation.tracks[0];
    assert.equal(t.arrangement_clips[0].markers.unit, "source_seconds");
    assert.equal(t.session_clips[0].warp_on, null);
});
test("budget failure is bounded and does not expose partial success", () => {
    const f = fixture(), ticks = {n: 0};
    const result = readSession(f.factory, () => (ticks.n += 31000));
    assert.equal(result.disposition, "read_limit"); assert.equal(result.observation, null);
});
test("production reader exposes no mutation operation and builds no eval code", () => {
    const api = fixture().factory("live_set");
    assert.throws(() => api.set("tempo", 120), /Forbidden Live capability/);
    assert.throws(() => { api.id = 0; }, /Forbidden Live property write/);
    assert.throws(() => Object.defineProperty(api, "path", {value: "anything"}), /Forbidden/);
    assert.throws(() => { delete api.id; }, /Forbidden/);
    for (const file of [readerPath, readerPath.replace("baste_reader", "baste_device")]) {
        const code = fs.readFileSync(file, "utf8");
        assert.doesNotMatch(code, /\.(?:call|set|goto)\s*\(/);
        assert.doesNotMatch(code, /\[\s*["'](?:call|set|goto)["']\s*\]/);
        assert.doesNotMatch(code, /\beval\s*\(|new Function/);
    }
});
test("Max entry point waits for live.thisdevice, releases replies and returns a fresh read", () => {
    const f = fixture(), messages = [], dictionaries = new Map();
    const unicodeName = "🪡🎛️".repeat(3000);
    f.data.get("live_set tracks 0").props.name = unicodeName;
    const context = {BasteReader: {readSession}, include() {}, LiveAPI: function (_, p) { return f.factory(p); },
        outlet: (...args) => messages.push(args), Dict: function (name) {
            this.name = name;
            // Reproduce Max's observed boolean-to-atom coercion at the wire boundary.
            this.parse = s => dictionaries.set(name, JSON.parse(s, (_, v) => typeof v === "boolean" ? Number(v) : v));
            this.freepeer = () => dictionaries.delete(name);
        }};
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(readerPath.replace("baste_reader", "baste_device"), "utf8"), context);
    context.probe(); assert.equal(messages.length, 0);
    const result = id => JSON.parse(dictionaries.get("pocket_baste_" + id).json_chunks.join(""));
    context.observe("a"); assert.equal(result("a").disposition, "device_not_loaded");
    context.release("a"); context.bang(); context.probe(); assert.deepEqual(messages.at(-1), [0, "ready"]);
    context.observe("b"); assert.equal(result("b").disposition, "ok");
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
test("representative generated read has an explicit latency budget", () => {
    const f = fixture(24, 6, 16), start = performance.now(), result = readSession(f.factory, Date.now);
    const elapsed = performance.now() - start;
    assert.equal(result.disposition, "ok"); assert.ok(elapsed < 1000);
    console.log(JSON.stringify({generated_read_ms: elapsed, objects: result.observation.objects_read}));
});
