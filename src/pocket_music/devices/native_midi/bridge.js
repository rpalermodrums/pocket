/* Separate authenticated loopback reader. No write endpoint and no arbitrary API calls. */
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const http = require('http');
const max = require('max-api');
const PROTOCOL = 'pocket.native-midi-wire/v1';
const SOURCE_NAMES = ['Native MIDI.maxpat', 'bridge.js', 'core.js', 'device.js'];
const MAX_RESULT = 2097152;
const folder = path.resolve(process.env.POCKET_NATIVE_MIDI_DIR || path.join(os.homedir(), '.pocket', 'native-midi'));
const nonce = crypto.randomBytes(16).toString('hex');
const token = crypto.randomBytes(32).toString('hex');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).sort().join('|') === keys.slice().sort().join('|');
const hex = (value, length) => typeof value === 'string' && new RegExp('^[0-9a-f]{' + length + '}$').test(value);
function fail(message) { throw new Error(message); }
function privateDirectory(directory) {
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
    for (const parent of [directory, path.dirname(directory)]) {
        if (fs.lstatSync(parent).isSymbolicLink()) { fail('Bridge directory must not be a symlink'); }
    }
}
function atomic(file, value, exclusive) {
    const data = Buffer.from(JSON.stringify(value));
    const temporary = file + '.' + crypto.randomBytes(12).toString('hex') + '.tmp';
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    try {
        if (exclusive) { fs.linkSync(temporary, file); fs.unlinkSync(temporary); }
        else { fs.renameSync(temporary, file); }
        const directory = fs.openSync(path.dirname(file), 'r');
        try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    } finally { if (fs.existsSync(temporary)) { fs.unlinkSync(temporary); } }
}
function packageIdentity() {
    const manifestPath = path.join(__dirname, 'manifest.json');
    if (fs.lstatSync(manifestPath).isSymbolicLink() || fs.statSync(manifestPath).size > 32768) { fail('Unsafe package manifest'); }
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (!exact(manifest, ['schema', 'protocol', 'sources', 'package_sha256']) || manifest.schema !== 'pocket.native-midi-package/v1' || manifest.protocol !== PROTOCOL || !exact(manifest.sources, SOURCE_NAMES)) { fail('Invalid package manifest'); }
    const observed = {};
    for (const name of SOURCE_NAMES) {
        const file = path.join(__dirname, name);
        if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > MAX_RESULT) { fail('Unsafe package source'); }
        observed[name] = hash(fs.readFileSync(file));
        if (observed[name] !== manifest.sources[name]) { fail('Package source integrity mismatch'); }
    }
    const identity = hash(JSON.stringify(observed)); // source names already sorted, plain ASCII
    if (identity !== manifest.package_sha256) { fail('Package identity mismatch'); }
    return identity;
}
const packageSha256 = packageIdentity();
privateDirectory(folder);
const journalFolder = path.join(folder, 'journals', nonce);
privateDirectory(journalFolder);
const descriptorPath = path.join(folder, 'connection-' + nonce + '.json');
const pending = new Map();
let bound = false;
let listening = false;
function answer(response, status, record) {
    if (!response.destroyed && !response.writableEnded) {
        response.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
        response.end(JSON.stringify(record));
    }
}
function error(response, status, code) { answer(response, status, {schema: 'pocket.native-midi-error/v1', error: code, session_nonce: nonce}); }
function journalPath(key) { return path.join(journalFolder, key + '.json'); }
function save(entry, exclusive) { atomic(journalPath(entry.request_key), entry, exclusive); }
function requestRecord(raw) {
    const record = JSON.parse(raw);
    if (!exact(record, ['schema', 'request_key', 'session_nonce', 'target', 'timeout_ms']) || record.schema !== 'pocket.native-midi-read-request/v1' || !hex(record.request_key, 64) || record.session_nonce !== nonce || !Number.isInteger(record.timeout_ms) || record.timeout_ms < 100 || record.timeout_ms > 60000) { fail('Invalid read request'); }
    if (!exact(record.target, ['location', 'track_index', 'clip_index']) || record.target.location !== 'arrangement' || !Number.isInteger(record.target.track_index) || !Number.isInteger(record.target.clip_index) || record.target.track_index < 0 || record.target.track_index > 255 || record.target.clip_index < 0 || record.target.clip_index > 255) { fail('Invalid target'); }
    return record;
}
const server = http.createServer((request, response) => {
    if (request.socket.remoteAddress !== '127.0.0.1' || request.headers.origin !== undefined || request.headers.authorization !== 'Bearer ' + token) { error(response, 403, 'unauthorized'); request.resume(); return; }
    if (request.method === 'GET' && request.url === '/health') {
        answer(response, 200, {schema: 'pocket.native-midi-health/v1', protocol: PROTOCOL, session_nonce: nonce, package_sha256: packageSha256, ready: bound, write_available: false}); return;
    }
    if (request.method !== 'POST' || request.url !== '/read') { error(response, 404, 'unsupported_route'); request.resume(); return; }
    if (request.headers['content-type'] !== 'application/json') { error(response, 415, 'json_required'); request.resume(); return; }
    const chunks = [];
    let bytes = 0;
    let oversized = false;
    request.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 8192) { oversized = true; error(response, 413, 'request_too_large'); }
        else { chunks.push(chunk); }
    });
    request.on('end', () => {
        if (oversized || response.destroyed || response.writableEnded) { return; }
        const raw = Buffer.concat(chunks).toString('utf8');
        let record;
        try { record = requestRecord(raw); } catch (_) { error(response, 400, 'invalid_request'); return; }
        if (!bound) { error(response, 503, 'device_not_ready'); return; }
        const file = journalPath(record.request_key);
        if (fs.existsSync(file)) {
            try {
                if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > MAX_RESULT * 2) { fail('Invalid journal'); }
                const previous = JSON.parse(fs.readFileSync(file, 'utf8'));
                if (previous.request_json !== raw || previous.request_sha256 !== hash(raw)) { error(response, 409, 'request_identity_conflict'); return; }
                if (previous.state === 'complete') { answer(response, 200, {schema: 'pocket.native-midi-response/v1', replay: true, journal: previous}); return; }
                error(response, 409, 'request_incomplete_use_status'); return;
            } catch (_) { error(response, 500, 'journal_integrity'); return; }
        }
        if (pending.size) { error(response, 409, 'read_in_progress'); return; }
        const entry = {schema: 'pocket.native-midi-bridge-journal/v1', protocol: PROTOCOL, session_nonce: nonce, package_sha256: packageSha256, request_key: record.request_key, request_json: raw, request_sha256: hash(raw), operation: 'read', state: 'dispatched', dispatch_count: 1, received_at: new Date().toISOString(), result_json: null, result_sha256: null};
        try { save(entry, true); } catch (_) { error(response, 500, 'journal_unavailable'); return; }
        const wait = {entry, response, timer: null};
        pending.set(record.request_key, wait);
        wait.timer = setTimeout(() => { error(response, 504, 'read_timeout_use_status'); }, record.timeout_ms);
        // Durable dispatched state deliberately precedes outlet. An exception or
        // disconnect leaves an incomplete journal; this request is never retried.
        try { max.outlet('observe', record.request_key, nonce, record.target.track_index, record.target.clip_index); }
        catch (_) { clearTimeout(wait.timer); error(response, 500, 'dispatch_uncertain_use_status'); }
    });
    request.on('error', () => {});
});
server.headersTimeout = 5000;
server.requestTimeout = 5000;
server.on('error', failure => { max.post('Native MIDI bridge: ' + failure.message); });
max.addHandler('ready', () => { if (!bound) { max.outlet('bind_session', nonce, packageSha256); } });
max.addHandler('bound', value => {
    if (value !== nonce || listening) { return; }
    bound = true; listening = true;
    server.listen(0, '127.0.0.1', () => {
        atomic(descriptorPath, {schema: 'pocket.native-midi-connection/v1', protocol: PROTOCOL, host: '127.0.0.1', port: server.address().port, token, session_nonce: nonce, package_sha256: packageSha256}, true);
        clearInterval(handshake);
        max.post('Pocket native MIDI read adapter ready; native writes unavailable.');
    });
});
max.addHandler('result', async (key, dictionaryName) => {
    if (!hex(key, 64) || !pending.has(key)) { return; }
    const wait = pending.get(key);
    try {
        const wire = await max.getDict(dictionaryName);
        if (!exact(wire, ['schema', 'request_key', 'session_nonce', 'json_chunks']) || wire.schema !== PROTOCOL || wire.request_key !== key || wire.session_nonce !== nonce || !Array.isArray(wire.json_chunks) || wire.json_chunks.length > 1024 || wire.json_chunks.some(chunk => typeof chunk !== 'string' || chunk.length > 4096)) { fail('Invalid native response envelope'); }
        const raw = wire.json_chunks.join('');
        if (Buffer.byteLength(raw) > MAX_RESULT) { fail('Native response exceeds bound'); }
        const result = JSON.parse(raw);
        if (!result || result.schema !== 'pocket.native-midi-observation/v1' || result.request_key !== key || result.session_nonce !== nonce || result.package_sha256 !== packageSha256 || result.protocol !== PROTOCOL || !['ok', 'unsupported'].includes(result.status)) { fail('Native response identity mismatch'); }
        wait.entry.state = 'complete'; wait.entry.result_json = raw; wait.entry.result_sha256 = hash(raw); wait.entry.completed_at = new Date().toISOString();
        save(wait.entry, false);
        answer(wait.response, 200, {schema: 'pocket.native-midi-response/v1', replay: false, journal: wait.entry});
        pending.delete(key);
    } catch (_) {
        error(wait.response, 502, 'invalid_native_result_use_status');
        // Unverified responses never become complete or authorize a retry.
    } finally { clearTimeout(wait.timer); max.outlet('release', key); }
});
const handshake = setInterval(() => { if (!bound) { max.outlet('probe'); } }, 250);
process.on('exit', () => { try { fs.unlinkSync(descriptorPath); } catch (_) {} });
