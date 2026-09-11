/* Max-only entry point. live.thisdevice -> bang, all requests pass deferlow. */
autowatch = 0;
inlets = 1;
outlets = 1;
include("baste_reader.js");
var deviceReady = false;
var replies = {};
function bang() { deviceReady = true; }
function probe() { if (deviceReady) { outlet(0, "ready"); } }
function observe(requestId) {
    var began = new Date().toISOString();
    var result = deviceReady ? BasteReader.readSession(function (path, id) {
        return new LiveAPI(null, id ? "id " + id : path);
    }, function () { return Date.now(); }) : {disposition: "device_not_loaded", observation: null};
    result.schema = "pocket.live-observation/v1";
    result.request_id = String(requestId);
    result.read_started_at = began;
    result.read_ended_at = new Date().toISOString();
    // Max Dict coerces JSON booleans to numeric atoms. Carry bounded chunks of
    // the serialized JSON as strings so the public record survives unchanged.
    var serialized = JSON.stringify(result), chunks = [];
    for (var offset = 0; offset < serialized.length;) {
        var end = Math.min(offset + 4096, serialized.length);
        var last = serialized.charCodeAt(end - 1);
        if (last >= 0xD800 && last <= 0xDBFF) { end -= 1; }
        chunks.push(serialized.slice(offset, end));
        offset = end;
    }
    var reply = new Dict("pocket_baste_" + requestId);
    reply.parse(JSON.stringify({schema: "pocket.baste-wire/v1", request_id: String(requestId),
        json_chunks: chunks}));
    replies[requestId] = reply;
    outlet(0, "result", requestId, reply.name);
}
function release(requestId) {
    if (replies[requestId]) { replies[requestId].freepeer(); delete replies[requestId]; }
}
