const { API_BASE_URL, UPLOAD_TIMEOUT_MS } = require('../config');

const STATUSES = ['suspected_disease', 'healthy', 'uncertain', 'not_tomato'];
const MESSAGES = {
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

function serverMessage(value, fallback) {
  if (!isText(value)) return fallback;
  const clean = value.replace(/<[^>]*>/g, '').replace(/[\u0000-\u001f\u007f]/g, ' ').trim();
  return clean ? clean.slice(0, 200) : fallback;
}

function parseResponse(response) {
  let body;
  try { body = JSON.parse(response.data); }
  catch (_) { throw failure('INVALID_RESPONSE', '服务返回的内容无法读取，请稍后重试。'); }
  if (response.statusCode !== 200) {
    const detail = body && body.error;
    const code = detail && typeof detail.code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(detail.code)
      ? detail.code : 'REQUEST_FAILED';
    const fallback = MESSAGES[code] || '本次识别失败，请稍后重试。';
    throw failure(code, serverMessage(detail && detail.message, fallback));
  }
  if (!validResponse(body)) throw failure('INVALID_RESPONSE', '识别结果不完整，请重试或换一张照片。');
  return body;
}

function uploadDiagnosis({ filePath, description = '', onProgress } = {}) {
  let task;
  let settled = false;
  let rejectPromise;
  const promise = new Promise((resolve, reject) => {
    rejectPromise = reject;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve(value);
    };
    if (!isText(filePath)) {
      finish(failure('INVALID_REQUEST', '请先选择一张番茄照片。'));
      return;
    }
    try {
      task = wx.uploadFile({
        url: API_BASE_URL.replace(/\/+$/, '') + '/api/diagnose',
        filePath,
        name: 'image',
        formData: { description },
        timeout: UPLOAD_TIMEOUT_MS,
        success(response) {
          if (settled) return;
          try { finish(null, parseResponse(response)); } catch (error) { finish(error); }
        },
        fail(error) {
          const detail = String(error && error.errMsg || '');
          if (/abort|cancel/i.test(detail)) finish(failure('REQUEST_CANCELLED', '已取消本次识别。'));
          else if (/timeout|timed out/i.test(detail)) finish(failure('NETWORK_TIMEOUT', '连接等待超时，请检查网络后重试。'));
          else finish(failure('NETWORK_ERROR', '暂时无法连接识别服务，请检查网络后重试。'));
        }
      });
      if (task && typeof task.onProgressUpdate === 'function' && typeof onProgress === 'function') {
        task.onProgressUpdate((event) => {
          if (settled || !event || !Number.isFinite(event.progress)) return;
          try { onProgress(Math.max(0, Math.min(100, Math.round(event.progress)))); } catch (_) { /* UI callbacks must not interrupt transport. */ }
        });
      }
    } catch (_) {
      finish(failure('NETWORK_ERROR', '暂时无法连接识别服务，请检查网络后重试。'));
    }
  });
  return {
    promise,
    abort() {
      if (settled) return;
      settled = true;
      rejectPromise(failure('REQUEST_CANCELLED', '已取消本次识别。'));
      if (task && typeof task.abort === 'function') {
        try { task.abort(); } catch (_) { /* Promise is already cancelled. */ }
      }
    }
  };
}

module.exports = { uploadDiagnosis };
