"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const {setTimeout: delay} = require("node:timers/promises");
const bridgePath = path.resolve(__dirname, "../src/pocket_music/devices/baste/baste_bridge.js");
const {startBridge} = require(bridgePath);

test("Node for Max runner starts without require.main being the script", () => {
    const handlers = {}, exits = {};
    const max = {addHandler(name, fn) { handlers[name] = fn; }};
    const context = {module: {exports: {}}, Buffer, Map,
        process: {env: {MAX_ENV: "max"}, on(name, fn) { exits[name] = fn; }},
        require: name => name === "max-api" ? max : require(name),
        setInterval: () => 1, clearInterval() {}};
    vm.runInNewContext(fs.readFileSync(bridgePath, "utf8"), context);
    assert.equal(typeof handlers.ready, "function");
    assert.equal(typeof handlers.result, "function");
    assert.equal(typeof exits.exit, "function");
    exits.exit();
});

test("busy, timeout and late responses cannot become another request's observation", async () => {
    const folder = fs.mkdtempSync(path.join(os.tmpdir(), "pocket-baste-test-"));
    const handlers = {}, calls = [], dictionaries = new Map();
    const bridge = startBridge({addHandler(name, fn) { handlers[name] = fn; }, post() {},
        outlet(...args) { calls.push(args); }, getDict: async name => dictionaries.get(name)}, folder);
    try {
        bridge.ready();
        for (const _ of Array.from({length: 100})) {
            if (fs.readdirSync(folder).length) break;
            await delay(5);
        }
        const connection = JSON.parse(fs.readFileSync(path.join(folder, fs.readdirSync(folder)[0])));
        const request = async (id, timeout) => {
            const response = await fetch(`http://127.0.0.1:${connection.port}/observe`, {
                method: "POST", headers: {Authorization: "Bearer " + connection.token},
                body: JSON.stringify({request_id: id, timeout_ms: timeout})});
            return {status: response.status, body: await response.json()};
        };
        const firstId = "a".repeat(32), nextId = "b".repeat(32);
        const first = request(firstId, 250);
        for (const _ of Array.from({length: 100})) {
            if (calls.some(c => c[0] === "observe")) break;
            await delay(2);
        }
        assert.equal((await request(nextId, 1000)).status, 409);
        assert.equal((await first).body.disposition, "observation_timeout");
        assert.equal((await request(nextId, 1000)).status, 409); // Max is still reading; do not queue work.
        await handlers.result(firstId, "timed-out-dictionary");
        const second = request(nextId, 1000);
        for (const _ of Array.from({length: 100})) {
            if (calls.some(c => c[0] === "observe" && c[1] === nextId)) break;
            await delay(2);
        }
        await handlers.result(firstId, "old-dictionary"); // No pending match; discard it.
        dictionaries.set("new-dictionary", {schema: "pocket.baste-wire/v1", request_id: nextId,
            json_chunks: [JSON.stringify({request_id: nextId, schema: "pocket.live-observation/v1",
                disposition: "ok", observation: {tracks: [], return_tracks: [], main_track: {}}})]});
        await handlers.result(nextId, "new-dictionary");
        const result = await second;
        assert.equal(result.body.request_id, nextId);
        assert.equal(result.body.disposition, "ok");
        assert.ok(calls.some(c => c[0] === "release" && c[1] === firstId));
        assert.ok(calls.some(c => c[0] === "release" && c[1] === nextId));

        // A dictionary transfer can outlive its timer. The response handler
        // must not write headers twice or clear a different pending request.
        const slowId = "c".repeat(32), transfer = {};
        dictionaries.set("slow-dictionary", new Promise(resolve => { transfer.finish = resolve; }));
        const slow = request(slowId, 40);
        for (const _ of Array.from({length: 100})) {
            if (calls.some(c => c[0] === "observe" && c[1] === slowId)) break;
            await delay(2);
        }
        const handler = handlers.result(slowId, "slow-dictionary");
        assert.equal((await slow).body.disposition, "observation_timeout");
        assert.equal((await request(nextId, 1000)).status, 409);
        transfer.finish({request_id: slowId, disposition: "ok"});
        await handler;
    } finally { bridge.close(); fs.rmSync(folder, {recursive: true}); }
});
