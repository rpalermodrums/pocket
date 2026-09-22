/* Max adapter. Read/status build: no native mutation function or arbitrary call route. */
autowatch = 0;
inlets = 1;
outlets = 1;
include("core.js");
var readyState = false;
var sessionNonce = null;
var packageSha256 = null;
var replies = {};
function bang() { readyState = true; }
function probe() { if (readyState) { outlet(0, "ready"); } }
function bind_session(nonce, packageHash) {
    if (!readyState || !/^[0-9a-f]{32}$/.test(String(nonce)) || !/^[0-9a-f]{64}$/.test(String(packageHash))) { return; }
    sessionNonce = String(nonce); packageSha256 = String(packageHash);
    outlet(0, "bound", sessionNonce);
}
function reply(key, value) {
    var serialized = JSON.stringify(value), chunks = [];
    if (serialized.length > 2097152) { throw new Error("Native response exceeds2MiB"); }
    for (var offset = 0; offset < serialized.length;) {
        var end = Math.min(offset + 4096, serialized.length), last = serialized.charCodeAt(end - 1);
        if (last >= 0xD800 && last <= 0xDBFF) { end -= 1; }
        chunks.push(serialized.slice(offset, end)); offset = end;
    }
    var dictionary = new Dict("pocket_native_midi_" + key);
    dictionary.parse(JSON.stringify({schema: "pocket.native-midi-wire/v1", request_key: key, session_nonce: sessionNonce, json_chunks: chunks}));
    replies[key] = dictionary; outlet(0, "result", key, dictionary.name);
}
function observe(key, nonce, trackIndex, clipIndex) {
    key = String(key); nonce = String(nonce);
    if (!/^[0-9a-f]{64}$/.test(key)) { return; }
    var result = {schema: "pocket.native-midi-observation/v1", request_key: key, session_nonce: sessionNonce, package_sha256: packageSha256,
        protocol: "pocket.native-midi-wire/v1", read_started_at: new Date().toISOString(), status: "unsupported", observation: null};
    try {
        if (!readyState || !sessionNonce || nonce !== sessionNonce) { throw new Error("stale_session: no native read dispatched"); }
        result.observation = NativeMidiCore.read(function (path) { return new LiveAPI(null, path); }, {location: "arrangement", track_index: trackIndex, clip_index: clipIndex});
        result.status = "ok";
    } catch (errorValue) { result.error = String(errorValue).slice(0, 2000); result.observation = null; }
    result.read_ended_at = new Date().toISOString();
    try { reply(key, result); } catch (replyError) {
        result.status = "unsupported"; result.observation = null; result.error = String(replyError).slice(0, 2000);
        reply(key, result);
    }
}
function release(key) {
    key = String(key);
    if (replies[key]) { replies[key].freepeer(); delete replies[key]; }
}
