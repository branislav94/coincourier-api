'use strict';

// Execute the actual receiver with local Google-service doubles; no network calls.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'google_apps_script', 'Code.gs'), 'utf8');
const SECRET = 'a'.repeat(64); // Deliberately synthetic; never a deployment secret.
const NOW = Date.parse('2026-10-07T09:00:00Z') / 1000;
const REPORT_DATE = '2026-10-06';
const PDF_BYTES = Buffer.from('%PDF-1.4\nsynthetic offline report\n%%EOF\n');

function unsigned(value) {
  return Buffer.isBuffer(value) ? Buffer.from(value) : Buffer.from(value);
}

function signed(value) {
  return Array.from(value, byte => byte > 127 ? byte - 256 : byte);
}

function blob(value, mimeType = 'application/octet-stream', name = '') {
  const content = typeof value === 'string' ? Buffer.from(value, 'utf8') : unsigned(value);
  return {
    getBytes: () => signed(content),
    getDataAsString: () => content.toString('utf8'),
    getContentType: () => mimeType,
    getName: () => name,
    setName(next) { name = next; return this; },
    setContentType(next) { mimeType = next; return this; },
    copyBlob: () => blob(content, mimeType, name),
  };
}

function iterator(values) {
  let position = 0;
  return { hasNext: () => position < values.length, next: () => values[position++] };
}

function canonical(envelope) {
  const lines = ['AI-COST-REPORTER-V1', envelope.timestamp, envelope.nonce,
    envelope.report_date, envelope.replace ? '1' : '0'];
  for (const file of envelope.files) {
    lines.push(file.name, file.mime_type, String(file.size), file.sha256);
  }
  return lines.join('\n');
}

function sign(envelope, secret = SECRET) {
  envelope.signature = crypto.createHmac('sha256', Buffer.from(secret, 'utf8'))
    .update(canonical(envelope), 'utf8').digest('hex');
  return envelope;
}

function record(overrides = {}) {
  return Object.assign({
    report_date_utc: REPORT_DATE,
    month_start_utc: '2026-10-01',
    generated_at_utc: '2026-10-07T08:00:00+00:00',
    currency: 'USD',
    data_type: 'PROVIDER_REPORTED_API_SPEND',
    openai_scope: { type: 'all_organization_projects', project_ids: [] },
    providers: {
      openai: { daily_usd: '1.20', month_to_date_usd: '7.40', daily_breakdown_usd: {} },
      xai: { daily_usd: '2.30', month_to_date_usd: '8.50', daily_breakdown_usd: {} },
    },
    daily_total_usd: '3.50',
    month_to_date_total_usd: '15.90',
    notes: [],
  }, overrides);
}

function fileRecord(extension, mimeType, bytes) {
  return {
    name: `ai-cost-report-${REPORT_DATE}.${extension}`,
    mime_type: mimeType,
    size: bytes.length,
    sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
    content_base64: bytes.toString('base64'),
  };
}

function envelope(options = {}) {
  const json = Buffer.from(JSON.stringify(options.record || record()), 'utf8');
  return sign({
    version: 1,
    timestamp: String(options.timestamp === undefined ? NOW : options.timestamp),
    nonce: options.nonce || crypto.randomBytes(16).toString('hex'),
    report_date: REPORT_DATE,
    replace: options.replace || false,
    files: [fileRecord('pdf', 'application/pdf', options.pdf || PDF_BYTES),
      fileRecord('json', 'application/json', json)],
  });
}

function runtime(options = {}) {
  const state = {
    now: options.now || NOW,
    driveOperations: 0,
    createdFiles: 0,
    updatedFiles: 0,
    lockHeld: false,
    lockBusy: false,
    failCreate: 0,
    failAfterCreate: 0,
    failDescription: false,
    failUpdate: false,
    properties: new Map([['AI_COST_SHARED_SECRET', SECRET]]),
    files: [],
    folders: [],
  };
  let nextId = 0;
  function driveOperation() {
    state.driveOperations += 1;
    assert.equal(state.lockHeld, true, 'Drive access must hold the script lock');
  }
  function makeFile(content, parent) {
    let currentBlob = content.copyBlob();
    let description = '';
    let trashed = false;
    const file = {
      parent,
      id: `file_${++nextId}`,
      getId() { driveOperation(); return this.id; },
      getName() { driveOperation(); return currentBlob.getName(); },
      getMimeType() { driveOperation(); return currentBlob.getContentType(); },
      getBlob() { driveOperation(); return currentBlob.copyBlob(); },
      getSize() { driveOperation(); return currentBlob.getBytes().length; },
      getDescription() { driveOperation(); return description; },
      setDescription(next) {
        driveOperation();
        if (state.failDescription) throw new Error('Simulated description failure');
        description = next;
        return this;
      },
      setTrashed(next) { driveOperation(); trashed = next; return this; },
      isTrashed() { driveOperation(); return trashed; },
      setContent(next) {
        driveOperation();
        currentBlob = blob(next, currentBlob.getContentType(), currentBlob.getName());
        return this;
      },
      _name: () => currentBlob.getName(),
      _content: () => Buffer.from(currentBlob.getBytes()),
      _update(next) { currentBlob = next.copyBlob(); },
      _trashed: () => trashed,
    };
    state.files.push(file);
    parent.files.push(file);
    return file;
  }
  function makeFolder(name, parent = null) {
    const folder = {
      id: parent ? `folder_${++nextId}` : 'root',
      name,
      parent,
      folders: [],
      files: [],
      getId() { driveOperation(); return this.id; },
      getName() { driveOperation(); return name; },
      getFoldersByName(next) {
        driveOperation();
        return iterator(this.folders.filter(child => child.name === next));
      },
      createFolder(next) { driveOperation(); return makeFolder(next, this); },
      getFilesByName(next) {
        driveOperation();
        return iterator(this.files.filter(file => file._name() === next && !file._trashed()));
      },
      createFile(content) {
        driveOperation();
        state.createdFiles += 1;
        if (state.createdFiles === state.failCreate) throw new Error('Simulated Drive failure');
        const file = makeFile(content, this);
        if (state.createdFiles === state.failAfterCreate) {
          throw new Error('Simulated loss after Drive saved the file');
        }
        return file;
      },
    };
    state.folders.push(folder);
    if (parent) parent.folders.push(folder);
    return folder;
  }
  const root = makeFolder('My Drive');
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [state.now * 1000])); }
    static now() { return state.now * 1000; }
  }
  const properties = {
    getProperty: key => state.properties.has(key) ? state.properties.get(key) : null,
    setProperty(key, value) { state.properties.set(key, String(value)); return this; },
    getProperties: () => Object.fromEntries(state.properties),
    setProperties(values, deleteAll = false) {
      if (deleteAll) state.properties.clear();
      for (const [key, value] of Object.entries(values)) state.properties.set(key, String(value));
      return this;
    },
    deleteProperty(key) { state.properties.delete(key); return this; },
  };
  const context = vm.createContext({
    Date: FixedDate,
    console: { log: () => { throw new Error('Receiver must not log request contents'); } },
    Utilities: {
      DigestAlgorithm: { SHA_256: 'SHA_256' },
      Charset: { UTF_8: 'UTF_8' },
      computeDigest: (_algorithm, value) => signed(crypto.createHash('sha256')
        .update(typeof value === 'string' ? Buffer.from(value, 'utf8') : unsigned(value)).digest()),
      computeHmacSha256Signature: (value, key) => signed(crypto.createHmac('sha256',
        typeof key === 'string' ? Buffer.from(key, 'utf8') : unsigned(key))
        .update(typeof value === 'string' ? Buffer.from(value, 'utf8') : unsigned(value)).digest()),
      base64Decode: value => signed(Buffer.from(value, 'base64')),
      base64Encode: value => unsigned(value).toString('base64'),
      newBlob: blob,
      getUuid: () => crypto.randomUUID(),
    },
    PropertiesService: { getScriptProperties: () => properties },
    LockService: {
      getScriptLock: () => ({
        tryLock() {
          if (state.lockBusy || state.lockHeld) return false;
          state.lockHeld = true;
          return true;
        },
        waitLock() {
          if (state.lockBusy || state.lockHeld) throw new Error('Lock unavailable');
          state.lockHeld = true;
        },
        releaseLock() { state.lockHeld = false; },
      }),
    },
    DriveApp: {
      getRootFolder() { driveOperation(); return root; },
      getFolderById(id) {
        driveOperation();
        const found = state.folders.find(folder => folder.id === id);
        if (!found) throw new Error('Unknown folder');
        return found;
      },
      getFileById(id) {
        driveOperation();
        const found = state.files.find(file => file.id === id);
        if (!found) throw new Error('Unknown file');
        return found;
      },
    },
    Drive: {
      Files: {
        update(_metadata, id, content) {
          driveOperation();
          if (state.failUpdate) throw new Error('Simulated update failure');
          const found = state.files.find(file => file.id === id);
          if (!found) throw new Error('Unknown update file');
          found._update(content);
          state.updatedFiles += 1;
          return { id };
        },
      },
    },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: text => ({ text, setMimeType() { return this; }, getContent: () => text }),
    },
  });
  vm.runInContext(SOURCE, context, { filename: 'google_apps_script/Code.gs' });
  function post(request) {
    const contents = typeof request === 'string' ? request : JSON.stringify(request);
    return JSON.parse(context.doPost({ postData: {
      contents, type: 'application/json', length: Buffer.byteLength(contents, 'utf8'),
    } }).getContent());
  }
  return { state, root, post,
    rawPost: event => JSON.parse(context.doPost(event).getContent()),
    get: () => JSON.parse(context.doGet().getContent()) };
}

function rejectedBeforeDrive(request, options = {}) {
  const receiver = runtime(options);
  const result = receiver.post(request);
  assert.equal(result.ok, false, 'Malformed or unauthenticated request must fail');
  assert.equal(receiver.state.driveOperations, 0, 'Rejected request must not access Drive');
  assert.equal(receiver.state.lockHeld, false, 'Lock must always be released');
}

const cases = [];
function test(name, run) { cases.push([name, run]); }

test('valid upload creates only the required private hierarchy and pair', () => {
  const receiver = runtime();
  const request = envelope();
  const result = receiver.post(request);
  assert.equal(result.ok, true);
  assert.equal(result.version, 1);
  assert.equal(result.report_date, REPORT_DATE);
  assert.equal(result.duplicate, false);
  assert.equal(receiver.state.files.length, 2);
  assert.deepEqual(receiver.state.folders.map(folder => folder.name),
    ['My Drive', 'AI Infrastructure Costs', '2026', '10']);
  for (const [index, kind] of ['pdf', 'json'].entries()) {
    assert.equal(result.files[kind].name, request.files[index].name);
    assert.equal(result.files[kind].sha256, request.files[index].sha256);
    assert.equal(receiver.state.files[index]._content().toString('base64'),
      request.files[index].content_base64);
  }
  assert.equal(receiver.state.lockHeld, false);
});

test('invalid signature, expired timestamp, and future timestamp fail before Drive', () => {
  const bad = envelope();
  bad.signature = '0'.repeat(64);
  rejectedBeforeDrive(bad);
  rejectedBeforeDrive(envelope({ timestamp: NOW - 301 }));
  rejectedBeforeDrive(envelope({ timestamp: NOW + 301 }));
});

test('five-minute boundaries are inclusive', () => {
  for (const timestamp of [NOW - 300, NOW + 300]) {
    assert.equal(runtime().post(envelope({ timestamp })).ok, true);
  }
});

test('replay is denied; a newly signed retry returns the same Drive IDs', () => {
  const receiver = runtime();
  const request = envelope();
  const first = receiver.post(request);
  assert.equal(first.ok, true);
  const beforeReplay = receiver.state.driveOperations;
  assert.equal(receiver.post(request).ok, false);
  assert.equal(receiver.state.driveOperations, beforeReplay);
  const retried = receiver.post(envelope());
  assert.equal(retried.ok, true);
  assert.equal(retried.duplicate, true);
  assert.deepEqual(retried.files, first.files);
  assert.equal(receiver.state.createdFiles, 2);
});

test('nonce protection survives cold-start execution contexts', () => {
  const first = runtime();
  const request = envelope();
  assert.equal(first.post(request).ok, true);
  const restarted = runtime();
  restarted.state.properties = first.state.properties;
  assert.equal(restarted.post(request).ok, false);
  assert.equal(restarted.state.driveOperations, 0);
});

test('concurrent lock contention does not create files or consume a nonce', () => {
  const receiver = runtime();
  receiver.state.lockBusy = true;
  const request = envelope();
  assert.equal(receiver.post(request).ok, false);
  assert.equal(receiver.state.driveOperations, 0);
  receiver.state.lockBusy = false;
  assert.equal(receiver.post(request).ok, true);
});

test('partial second-file failure recovers the existing PDF on a fresh retry', () => {
  const receiver = runtime();
  receiver.state.failCreate = 2;
  const failedRequest = envelope();
  assert.equal(receiver.post(failedRequest).ok, false);
  assert.equal(receiver.state.files.length, 1);
  assert.equal(receiver.state.lockHeld, false);
  const beforeReplay = receiver.state.driveOperations;
  assert.equal(receiver.post(failedRequest).ok, false);
  assert.equal(receiver.state.driveOperations, beforeReplay);
  receiver.state.failCreate = 0;
  assert.equal(receiver.post(envelope()).ok, true);
  assert.equal(receiver.state.files.length, 2);
  assert.equal(receiver.state.files.filter(file => file._name().endsWith('.pdf')).length, 1);
});

test('crash after Drive creation is recovered by content integrity without duplicate files', () => {
  const receiver = runtime();
  receiver.state.failAfterCreate = 1;
  assert.equal(receiver.post(envelope()).ok, false);
  assert.equal(receiver.state.files.length, 1);
  receiver.state.failAfterCreate = 0;
  assert.equal(receiver.post(envelope()).ok, true);
  assert.equal(receiver.state.files.length, 2);
});

test('conflicting same-name content requires explicit replacement and retains IDs', () => {
  const receiver = runtime();
  const first = receiver.post(envelope());
  assert.equal(first.ok, true);
  const changed = envelope({ pdf: Buffer.from('%PDF-1.4\nchanged offline fixture\n%%EOF\n') });
  assert.equal(receiver.post(changed).ok, false);
  const forced = receiver.post(envelope({
    pdf: Buffer.from('%PDF-1.4\nchanged offline fixture\n%%EOF\n'), replace: true,
  }));
  assert.equal(forced.ok, true);
  assert.equal(forced.files.pdf.id, first.files.pdf.id);
  assert.equal(forced.files.json.id, first.files.json.id);
  assert.equal(receiver.state.files.length, 2);
  assert.equal(receiver.state.updatedFiles >= 1, true);
});

test('replacement failure releases the lock and reports failure', () => {
  const receiver = runtime();
  assert.equal(receiver.post(envelope()).ok, true);
  receiver.state.failUpdate = true;
  assert.equal(receiver.post(envelope({
    pdf: Buffer.from('%PDF-1.4\nnew\n%%EOF\n'), replace: true,
  })).ok, false);
  assert.equal(receiver.state.lockHeld, false);
  assert.equal(receiver.state.files.length, 2);
});

test('filename, MIME, date, version, extra fields and signed metadata are strictly checked', () => {
  const mutations = [
    request => { request.files[0].name = '../report.pdf'; },
    request => { request.files[1].name = 'ai-cost-report-2026-10-05.json'; },
    request => { request.files[0].mime_type = 'text/html'; },
    request => { request.report_date = '2026-02-30'; },
    request => { request.report_date = '2026-10-07'; },
    request => { request.report_date = '1999-12-31'; },
    request => { request.version = 2; },
    request => { request.replace = 'false'; },
    request => { request.nonce = 'bad'; },
    request => { request.timestamp = '0' + request.timestamp; },
    request => { request.timestamp = Number(request.timestamp); },
    request => { request.timestamp += '\n'; },
    request => { request.nonce += '\n'; },
    request => { request.files[0].sha256 += '\n'; },
    request => { request.report_date += '\n'; },
    request => { request.unexpected = true; },
    request => { request.files[0].unexpected = true; },
    request => { request.files.reverse(); },
  ];
  for (const mutation of mutations) {
    const request = envelope();
    mutation(request);
    sign(request);
    rejectedBeforeDrive(request);
  }
  const changedAfterSigning = envelope();
  changedAfterSigning.replace = true;
  rejectedBeforeDrive(changedAfterSigning);
});

test('byte count, digest, strict base64, PDF magic and JSON provenance are checked', () => {
  const wrongSize = envelope();
  wrongSize.files[0].size += 1;
  rejectedBeforeDrive(sign(wrongSize));
  const wrongDigest = envelope();
  wrongDigest.files[0].sha256 = '0'.repeat(64);
  rejectedBeforeDrive(sign(wrongDigest));
  const changedBytes = envelope();
  changedBytes.files[0].content_base64 = Buffer.from('%PDF-tampered').toString('base64');
  rejectedBeforeDrive(changedBytes);
  const spacedBase64 = envelope();
  spacedBase64.files[0].content_base64 += '\n';
  rejectedBeforeDrive(spacedBase64);
  rejectedBeforeDrive(envelope({ pdf: Buffer.from('not a PDF') }));
  rejectedBeforeDrive(envelope({ pdf: Buffer.from('%PDF-1.4\nmissing end marker') }));
  const invalidUtf8 = envelope();
  invalidUtf8.files[1] = fileRecord('json', 'application/json', Buffer.from([255, 254]));
  rejectedBeforeDrive(sign(invalidUtf8));
  for (const override of [
    { data_type: 'SYNTHETIC_DEMO' }, { currency: 'EUR' },
    { report_date_utc: '2026-10-05' }, { providers: { openai: {} } },
  ]) {
    rejectedBeforeDrive(envelope({ record: record(override) }));
  }
});

test('PDF, JSON and total request size limits are enforced before Drive', () => {
  const bigPdf = Buffer.alloc(2 * 1024 * 1024 + 1, 32);
  PDF_BYTES.copy(bigPdf);
  rejectedBeforeDrive(envelope({ pdf: bigPdf }));
  const bigRecord = record({ notes: ['x'.repeat(512 * 1024)] });
  rejectedBeforeDrive(envelope({ record: bigRecord }));
  rejectedBeforeDrive(' '.repeat(4 * 1024 * 1024 + 1));
});

test('nonce cache is bounded and expired entries are pruned', () => {
  const full = runtime();
  for (let index = 0; index < 512; index += 1) {
    full.state.properties.set(`AI_COST_NONCE_${index.toString(16).padStart(32, '0')}`,
      String(NOW + 300));
  }
  assert.equal(full.post(envelope()).ok, false);
  assert.equal(full.state.driveOperations, 0);
  const expired = runtime();
  expired.state.properties.set('AI_COST_NONCE_' + '1'.repeat(32), String(NOW - 1));
  assert.equal(expired.post(envelope()).ok, true);
  assert.equal(expired.state.properties.has('AI_COST_NONCE_' + '1'.repeat(32)), false);
});

test('missing/bad script configuration and doGet never expose Drive', () => {
  for (const secret of [null, 'short', 'A'.repeat(64), SECRET + '\n']) {
    const receiver = runtime();
    if (secret === null) receiver.state.properties.delete('AI_COST_SHARED_SECRET');
    else receiver.state.properties.set('AI_COST_SHARED_SECRET', secret);
    assert.equal(receiver.post(envelope()).ok, false);
    assert.equal(receiver.state.driveOperations, 0);
  }
  const receiver = runtime();
  const result = receiver.get();
  assert.equal(result.ok, false);
  assert.equal(receiver.state.driveOperations, 0);
  assert.equal(JSON.stringify(result).includes('file_'), false);
});

test('missing post data and wrong request MIME are rejected before Drive', () => {
  for (const event of [undefined, {}, { postData: {} },
    { postData: { contents: JSON.stringify(envelope()), type: 'text/plain' } }]) {
    const receiver = runtime();
    assert.equal(receiver.rawPost(event).ok, false);
    assert.equal(receiver.state.driveOperations, 0);
  }
});

test('configured parent folder is used and an invalid parent cannot access Drive', () => {
  const receiver = runtime();
  receiver.root.id = 'coincourier_owned_folder';
  receiver.state.properties.set('AI_COST_PARENT_FOLDER_ID', receiver.root.id);
  assert.equal(receiver.post(envelope()).ok, true);
  assert.equal(receiver.root.folders[0].name, 'AI Infrastructure Costs');
  const invalid = runtime();
  invalid.state.properties.set('AI_COST_PARENT_FOLDER_ID', '../other');
  assert.equal(invalid.post(envelope()).ok, false);
  assert.equal(invalid.state.driveOperations, 0);
});

test('preexisting duplicate names fail safely without creating or replacing reports', () => {
  const folders = runtime();
  folders.state.lockHeld = true;
  folders.root.createFolder('AI Infrastructure Costs');
  folders.root.createFolder('AI Infrastructure Costs');
  folders.state.lockHeld = false;
  assert.equal(folders.post(envelope()).ok, false);
  assert.equal(folders.state.files.length, 0);
  const files = runtime();
  assert.equal(files.post(envelope()).ok, true);
  const month = files.state.folders[files.state.folders.length - 1];
  files.state.lockHeld = true;
  month.createFile(blob(PDF_BYTES, 'application/pdf', `ai-cost-report-${REPORT_DATE}.pdf`));
  files.state.lockHeld = false;
  const before = files.state.createdFiles;
  assert.equal(files.post(envelope({ replace: true })).ok, false);
  assert.equal(files.state.createdFiles, before);
  assert.equal(files.state.updatedFiles, 0);
});

test('both existing files are preflighted before any replacement', () => {
  const receiver = runtime();
  assert.equal(receiver.post(envelope()).ok, true);
  receiver.state.files[1]._update(blob(Buffer.from('wrong'), 'text/html',
    `ai-cost-report-${REPORT_DATE}.json`));
  assert.equal(receiver.post(envelope({
    pdf: Buffer.from('%PDF-1.4\nreplacement\n%%EOF\n'), replace: true,
  })).ok, false);
  assert.equal(receiver.state.updatedFiles, 0);
  assert.equal(receiver.state.files[0]._content().equals(PDF_BYTES), true);
});

test('future-skew nonce remains reserved until its full acceptance window ends', () => {
  const receiver = runtime();
  const request = envelope({ timestamp: NOW + 300 });
  assert.equal(receiver.post(request).ok, true);
  receiver.state.now = NOW + 600;
  const beforeReplay = receiver.state.driveOperations;
  assert.equal(receiver.post(request).ok, false);
  assert.equal(receiver.state.driveOperations, beforeReplay);
  assert.equal(receiver.state.properties.has('AI_COST_NONCE_' + request.nonce), true);
});

if (process.argv.includes('--request')) {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const receiver = runtime({ now: input.now || NOW });
  const response = receiver.post(input.envelope);
  process.stdout.write(JSON.stringify({ response,
    drive_operations: receiver.state.driveOperations,
    file_count: receiver.state.files.length }));
} else {
  let passed = 0;
  for (const [name, run] of cases) {
    try {
      run();
      passed += 1;
      process.stdout.write(`PASS ${name}\n`);
    } catch (error) {
      process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
      process.exitCode = 1;
    }
  }
  process.stdout.write(`${passed}/${cases.length} Apps Script receiver tests passed.\n`);
}
