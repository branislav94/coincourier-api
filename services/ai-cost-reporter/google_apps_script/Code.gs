/** Private, authenticated PDF/JSON receiver. See README.md for deployment. */
const AI_COST_MAX_PDF_BYTES = 2 * 1024 * 1024;
const AI_COST_MAX_JSON_BYTES = 512 * 1024;
const AI_COST_MAX_REQUEST_BYTES = 4 * 1024 * 1024;
const AI_COST_WINDOW_SECONDS = 300;
const AI_COST_NONCE_PREFIX = 'AI_COST_NONCE_';
const AI_COST_MAX_NONCES = 512;

/** GET never reads configuration or Drive and never returns stored data. */
function doGet() {
  return aiCostResponse_({ok: false, error: 'method_not_allowed'});
}

/** Authenticate and validate everything before taking the lock or accessing Drive. */
function doPost(e) {
  let lock = null;
  let locked = false;
  try {
    const request = aiCostParse_(e);
    const properties = PropertiesService.getScriptProperties();
    const secret = properties.getProperty('AI_COST_SHARED_SECRET');
    if (!aiCostMatches_(secret, /^[0-9a-f]{64}$/)) {
      aiCostFail_('configuration_error');
    }
    const signature = aiCostHex_(Utilities.computeHmacSha256Signature(
      aiCostCanonical_(request), secret, Utilities.Charset.UTF_8
    ));
    if (!aiCostEqual_(signature, request.signature)) {
      aiCostFail_('invalid_signature');
    }
    aiCostCheckTime_(request.timestamp);
    const decoded = request.files.map(aiCostDecode_);
    aiCostValidateContents_(request.report_date, decoded);

    lock = LockService.getScriptLock();
    locked = lock.tryLock(10000);
    if (!locked) aiCostFail_('busy');
    // Recheck after waiting; the same lock protects nonces, folders and files.
    aiCostCheckTime_(request.timestamp);
    aiCostRememberNonce_(properties, request);
    const parentId = properties.getProperty('AI_COST_PARENT_FOLDER_ID');
    if (parentId && !aiCostMatches_(parentId, /^[A-Za-z0-9_-]{1,256}$/)) {
      aiCostFail_('configuration_error');
    }
    const parent = parentId ? DriveApp.getFolderById(parentId) : DriveApp.getRootFolder();
    const root = aiCostFolder_(parent, 'AI Infrastructure Costs');
    const year = aiCostFolder_(root, request.report_date.slice(0, 4));
    const month = aiCostFolder_(year, request.report_date.slice(5, 7));
    // Preflight BOTH names before writing either artifact. Never pick an arbitrary
    // duplicate or silently accept another report's content under the same name.
    const plans = decoded.map(function (file) {
      const existing = aiCostFile_(month, file.name);
      if (!existing) return {file: file, existing: null, unchanged: false};
      if (existing.getMimeType() !== file.mime_type) aiCostFail_('conflict');
      const limit = file.mime_type === 'application/pdf'
        ? AI_COST_MAX_PDF_BYTES : AI_COST_MAX_JSON_BYTES;
      if (existing.getSize() > limit) aiCostFail_('conflict');
      const bytes = existing.getBlob().getBytes();
      if (bytes.length > limit) aiCostFail_('conflict');
      const unchanged = bytes.length === file.size && aiCostDigest_(bytes) === file.sha256;
      if (!unchanged && !request.replace) aiCostFail_('conflict');
      return {file: file, existing: existing, unchanged: unchanged};
    });
    const files = {};
    plans.forEach(function (plan, index) {
      const file = plan.file;
      let id;
      if (plan.unchanged) {
        id = plan.existing.getId();
      } else {
        const blob = Utilities.newBlob(file.bytes, file.mime_type, file.name);
        if (plan.existing) {
          // Advanced Drive v3 service preserves binary file IDs on force resend.
          const updated = Drive.Files.update(
            {mimeType: file.mime_type}, plan.existing.getId(), blob, {fields: 'id'}
          );
          id = updated && updated.id;
        } else {
          // Name and content are created together; a crash before the response
          // needs no property/description ledger to rediscover this exact file.
          id = month.createFile(blob).getId();
        }
      }
      if (!aiCostMatches_(id, /^[A-Za-z0-9_-]{1,256}$/)) {
        aiCostFail_('storage_failure');
      }
      files[index === 0 ? 'pdf' : 'json'] = {id: id, name: file.name, sha256: file.sha256};
    });
    return aiCostResponse_({
      ok: true, version: 1, report_date: request.report_date,
      duplicate: plans.every(function (plan) { return plan.unchanged; }), files: files
    });
  } catch (error) {
    // Never return/log exception text, request bodies, signatures, secrets or Drive details.
    return aiCostResponse_({ok: false, error: error.aiCostCode || 'storage_failure'});
  } finally {
    if (locked) lock.releaseLock();
  }
}

/** Run manually in the editor once to grant the owner's Drive permission. */
function authorizeSetup() {
  const properties = PropertiesService.getScriptProperties();
  const secret = properties.getProperty('AI_COST_SHARED_SECRET');
  if (!aiCostMatches_(secret, /^[0-9a-f]{64}$/)) aiCostFail_('configuration_error');
  const parentId = properties.getProperty('AI_COST_PARENT_FOLDER_ID');
  if (parentId && !aiCostMatches_(parentId, /^[A-Za-z0-9_-]{1,256}$/)) aiCostFail_('configuration_error');
  const folder = parentId ? DriveApp.getFolderById(parentId) : DriveApp.getRootFolder();
  Drive.Files.get(folder.getId(), {fields: 'id'});
}

/** Bound and validate the envelope without trusting custom HTTP headers. */
function aiCostParse_(e) {
  if (!e || !e.postData || typeof e.postData.contents !== 'string' ||
      typeof e.postData.type !== 'string' ||
      e.postData.type.split(';')[0].trim().toLowerCase() !== 'application/json') {
    aiCostFail_('invalid_request');
  }
  const text = e.postData.contents;
  if (!text || text.length > AI_COST_MAX_REQUEST_BYTES ||
      Utilities.newBlob(text).getBytes().length > AI_COST_MAX_REQUEST_BYTES) {
    aiCostFail_('invalid_request');
  }
  let request;
  try { request = JSON.parse(text); } catch (error) { aiCostFail_('invalid_request'); }
  aiCostKeys_(request, ['version', 'timestamp', 'nonce', 'report_date', 'replace', 'files', 'signature']);
  if (request.version !== 1 ||
      !aiCostMatches_(request.timestamp, /^(0|[1-9][0-9]{0,10})$/) ||
      !aiCostMatches_(request.nonce, /^[0-9a-f]{32}$/) ||
      !aiCostMatches_(request.signature, /^[0-9a-f]{64}$/) ||
      typeof request.replace !== 'boolean' || !Array.isArray(request.files) ||
      request.files.length !== 2) {
    aiCostFail_('invalid_request');
  }
  const day = request.report_date;
  if (!aiCostMatches_(day, /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/)) {
    aiCostFail_('invalid_request');
  }
  const parsed = new Date(day + 'T00:00:00.000Z');
  const today = new Date(Date.now()).toISOString().slice(0, 10);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== day ||
      day < '2000-01-01' || day >= today) {
    aiCostFail_('invalid_request');
  }
  request.files.forEach(function (file, index) {
    aiCostKeys_(file, ['name', 'mime_type', 'size', 'sha256', 'content_base64']);
    const extension = index === 0 ? 'pdf' : 'json';
    const limit = index === 0 ? AI_COST_MAX_PDF_BYTES : AI_COST_MAX_JSON_BYTES;
    if (file.name !== 'ai-cost-report-' + day + '.' + extension ||
        file.mime_type !== 'application/' + extension ||
        !Number.isSafeInteger(file.size) || file.size < 1 || file.size > limit ||
        !aiCostMatches_(file.sha256, /^[0-9a-f]{64}$/) ||
        typeof file.content_base64 !== 'string' ||
        file.content_base64.length !== 4 * Math.ceil(file.size / 3) ||
        !aiCostMatches_(file.content_base64, /^[A-Za-z0-9+/]*={0,2}$/)) {
      aiCostFail_('invalid_request');
    }
  });
  return request;
}

/** Exact protocol v1: LF separators, UTF-8, PDF then JSON, no final newline. */
function aiCostCanonical_(request) {
  const lines = ['AI-COST-REPORTER-V1', request.timestamp, request.nonce,
    request.report_date, request.replace ? '1' : '0'];
  request.files.forEach(function (file) {
    lines.push(file.name, file.mime_type, String(file.size), file.sha256);
  });
  return lines.join('\n');
}

/** Verify strict standard Base64, raw-byte length and signed SHA-256. */
function aiCostDecode_(file) {
  let bytes;
  try { bytes = Utilities.base64Decode(file.content_base64); }
  catch (error) { aiCostFail_('invalid_request'); }
  if (bytes.length !== file.size || Utilities.base64Encode(bytes) !== file.content_base64 ||
      aiCostDigest_(bytes) !== file.sha256) {
    aiCostFail_('invalid_request');
  }
  return {name: file.name, mime_type: file.mime_type, size: file.size,
    sha256: file.sha256, bytes: bytes};
}

/** Check PDF markers and the reporter's live JSON date/provider metadata. */
function aiCostValidateContents_(day, files) {
  const pdf = files[0].bytes;
  const head = aiCostAscii_(pdf.slice(0, 5));
  const tail = aiCostAscii_(pdf.slice(-1024));
  if (head !== '%PDF-' || tail.indexOf('%%EOF') === -1) aiCostFail_('invalid_request');
  const text = Utilities.newBlob(files[1].bytes).getDataAsString('UTF-8');
  // Invalid UTF-8 must not be replaced silently by U+FFFD during decoding.
  if (aiCostDigest_(Utilities.newBlob(text).getBytes()) !== files[1].sha256) {
    aiCostFail_('invalid_request');
  }
  let report;
  try { report = JSON.parse(text); } catch (error) { aiCostFail_('invalid_request'); }
  if (!aiCostObject_(report) || report.report_date_utc !== day ||
      report.data_type !== 'PROVIDER_REPORTED_API_SPEND' || report.currency !== 'USD' ||
      !aiCostObject_(report.providers) || !aiCostObject_(report.providers.openai) ||
      !aiCostObject_(report.providers.xai)) {
    aiCostFail_('invalid_request');
  }
}

/** Keep a durable nonce until its signed timestamp leaves the acceptance window. */
function aiCostRememberNonce_(properties, request) {
  const now = Math.floor(Date.now() / 1000);
  const entries = properties.getProperties();
  let count = 0;
  Object.keys(entries).forEach(function (key) {
    if (key.indexOf(AI_COST_NONCE_PREFIX) !== 0) return;
    if (!aiCostMatches_(entries[key], /^[0-9]+$/)) aiCostFail_('configuration_error');
    if (Number(entries[key]) < now) properties.deleteProperty(key);
    else count++;
  });
  const key = AI_COST_NONCE_PREFIX + request.nonce;
  if (properties.getProperty(key) !== null) aiCostFail_('replayed_request');
  if (count >= AI_COST_MAX_NONCES) aiCostFail_('replay_capacity');
  // Written BEFORE Drive, including on later partial storage failures. A caller
  // retries the unchanged artifacts with a fresh timestamp/nonce/signature.
  properties.setProperty(key, String(Number(request.timestamp) + AI_COST_WINDOW_SECONDS));
}

/** Allow at most five minutes of past or future clock skew. */
function aiCostCheckTime_(timestamp) {
  if (Math.abs(Math.floor(Date.now() / 1000) - Number(timestamp)) > AI_COST_WINDOW_SECONDS) {
    aiCostFail_('expired_timestamp');
  }
}

/** Find/create exactly one private child folder while holding the script lock. */
function aiCostFolder_(parent, name) {
  const folders = parent.getFoldersByName(name);
  if (!folders.hasNext()) return parent.createFolder(name);
  const folder = folders.next();
  if (folders.hasNext()) aiCostFail_('duplicates');
  return folder;
}

/** Find exactly one same-name file, failing safely on preexisting duplicates. */
function aiCostFile_(folder, name) {
  const files = folder.getFilesByName(name);
  if (!files.hasNext()) return null;
  const file = files.next();
  if (files.hasNext()) aiCostFail_('duplicates');
  return file;
}

function aiCostDigest_(bytes) {
  return aiCostHex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes));
}

function aiCostHex_(bytes) {
  return bytes.map(function (value) { return ('0' + (value & 255).toString(16)).slice(-2); }).join('');
}

function aiCostAscii_(bytes) {
  return bytes.map(function (value) { return String.fromCharCode(value & 255); }).join('');
}

/** Constant-work comparison for validated, equal-length hex signatures. */
function aiCostEqual_(left, right) {
  if (left.length !== right.length) return false;
  let different = 0;
  for (let i = 0; i < left.length; i++) different |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return different === 0;
}

function aiCostObject_(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** JavaScript's $ also permits a final newline; require a full string match. */
function aiCostMatches_(value, pattern) {
  if (typeof value !== 'string') return false;
  const match = pattern.exec(value);
  return match !== null && match[0] === value;
}

function aiCostKeys_(value, expected) {
  if (!aiCostObject_(value)) aiCostFail_('invalid_request');
  const keys = Object.keys(value).sort();
  if (keys.join(',') !== expected.slice().sort().join(',')) aiCostFail_('invalid_request');
}

function aiCostFail_(code) {
  const error = new Error('AI Cost Reporter request failed');
  error.aiCostCode = code;
  throw error;
}

function aiCostResponse_(body) {
  // ContentService can return HTTP 200 for failures: callers MUST check ok/body.
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}
