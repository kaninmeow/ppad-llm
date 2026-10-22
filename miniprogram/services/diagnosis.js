const { API_BASE_URL, UPLOAD_TIMEOUT_MS } = require('../config');

const STATUSES = ['suspected_disease', 'healthy', 'uncertain', 'not_tomato'];
const MESSAGES = {
  SECURITY_NOT_CONFIGURED: '服务尚未配置内容安全检测，请联系管理员。',
  SECURITY_UNAVAILABLE: '内容安全检测暂时不可用，请稍后重试。',
  SECURITY_TIMEOUT: '内容安全检测超时，请稍后重试。',
  LOGIN_FAILED: '微信登录失败，请重新进入小程序后重试。',
  CONTENT_REJECTED: '您提交的内容含违规信息。',
  TOO_MANY_REQUESTS: '当前识别请求较多，请稍后重试。',
  QUEUE_FULL: '当前识别请求较多，请稍后重试。',
  JOB_NOT_FOUND: '本次识别已失效，请重新提交。',
  NOT_FOUND: '本次识别已失效，请重新提交。',
  MODEL_NOT_CONFIGURED: '识别服务尚未配置，请联系服务管理员。',
  INVALID_IMAGE: '无法读取这张图片，请重新选择清晰的照片。',
  IMAGE_TOO_LARGE: '图片超过 8 MB，请压缩或重新拍摄后重试。',
  UNSUPPORTED_IMAGE: '请使用 JPEG、PNG 或 WebP 格式的照片。',
  INVALID_REQUEST: '提交内容有误，请检查图片和描述后重试。',
  MODEL_AUTH_FAILED: '识别服务暂时无法使用，请联系服务管理员。',
  MODEL_RATE_LIMITED: '当前识别请求较多，请稍后重试。',
  MODEL_TIMEOUT: '识别等待超时，请稍后重试。',
  MODEL_UNAVAILABLE: '识别服务暂时不可用，请稍后重试。',
  INVALID_MODEL_RESPONSE: '本次识别未得到完整结果，请重试。',
  MODEL_REQUEST_FAILED: '本次识别失败，请稍后重试。'
};

function failure(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function isText(value) { return typeof value === 'string' && value.trim().length > 0; }
function textList(value) { return Array.isArray(value) && value.every(isText); }

function validCandidates(result) {
  const candidates = result.disease_candidates;
  if (!Array.isArray(candidates) || candidates.length > 3) return false;
  if (result.status !== 'suspected_disease') return candidates.length === 0;
  if (!candidates.length || !candidates[0] || candidates[0].name !== result.disease_name) return false;
  const names = new Set();
  return candidates.every((candidate, index) => {
    if (!candidate || !isText(candidate.name) || !isText(candidate.reason) ||
        !Number.isInteger(candidate.likelihood) || candidate.likelihood < 0 || candidate.likelihood > 100) return false;
    const name = candidate.name.trim();
    if (names.has(name) || (index > 0 && candidate.likelihood > candidates[index - 1].likelihood)) return false;
    names.add(name);
    return true;
  });
}

function validResponse(body) {
  if (!body || typeof body !== 'object' || !isText(body.request_id) ||
      !isText(body.model) || !Number.isFinite(body.elapsed_ms) || body.elapsed_ms < 0 ||
      !isText(body.notice)) return false;
  const result = body.result;
  if (!result || !STATUSES.includes(result.status) || !isText(result.summary)) return false;
  if (result.status === 'suspected_disease' ? !isText(result.disease_name) : result.disease_name !== null) return false;
  if (!validCandidates(result)) return false;
  return textList(result.observed_symptoms) && textList(result.recommendations) &&
    textList(result.follow_up_questions) && Array.isArray(result.possible_causes) &&
    result.possible_causes.every((cause) => cause && isText(cause.name) && isText(cause.reason));
}

const TOTAL_TIMEOUT_MS = 32 * 60 * 1000;
const INVALID_RESPONSE_MESSAGE = '识别结果不完整，请重试或换一张照片。';

function parseBody(response) {
  let body;
  try { body = typeof response.data === 'string' ? JSON.parse(response.data) : response.data; }
  catch (_) { throw failure('INVALID_RESPONSE', '服务返回的内容无法读取，请稍后重试。'); }
  if (response.statusCode !== 200 && response.statusCode !== 202) {
    const detail = body && body.error;
    const code = detail && typeof detail.code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(detail.code)
      ? detail.code : 'REQUEST_FAILED';
    // Upstream error text may itself contain unmoderated user/model content.
    throw failure(code, MESSAGES[code] || '本次识别失败，请稍后重试。');
  }
  return body;
}

function validPending(body, admission) {
  if (!body || typeof body !== 'object' || Array.isArray(body) ||
      !Number.isFinite(body.poll_after_ms) ||
      !(admission ? body.status === 'checking' : ['checking', 'analysing'].includes(body.status))) return false;
  const fields = admission ? ['job_id', 'job_token', 'status', 'poll_after_ms'] : ['status', 'poll_after_ms'];
  if (Object.keys(body).some((key) => !fields.includes(key))) return false;
  const opaque = (value) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,512}$/.test(value);
  return !admission || (opaque(body.job_id) && opaque(body.job_token));
}

function uploadDiagnosis({ filePath, description = '', onProgress, onStage } = {}) {
  let settled = false, current = null, delayTimer = null, deadlineTimer = null;
  let finish, jobId, jobToken;
  const deadline = Date.now() + TOTAL_TIMEOUT_MS;
  const notify = (callback, value) => {
    if (!settled && typeof callback === 'function') {
      try { callback(value); } catch (_) { /* UI callbacks must not interrupt transport. */ }
    }
  };
  const abortNative = (slot) => {
    if (slot && slot.task && typeof slot.task.abort === 'function') {
      try { slot.task.abort(); } catch (_) { /* Promise has already settled. */ }
    }
  };
  const promise = new Promise((resolve, reject) => {
    finish = (error, value, abort = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(delayTimer); clearTimeout(deadlineTimer);
      const slot = current; current = null;
      jobId = null; jobToken = null;
      if (abort) abortNative(slot);
      if (error) reject(error); else resolve(value);
    };
    const timeout = () => finish(failure('SECURITY_TIMEOUT', MESSAGES.SECURITY_TIMEOUT), null, true);
    const active = () => {
      if (settled) return false;
      if (Date.now() >= deadline) { timeout(); return false; }
      return true;
    };
    // Each slot owns one native operation, including synchronous mock callbacks.
    // A completed slot cannot accept duplicate/late callbacks or replace a later task.
    const start = (invoke, options, success, login = false, progress = false) => {
      if (!active()) return;
      const slot = { task: null }; current = slot;
      options.timeout = Math.min(UPLOAD_TIMEOUT_MS, deadline - Date.now());
      options.success = (response) => {
        if (!active() || current !== slot) return;
        current = null;
        try { success(response); } catch (error) { finish(error); }
      };
      options.fail = (error) => {
        if (!active() || current !== slot) return;
        current = null;
        const detail = String(error && error.errMsg || '');
        if (login) finish(failure('LOGIN_FAILED', MESSAGES.LOGIN_FAILED));
        else if (/abort|cancel/i.test(detail)) finish(failure('REQUEST_CANCELLED', '已取消本次识别。'));
        else if (/timeout|timed out/i.test(detail)) finish(failure('NETWORK_TIMEOUT', '连接等待超时，请检查网络后重试。'));
        else finish(failure('NETWORK_ERROR', '暂时无法连接识别服务，请检查网络后重试。'));
      };
      try {
        slot.task = invoke(options);
        if (progress && current === slot && slot.task && typeof slot.task.onProgressUpdate === 'function') {
          slot.task.onProgressUpdate((event) => {
            if (active() && current === slot && event && Number.isFinite(event.progress)) {
              notify(onProgress, Math.max(0, Math.min(100, Math.round(event.progress))));
            }
          });
        }
      } catch (_) { options.fail({ errMsg: 'platform failure' }); }
    };
    const waitForPoll = (body) => {
      notify(onStage, body.status);
      if (!active()) return;
      delayTimer = setTimeout(() => {
        delayTimer = null;
        if (!active()) return;
        start((options) => wx.request(options), {
          url: API_BASE_URL.replace(/\/+$/, '') + '/api/diagnose/' + encodeURIComponent(jobId),
          method: 'GET', header: { Authorization: 'Bearer ' + jobToken }
        }, (response) => {
          const body = parseBody(response);
          if (response.statusCode === 200) {
            if (!validResponse(body)) throw failure('INVALID_RESPONSE', INVALID_RESPONSE_MESSAGE);
            finish(null, body);
          } else {
            if (!validPending(body, false)) throw failure('INVALID_RESPONSE', INVALID_RESPONSE_MESSAGE);
            waitForPoll(body);
          }
        });
      }, Math.max(1000, Math.min(5000, body.poll_after_ms)));
    };
    if (!isText(filePath)) { finish(failure('INVALID_REQUEST', '请先选择一张番茄照片。')); return; }
    deadlineTimer = setTimeout(timeout, TOTAL_TIMEOUT_MS);
    start((options) => wx.login(options), {}, (login) => {
      if (!login || !isText(login.code) || login.code.length > 512) throw failure('LOGIN_FAILED', MESSAGES.LOGIN_FAILED);
      start((options) => wx.uploadFile(options), {
        url: API_BASE_URL.replace(/\/+$/, '') + '/api/diagnose', filePath, name: 'image',
        formData: { description, login_code: login.code }
      }, (response) => {
        const body = parseBody(response);
        if (response.statusCode !== 202 || !validPending(body, true)) throw failure('INVALID_RESPONSE', INVALID_RESPONSE_MESSAGE);
        jobId = body.job_id; jobToken = body.job_token;
        waitForPoll(body);
      }, false, true);
    }, true);
  });
  return { promise, abort() { finish(failure('REQUEST_CANCELLED', '已取消本次识别。'), null, true); } };
}

module.exports = { uploadDiagnosis };
