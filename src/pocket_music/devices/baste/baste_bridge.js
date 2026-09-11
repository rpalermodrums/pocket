"use strict";
// Transport only. This process has no LiveAPI capability and no mutation route.
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");

function startBridge(maxAPI, directory = process.env.POCKET_BASTE_DIR || path.join(os.homedir(), ".pocket/baste")) {
    const token = crypto.randomBytes(32).toString("hex");
    const instance = crypto.randomBytes(16).toString("hex");
    const pending = new Map();
    const state = {server: null, descriptor: null, probe: null};
    function send(res, status, body) {
        if (res.destroyed || res.writableEnded) return;
        res.writeHead(status, {"Content-Type": "application/json", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'", "X-Content-Type-Options": "nosniff"});
        res.end(JSON.stringify(body));
    }
    function authenticated(req) {
        const provided = Buffer.from(req.headers.authorization || "");
        const expected = Buffer.from("Bearer " + token);
        return provided.length === expected.length && crypto.timingSafeEqual(provided, expected);
    }
    function ready() {
        if (state.server) return;
        clearInterval(state.probe);
        const server = http.createServer((req, res) => {
            if (req.socket.remoteAddress !== "127.0.0.1" || !authenticated(req) || req.headers.origin) {
                return send(res, 403, {error: "Loopback device client only"});
            }
            if (req.method === "GET" && req.url === "/health") {
                return send(res, 200, {schema: "pocket.baste-health/v1", instance_id: instance});
            }
            if (req.method !== "POST" || req.url !== "/observe") return send(res, 404, {error: "Unknown route"});
            const chunks = [];
            const received = {bytes: 0};
            req.on("data", chunk => {
                received.bytes += chunk.length;
                if (received.bytes > 4096) { send(res, 413, {error: "Request too large"}); req.destroy(); }
                else chunks.push(chunk);
            });
            req.on("end", () => {
                try {
                    const input = JSON.parse(Buffer.concat(chunks).toString());
                    const id = input.request_id;
                    if (!/^[a-f0-9]{32}$/.test(id) || !Number.isInteger(input.timeout_ms) ||
                            input.timeout_ms < 1 || input.timeout_ms > 60000 ||
                            Object.keys(input).sort().join(",") !== "request_id,timeout_ms") {
                        return send(res, 400, {error: "Invalid observation request"});
                    }
                    if (pending.size) return send(res, 409, {error: "A read is already in progress"});
                    const timer = setTimeout(() => {
                        // Timing out the client cannot cancel a synchronous
                        // LiveAPI read. Keep its slot until Max actually replies.
                        pending.get(id).timedOut = true;
                        send(res, 200, {schema: "pocket.live-observation/v1", request_id: id,
                            disposition: "observation_timeout", observation: null});
                    }, input.timeout_ms);
                    pending.set(id, {res, timer, timedOut: false});
                    maxAPI.outlet("observe", id);
                } catch (_) { send(res, 400, {error: "Invalid JSON"}); }
            });
        });
        state.server = server;
        server.requestTimeout = 65000;
        server.on("error", err => { maxAPI.post("Baste bridge: " + err.message); });
        server.listen(0, "127.0.0.1", () => {
            fs.mkdirSync(directory, {recursive: true, mode: 0o700});
            state.descriptor = path.join(directory, "connection-" + instance + ".json");
            fs.writeFileSync(state.descriptor, JSON.stringify({schema: "pocket.baste-connection/v1",
                host: "127.0.0.1", port: server.address().port, token, instance_id: instance}),
                {flag: "wx", mode: 0o600});
        });
    }
    maxAPI.addHandler("ready", ready);
    maxAPI.addHandler("result", async (id, dictName) => {
        const item = pending.get(id);
        if (!item) { maxAPI.outlet("release", id); return; }
        try {
            if (item.timedOut) return;
            const wire = await maxAPI.getDict(dictName);
            if (item.timedOut || pending.get(id) !== item) return;
            if (wire.schema !== "pocket.baste-wire/v1" || wire.request_id !== id ||
                    !Array.isArray(wire.json_chunks) || wire.json_chunks.length > 2048 ||
                    !wire.json_chunks.every(chunk => typeof chunk === "string" && chunk.length <= 4096)) {
                throw new Error("Invalid device wire record");
            }
            const data = JSON.parse(wire.json_chunks.join(""));
            if (data.request_id !== id) throw new Error("Mismatched device reply");
            send(item.res, 200, data);
        } catch (err) {
            send(item.res, 200, {schema: "pocket.live-observation/v1", request_id: id,
                disposition: "bridge_error", observation: null, error: String(err.message)});
        } finally {
            clearTimeout(item.timer);
            if (pending.get(id) === item) pending.delete(id);
            maxAPI.outlet("release", id);
        }
    });
    state.probe = setInterval(() => maxAPI.outlet("probe"), 250);
    function close() {
        clearInterval(state.probe);
        for (const item of pending.values()) { clearTimeout(item.timer); item.res.destroy(); }
        pending.clear();
        if (state.server) state.server.close();
        if (state.descriptor) { try { fs.unlinkSync(state.descriptor); } catch (_) { /* already gone */ } }
    }
    return {close, ready};
}
module.exports = {startBridge};
if (process.env.MAX_ENV || require.main === module) {
    const bridge = startBridge(require("max-api"));
    process.on("exit", bridge.close);
    process.on("SIGTERM", () => { bridge.close(); process.exit(0); });
}
