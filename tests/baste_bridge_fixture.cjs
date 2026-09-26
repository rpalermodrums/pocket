// SPDX-License-Identifier: AGPL-3.0-only
// Real loopback transport, simulated Max dictionaries. No claim of native execution.
const {startBridge} = require("../src/pocket_music/devices/baste/baste_bridge.js");
const handlers = {};
const max = {
    addHandler(name, handler) { handlers[name] = handler; },
    post() {},
    outlet(name, id) {
        if (name === "observe") setImmediate(() => handlers.result(id, id));
    },
    async getDict(id) {
        const data = {schema: "pocket.live-observation/v1", request_id: id, disposition: "ok",
            observation: {tracks: [], return_tracks: [], main_track: {name: "Main"}},
            read_started_at: new Date().toISOString(), read_ended_at: new Date().toISOString(), read_elapsed_ms: 1,
            live_objects_created: 5, live_objects_released: 5, release_elapsed_ms: 0, release_error: null};
        return {schema: "pocket.baste-wire/v1", request_id: id, json_chunks: [JSON.stringify(data)]};
    }
};
const bridge = startBridge(max, process.argv[2]);
bridge.ready();
process.on("SIGTERM", () => { bridge.close(); process.exit(0); });
