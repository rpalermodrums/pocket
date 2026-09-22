/* Separate supervised note writer. Explicit prepared requests, double guard, one whitelisted call. */
autowatch = 0;
inlets = 1;
outlets = 1;
include("reader.js");
include("writer.js");
var preparedWrites = {};
var seenWrites = {};
var inboundWriteDictionary = null;
var readyState = false;
var sessionNonce = null;
var packageSha256 = null;
var replies = {};
function bang() { readyState = true; }
function probe() { if (readyState) { outlet(0, "ready"); } }
function bind_session(nonce, packageHash) {
    if (!readyState || !/^[0-9a-f]{32}$/.test(String(nonce)) || !/^[0-9a-f]{64}$/.test(String(packageHash))) { return; }
    sessionNonce = String(nonce); packageSha256 = String(packageHash);
    inboundWriteDictionary = new Dict("pocket_write_input_" + sessionNonce);
    inboundWriteDictionary.parse(JSON.stringify({json_chunks: []}));
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

function readSnapshot(request) {
    return NativeMidiCore.read(function (path) { return new LiveAPI(null, path); }, request.target);
}
function prepare_write(key, nonce, dictionaryName) {
    key = String(key); nonce = String(nonce);
    if (!/^[0-9a-f]{64}$/.test(key)) { return; }
    var result = {schema: "pocket.native-midi-write-result/v1", request_key: key, session_nonce: sessionNonce,
        package_sha256: packageSha256, protocol: "pocket.native-midi-wire/v1", stage: "prepare", outcome: "outcome_unknown",
        dispatch_count: 0, before: null, after: null, method: null, dictionary: null, call_return: null,
        started_at: new Date().toISOString()};
    try {
        if (!readyState || !sessionNonce || nonce !== sessionNonce || seenWrites[key]) { throw new Error("stale_session_or_repeated_request"); }
        seenWrites[key] = true;
        var inbound = new Dict(String(dictionaryName));
        var wire = JSON.parse(inbound.stringify());
        if (!Array.isArray(wire.json_chunks)) { throw new Error("Invalid write dictionary transport"); }
        var request = JSON.parse(wire.json_chunks.join(""));
        if (request.request_key !== key || request.session_nonce !== sessionNonce || request.package_sha256 !== packageSha256) { throw new Error("Write request identity mismatch"); }
        result.before = readSnapshot(request);
        var plan = NativeMidiWriter.plan(request, result.before);
        result.method = plan.method; result.dictionary = plan.dictionary;
        preparedWrites[key] = {request: request, before: result.before, plan: plan};
        result.outcome = "ready";
    } catch (errorValue) { result.error = String(errorValue).slice(0, 2000); if (result.before !== null) { result.outcome = "refused_before_dispatch"; } }
    result.ended_at = new Date().toISOString(); reply(key, result);
}
function commit_write(key, nonce) {
    key = String(key); nonce = String(nonce);
    var saved = preparedWrites[key]; delete preparedWrites[key];
    var result = {schema: "pocket.native-midi-write-result/v1", request_key: key, session_nonce: sessionNonce,
        package_sha256: packageSha256, protocol: "pocket.native-midi-wire/v1", stage: "commit", outcome: "outcome_unknown",
        dispatch_count: 0, before: null, after: null, method: null, dictionary: null, call_return: null,
        started_at: new Date().toISOString()};
    try {
        if (!readyState || nonce !== sessionNonce || !saved) { throw new Error("Missing unique prepared write"); }
        result.before = readSnapshot(saved.request);
        var plan = NativeMidiWriter.plan(saved.request, result.before);
        var dictionary = new Dict(); dictionary.parse(JSON.stringify(plan.dictionary));
        if (NativeMidiWriter.canonical(JSON.parse(dictionary.stringify())) !== NativeMidiWriter.canonical(plan.dictionary)) { dictionary.freepeer(); throw new Error("Max note dictionary roundtrip changed payload"); }
        result.method = plan.method; result.dictionary = plan.dictionary; result.dispatch_count = 1;
        try { result.call_return = new LiveAPI(null, saved.request.expected.target.path).call(plan.method, dictionary); }
        finally { dictionary.freepeer(); }
        result.after = readSnapshot(saved.request);
        NativeMidiWriter.verify(saved.request, result.before, result.after);
        result.outcome = "verified_readback";
    } catch (errorValue) { result.error = String(errorValue).slice(0, 2000); if (result.dispatch_count === 0 && result.before !== null) { result.outcome = "refused_before_dispatch"; } }
    result.ended_at = new Date().toISOString(); reply(key, result);
}
function cancel_write(key) { delete preparedWrites[String(key)]; }
