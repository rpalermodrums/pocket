/* Guarded two-stage native note writer and reader. No arbitrary API calls. */
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const http = require('http');
const max = require('max-api');
const PROTOCOL = 'pocket.native-midi-wire/v1';
const SOURCE_NAMES = ['Native MIDI Writer.maxpat', 'bridge.js', 'device.js', 'reader.js', 'writer.js'];
const MAX_RESULT = 2097152;
const folder = path.resolve(process.env.POCKET_NATIVE_MIDI_WRITER_DIR || path.join(os.homedir(), '.pocket', 'native-midi-writer'));
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
const hostFolder = path.join(os.homedir(), '.pocket', 'native-midi-writer');
privateDirectory(hostFolder);
const hostLease = path.join(hostFolder, 'host-lease.json');
const journalFolder = path.join(folder, 'journals', nonce);
privateDirectory(journalFolder);
const descriptorPath = path.join(folder, 'connection-' + nonce + '.json');
const pending = new Map();
let bound = false;
let listening = false;
let bindingInProgress = false;
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
        answer(response, 200, {schema: 'pocket.native-midi-health/v1', protocol: PROTOCOL, session_nonce: nonce, package_sha256: packageSha256, ready: bound, write_available: true}); return;
    }
    if (request.method === 'POST' && ['/prepare', '/commit', '/cancel'].includes(request.url)) { writeRoute(request, response); return; }
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
server.on('error', failure => { max.post('Native MIDI writer bridge: ' + failure.message); });
max.addHandler('ready', () => { if (!bound) { max.outlet('bind_session', nonce, packageSha256); } });
max.addHandler('bound', async value => {
    if (value !== nonce || listening || bindingInProgress) { return; }
    bindingInProgress = true;
    try {
        const input = await max.getDict('pocket_write_input_' + nonce);
        if (!exact(input,['json_chunks']) || !Array.isArray(input.json_chunks) || input.json_chunks.length !== 0) { fail('Writer input dictionary readiness mismatch'); }
        bound = true; listening = true;
        server.listen(0, '127.0.0.1', () => {
            atomic(descriptorPath, {schema: 'pocket.native-midi-connection/v1', protocol: PROTOCOL, host: '127.0.0.1', port: server.address().port, token, session_nonce: nonce, package_sha256: packageSha256}, true);
            clearInterval(handshake);
            max.post('Pocket guarded MIDI writer ready; explicit requests only.');
        });
    } catch (failure) { max.post('Pocket writer not ready: ' + failure.message); }
    finally { bindingInProgress = false; }
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
        if (wait.writer) { finishWrite(wait, raw, result); return; }
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

function writeRequest(raw) {
    const record = JSON.parse(raw);
    if (!exact(record, ['schema','request_key','session_nonce','package_sha256','target','expected','edit','saved_als_path','saved_als_sha256','workspace_path','workspace_sha256','binding','timeout_ms']) || record.schema !== 'pocket.native-midi-write-request/v1' || !hex(record.request_key,64) || record.session_nonce !== nonce || record.package_sha256 !== packageSha256 || !Number.isInteger(record.timeout_ms) || record.timeout_ms < 100 || record.timeout_ms > 60000) { fail('Invalid write request'); }
    if (!['insert_empty','set_velocity'].includes(record.edit.kind) || !hex(record.saved_als_sha256,64) || !hex(record.workspace_sha256,64) || typeof record.saved_als_path !== 'string' || typeof record.workspace_path !== 'string' || !path.isAbsolute(record.saved_als_path) || !path.isAbsolute(record.workspace_path) || path.basename(record.workspace_path) !== 'workspace.json' || !record.saved_als_path.startsWith(path.dirname(record.workspace_path) + path.sep)) { fail('Invalid isolated workspace binding'); }
    return record;
}
function fileGuard(record) {
    for (const pair of [[record.saved_als_path,record.saved_als_sha256],[record.workspace_path,record.workspace_sha256]]) {
        let current = pair[0];
        while (current !== path.dirname(current)) { if (fs.lstatSync(current).isSymbolicLink()) { fail('Workspace symlink'); } current = path.dirname(current); }
        const info = fs.statSync(pair[0]);
        if (!info.isFile() || info.nlink !== 1 || info.size > 16777216 || hash(fs.readFileSync(pair[0])) !== pair[1]) { fail('Workspace bytes changed before dispatch'); }
    }
    const workspace = JSON.parse(fs.readFileSync(record.workspace_path,'utf8'));
    if (workspace.state !== 'host_pending' || workspace.workspace_id !== record.binding.workspace_id || workspace.revision !== record.binding.pending_revision) { fail('Workspace is not bound pending state'); }
}
function writeRoute(request, response) {
    const chunks = []; let size = 0; let excessive = false;
    request.on('data', chunk => { size += chunk.length; if (size > MAX_RESULT) { excessive = true; error(response,413,'request_too_large'); } else { chunks.push(chunk); } });
    request.on('error', () => {});
    request.on('end', async () => {
        if (excessive || response.destroyed || response.writableEnded) { return; }
        const raw = Buffer.concat(chunks).toString('utf8');
        let record, entry;
        try {
            if (request.headers['content-type'] !== 'application/json') { fail('JSON required'); }
            if (request.url === '/prepare') {
                record = writeRequest(raw);
                if (pending.size || fs.existsSync(journalPath(record.request_key))) { error(response,409,'already_seen_or_busy'); return; }
                if (fs.existsSync(hostLease)) { transportRefusal(record,raw,'host_lease_conflict',response); return; }
                fileGuard(record);
                entry = {schema:'pocket.native-midi-write-journal/v1',protocol:PROTOCOL,session_nonce:nonce,package_sha256:packageSha256,request_key:record.request_key,request_json:raw,request_sha256:hash(raw),operation:record.edit.kind,state:'preparing',host_lease_acquired:false,dispatch_intent_count:0,received_at:new Date().toISOString(),prepare_result_json:null,prepare_result_sha256:null,result_json:null,result_sha256:null};
                save(entry,true);
                try { atomic(hostLease,{schema:'pocket.native-midi-host-lease/v1',session_nonce:nonce,request_key:record.request_key,package_sha256:packageSha256,request_sha256:entry.request_sha256,workspace_id:record.binding.workspace_id,pending_revision:record.binding.pending_revision},true); } catch (leaseError) {
                    if (leaseError.code === 'EEXIST') { transportRefusal(record,raw,'host_lease_conflict',response,entry); return; }
                    throw leaseError;
                }
                entry.host_lease_acquired = true; save(entry,false);
                const wait = {writer:true,entry,response,timer:null,stage:'prepare'};
                pending.set(record.request_key,wait);
                const dictionaryName = 'pocket_write_input_' + nonce;
                const chunks = []; for (let i=0; i<raw.length; i+=4096) { chunks.push(raw.slice(i,i+4096)); }
                await max.setDict(dictionaryName,{json_chunks:chunks});
                wait.timer = setTimeout(() => error(response,504,'prepare_timeout_use_status'),record.timeout_ms);
                max.outlet('prepare_write',record.request_key,nonce,dictionaryName);
                return;
            }
            record = JSON.parse(raw);
            if (!exact(record,['request_key','session_nonce','prepare_sha256']) || !hex(record.request_key,64) || record.session_nonce !== nonce || !hex(record.prepare_sha256,64)) { fail('Invalid prepared request identity'); }
            const file = journalPath(record.request_key);
            if (fs.lstatSync(file).isSymbolicLink() || fs.statSync(file).size > MAX_RESULT * 3) { fail('Unsafe write journal'); }
            entry = JSON.parse(fs.readFileSync(file,'utf8'));
            if (entry.schema !== 'pocket.native-midi-write-journal/v1' || entry.session_nonce !== nonce || entry.state !== 'prepared' || entry.prepare_result_sha256 !== record.prepare_sha256 || hash(entry.prepare_result_json) !== record.prepare_sha256 || pending.size) { error(response,409,'write_not_prepared_or_busy'); return; }
            const original = writeRequest(entry.request_json);
            const lease = JSON.parse(fs.readFileSync(hostLease,'utf8'));
            if (lease.session_nonce !== nonce || lease.request_key !== record.request_key || lease.request_sha256 !== entry.request_sha256) { fail('Native host lease no longer matches prepared request'); }
            if (hash(entry.request_json) !== entry.request_sha256) { fail('Request bytes changed'); }
            if (request.url === '/cancel') {
                const result = JSON.parse(entry.prepare_result_json);
                result.stage = 'cancel'; result.outcome = 'refused_before_dispatch'; result.error = 'Explicitly cancelled before commit';
                result.ended_at = new Date().toISOString();
                entry.state = 'complete'; entry.result_json = JSON.stringify(result); entry.result_sha256 = hash(entry.result_json); entry.completed_at = result.ended_at;
                save(entry,false); max.outlet('cancel_write',record.request_key);
                answer(response,200,{schema:'pocket.native-midi-write-response/v1',journal:entry}); return;
            }
            fileGuard(original);
            entry.state = 'dispatched'; entry.dispatch_intent_count = 1; save(entry,false);
            const wait = {writer:true,entry,response,timer:null,stage:'commit'};
            pending.set(record.request_key,wait);
            wait.timer = setTimeout(() => error(response,504,'commit_timeout_use_status'),original.timeout_ms);
            max.outlet('commit_write',record.request_key,nonce);
        } catch (failure) { max.post('Pocket writer request failed: ' + failure.message); error(response,400,'write_refused_or_uncertain:' + failure.message.slice(0,200)); }
    });
}
function finishWrite(wait, raw, result) {
    const entry = wait.entry;
    if (!result || result.schema !== 'pocket.native-midi-write-result/v1' || result.request_key !== entry.request_key || result.session_nonce !== nonce || result.package_sha256 !== packageSha256 || result.stage !== wait.stage || !['ready','refused_before_dispatch','verified_readback','outcome_unknown'].includes(result.outcome) || ![0,1].includes(result.dispatch_count)) { fail('Write result identity mismatch'); }
    if (wait.stage === 'prepare') {
        entry.prepare_result_json = raw; entry.prepare_result_sha256 = hash(raw);
        if (result.outcome === 'ready' && result.dispatch_count === 0) { entry.state = 'prepared'; }
        else { entry.state = 'complete'; entry.result_json = raw; entry.result_sha256 = hash(raw); entry.completed_at = new Date().toISOString(); }
    } else { entry.state = 'complete'; entry.result_json = raw; entry.result_sha256 = hash(raw); entry.completed_at = new Date().toISOString(); }
    save(entry,false);
    pending.delete(entry.request_key);
    answer(wait.response,200,{schema:'pocket.native-midi-write-response/v1',journal:entry});
}

function transportRefusal(record, raw, reason, response, previous) {
    const now = new Date().toISOString();
    const result = {schema:'pocket.native-midi-write-result/v1',request_key:record.request_key,session_nonce:nonce,package_sha256:packageSha256,protocol:PROTOCOL,stage:'transport_refusal',outcome:'refused_before_dispatch',dispatch_count:0,before:null,after:null,method:null,dictionary:null,call_return:null,started_at:now,ended_at:now,error:reason};
    const serialized = JSON.stringify(result);
    const entry = previous || {schema:'pocket.native-midi-write-journal/v1',protocol:PROTOCOL,session_nonce:nonce,package_sha256:packageSha256,request_key:record.request_key,request_json:raw,request_sha256:hash(raw),operation:record.edit.kind,host_lease_acquired:false,dispatch_intent_count:0,received_at:now};
    Object.assign(entry,{state:'complete',prepare_result_json:serialized,prepare_result_sha256:hash(serialized),result_json:serialized,result_sha256:hash(serialized),completed_at:now});
    save(entry,!previous);
    answer(response,200,{schema:'pocket.native-midi-write-response/v1',journal:entry});
}
