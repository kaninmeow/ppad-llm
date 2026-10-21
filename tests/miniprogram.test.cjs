const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const config = require('../miniprogram/config');
function loadService(wx) {
  const context = { module: { exports: {} }, require: () => config, wx };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'miniprogram/services/diagnosis.js'), 'utf8'), context);
  return context.module.exports;
}
function response(status = 'suspected_disease') {
  return {
    request_id: 'request-123', model: 'qwen3.8-flash', elapsed_ms: 1200,
    result: {
      status, disease_name: status === 'suspected_disease' ? '疑似早疫病' : null,
      disease_candidates: status === 'suspected_disease' ? [
        { name: '疑似早疫病', likelihood: 70, reason: '可见环纹样褐斑' },
        { name: '斑枯病', likelihood: 25, reason: '局部斑点可能相似' }
      ] : [],
      summary: '叶片上可见褐色斑点，需要结合现场确认。', observed_symptoms: ['叶片出现褐斑'],
      possible_causes: [{ name: '真菌侵染', reason: '斑点呈环纹状' }],
      recommendations: ['检查其他叶片'], follow_up_questions: []
    },
    notice: '本结果仅基于图片进行初步识别，不能替代现场诊断。'
  };
}
function transport() {
  const state = { calls: 0, aborted: 0 };
  const service = loadService({ uploadFile(options) {
    state.calls += 1;
    state.options = options;
    return {
      onProgressUpdate(callback) { state.progress = callback; },
      abort() { state.aborted += 1; options.fail({ errMsg: 'uploadFile:fail abort' }); }
    };
  } });
  return { state, service };
}
function pageHarness(uploadDiagnosis) {
  let definition;
  const wx = {
    chooseMedia(options) { wx.selection = options; },
    previewImage(options) { wx.preview = options; }
  };
  const context = {
    Page(value) { definition = value; }, wx,
    require(name) { return name.endsWith('config') ? config : { uploadDiagnosis }; }
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, 'miniprogram/pages/index/index.js'), 'utf8'), context);
  const page = Object.assign({}, definition, {
    data: JSON.parse(JSON.stringify(definition.data)), updates: [],
    setData(update) { this.updates.push(update); Object.assign(this.data, update); }
  });
  page.onLoad();
  return { page, wx };
}
function pendingUpload() {
  let resolve, reject;
  const state = { calls: 0, aborted: 0 };
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  const upload = (options) => {
    state.calls += 1; state.options = options;
    return { promise, abort() { state.aborted += 1; } };
  };
  return { upload, state, resolve, reject };
}
function choose(wx, file = { tempFilePath: '/new.jpg', size: 1024 }) {
  wx.selection.success({ tempFiles: [file] });
  wx.selection.complete();
}

test('upload serializes the actual image, description, URL and timeout', async () => {
  const { state, service } = transport();
  const request = service.uploadDiagnosis({ filePath: '/tomato.jpg', description: '第三天有斑点' });
  assert.equal(state.options.url, 'http://127.0.0.1:8000/api/diagnose');
  assert.equal(state.options.name, 'image');
  assert.equal(state.options.filePath, '/tomato.jpg');
  assert.equal(state.options.formData.description, '第三天有斑点');
  assert.equal(state.options.timeout, 90000);
  state.options.success({ statusCode: 200, data: JSON.stringify(response()) });
  const result = await request.promise;
  assert.equal(result.result.disease_name, '疑似早疫病');
  assert.equal(result.request_id, 'request-123');
});

for (const status of ['healthy', 'uncertain', 'not_tomato']) {
  test(`accepts the ${status} result with null disease and empty sections`, async () => {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/tomato.jpg' });
    const body = response(status);
    body.result.observed_symptoms = [];
    body.result.possible_causes = [];
    body.result.recommendations = [];
    state.options.success({ statusCode: 200, data: JSON.stringify(body) });
    assert.equal((await request.promise).result.status, status);
  });
}

test('server error preserves stable code and sanitized readable Chinese message', async () => {
  const { state, service } = transport();
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
  state.options.success({ statusCode: 429, data: JSON.stringify({ error: { code: 'MODEL_RATE_LIMITED', message: '<b>请求较多</b>\n请稍后重试' } }) });
  await assert.rejects(request.promise, (error) => error.code === 'MODEL_RATE_LIMITED' && error.message === '请求较多 请稍后重试');
});

test('unknown or malformed HTTP errors use a stable readable fallback', async () => {
  for (const body of [{ error: { code: 'NEW_ERROR', message: '' } }, {}, null]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.success({ statusCode: 503, data: JSON.stringify(body) });
    await assert.rejects(request.promise, (error) => ['NEW_ERROR', 'REQUEST_FAILED'].includes(error.code) && error.message.includes('请稍后重试'));
  }
});

test('non-JSON proxy HTML is rejected safely on success and failure responses', async () => {
  for (const statusCode of [200, 502]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.success({ statusCode, data: '<html>gateway error</html>' });
    await assert.rejects(request.promise, (error) => error.code === 'INVALID_RESPONSE' && !error.message.includes('html'));
  }
});

test('invalid result status, missing fields and inconsistent disease are rejected', async () => {
  const mutations = [
    (body) => { delete body.result; },
    (body) => { body.result.status = 'certain'; },
    (body) => { body.result.recommendations = 'not an array'; },
    (body) => { body.result.possible_causes = [{ name: '原因' }]; },
    (body) => { body.result.summary = ''; },
    (body) => { body.result.disease_name = null; },
    (body) => { body.result.status = 'healthy'; },
    (body) => { delete body.notice; },
    (body) => { body.elapsed_ms = -1; }
  ];
  for (const mutate of mutations) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    const body = response(); mutate(body);
    state.options.success({ statusCode: 200, data: JSON.stringify(body) });
    await assert.rejects(request.promise, (error) => error.code === 'INVALID_RESPONSE');
  }
});

test('rejects invalid likelihood ranges, types and candidate structure before rendering', async () => {
  const mutations = [
    (body) => { delete body.result.disease_candidates; },
    (body) => { body.result.disease_candidates = []; },
    (body) => { body.result.disease_candidates = null; },
    (body) => { body.result.disease_candidates[0].name = '晚疫病'; },
    (body) => { body.result.disease_candidates[1].name = '疑似早疫病'; },
    (body) => { body.result.disease_candidates[1].likelihood = 90; },
    (body) => { delete body.result.disease_candidates[0].reason; },
    (body) => { body.result.disease_candidates[0].reason = ' '; },
    (body) => { body.result.disease_candidates = Array(4).fill(body.result.disease_candidates[0]); },
    ...[-1, 101, 70.5, '70', true, null].map((value) => (body) => { body.result.disease_candidates[0].likelihood = value; }),
    ...['healthy', 'uncertain', 'not_tomato'].map((status) => (body) => { body.result.status = status; body.result.disease_name = null; })
  ];
  for (const mutate of mutations) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    const body = response(); mutate(body);
    state.options.success({ statusCode: 200, data: JSON.stringify(body) });
    await assert.rejects(request.promise, (error) => error.code === 'INVALID_RESPONSE');
  }
});

test('accepts one to three ranked candidates without changing their estimated percentages', async () => {
  for (const likelihoods of [[70], [80, 50], [70, 20, 10], [100, 0]]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    const body = response();
    body.result.disease_candidates = likelihoods.map((likelihood, index) => ({
      name: ['疑似早疫病', '斑枯病', '叶霉病'][index], likelihood, reason: '需要进一步鉴别'
    }));
    state.options.success({ statusCode: 200, data: JSON.stringify(body) });
    assert.equal(JSON.stringify((await request.promise).result.disease_candidates), JSON.stringify(body.result.disease_candidates));
  }
});

test('page separates the current suspected disease from alternatives and supports a single candidate', async () => {
  for (const count of [1, 2]) {
    const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
    const body = response(); body.result.disease_candidates = body.result.disease_candidates.slice(0, count);
    page.setData({ photoPath: '/photo.jpg' });
    const request = page.recognize(); pending.resolve(body); await request;
    assert.equal(JSON.stringify(page.data.resultView.primaryDisease), JSON.stringify(body.result.disease_candidates[0]));
    assert.equal(JSON.stringify(page.data.resultView.alternativeDiseases), JSON.stringify(body.result.disease_candidates.slice(1)));
  }
});

for (const status of ['healthy', 'uncertain', 'not_tomato']) {
  test(`page does not manufacture disease percentages for ${status}`, async () => {
    const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
    page.setData({ photoPath: '/photo.jpg' });
    const request = page.recognize(); pending.resolve(response(status)); await request;
    assert.equal(page.data.resultView.primaryDisease, null);
    assert.equal(JSON.stringify(page.data.resultView.alternativeDiseases), '[]');
  });
}

test('network timeout, disconnect and native cancellation have distinct codes', async () => {
  for (const [errMsg, code] of [['uploadFile:fail timeout', 'NETWORK_TIMEOUT'], ['connection refused', 'NETWORK_ERROR'], ['uploadFile:fail abort', 'REQUEST_CANCELLED']]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.fail({ errMsg });
    await assert.rejects(request.promise, (error) => error.code === code && /[\u4e00-\u9fff]/.test(error.message));
  }
});

test('abort settles once even if native abort callback or late success occurs', async () => {
  const { state, service } = transport();
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
  request.abort(); request.abort();
  state.options.success({ statusCode: 200, data: JSON.stringify(response()) });
  await assert.rejects(request.promise, (error) => error.code === 'REQUEST_CANCELLED');
  assert.equal(state.aborted, 1);
});

test('upload progress is clamped, ignores invalid/late values and protects against callback throws', async () => {
  const { state, service } = transport();
  const seen = [];
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg', onProgress(value) { seen.push(value); if (value === 50) throw new Error('UI'); } });
  for (const progress of [-3, 50, 110, NaN]) state.progress({ progress });
  state.options.success({ statusCode: 200, data: JSON.stringify(response()) });
  state.progress({ progress: 60 });
  await request.promise;
  assert.deepEqual(seen, [0, 50, 100]);
});

test('minimal native task mocks are supported and synchronous upload errors are normalized', async () => {
  const service = loadService({ uploadFile(options) { options.success({ statusCode: 200, data: JSON.stringify(response()) }); return {}; } });
  await service.uploadDiagnosis({ filePath: '/photo.jpg', onProgress() {} }).promise;
  const broken = loadService({ uploadFile() { throw new Error('platform error'); } });
  await assert.rejects(broken.uploadDiagnosis({ filePath: '/photo.jpg' }).promise, (error) => error.code === 'NETWORK_ERROR');
});

test('page rejects duplicate submissions, locks edits and selection, and retains input on success', async () => {
  const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
  page.setData({ photoPath: '/photo.jpg', description: '三天' });
  const request = page.recognize();
  await page.recognize();
  page.choosePhoto(); page.removePhoto(); page.onDescriptionInput({ detail: { value: '修改' } });
  assert.equal(pending.state.calls, 1);
  assert.equal(page.data.busy, true);
  assert.equal(page.data.photoPath, '/photo.jpg');
  assert.equal(pending.state.options.description, '三天');
  pending.state.options.onProgress(100);
  assert.equal(page.data.uploadComplete, true);
  pending.resolve(response()); await request;
  assert.equal(page.data.busy, false);
  assert.equal(page.data.resultView.label, '疑似病害');
  assert.equal(page.data.description, '三天');
  assert.equal(page.data.photoPath, '/photo.jpg');
});

test('page failure resets busy and keeps input for explicit retry', async () => {
  const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
  page.setData({ photoPath: '/photo.jpg', description: '叶片黄斑' });
  const request = page.recognize();
  pending.reject(Object.assign(new Error('网络不通，请重试'), { code: 'NETWORK_ERROR' }));
  await request;
  assert.equal(page.data.busy, false);
  assert.equal(page.data.error, '网络不通，请重试');
  assert.equal(page.data.photoPath, '/photo.jpg');
  assert.equal(page.data.description, '叶片黄斑');
  assert.equal(page.data.resultView, null);
  const retry = page.recognize(); await retry;
  assert.equal(pending.state.calls, 2);
});

test('accepted new photo clears prior diagnosis and error, preview opens local photo', () => {
  const { page, wx } = pageHarness(() => { throw new Error('must not upload on selection'); });
  page.setData({ photoPath: '/old.jpg', resultView: { summary: 'old' }, error: 'old error' });
  page.choosePhoto();
  assert.equal(wx.selection.count, 1);
  assert.equal(JSON.stringify(wx.selection.mediaType), '["image"]');
  assert.equal(JSON.stringify(wx.selection.sourceType), '["album","camera"]');
  assert.equal(JSON.stringify(wx.selection.sizeType), '["compressed"]');
  choose(wx);
  assert.equal(page.data.photoPath, '/new.jpg');
  assert.equal(page.data.resultView, null);
  assert.equal(page.data.error, '');
  page.previewPhoto();
  assert.equal(wx.preview.current, '/new.jpg');
  assert.equal(wx.preview.urls[0], '/new.jpg');
});

test('oversized/unreadable images do not replace selected image, selection cancel is quiet', () => {
  const { page, wx } = pageHarness();
  page.setData({ photoPath: '/old.jpg' });
  page.choosePhoto(); choose(wx, { tempFilePath: '/huge.jpg', size: config.MAX_IMAGE_BYTES + 1 });
  assert.equal(page.data.photoPath, '/old.jpg');
  assert.match(page.data.error, /8 MB/);
  page.choosePhoto(); choose(wx, { tempFilePath: '/unknown.jpg' });
  assert.match(page.data.error, /无法读取/);
  page.setData({ error: '' }); page.choosePhoto();
  wx.selection.fail({ errMsg: 'chooseMedia:fail cancel' }); wx.selection.complete();
  assert.equal(page.data.error, ''); assert.equal(page.data.choosing, false);
});

test('description changes and removal clear stale output and enforce 500 characters', () => {
  const { page } = pageHarness();
  page.setData({ photoPath: '/photo.jpg', resultView: { summary: 'old' }, error: 'old error' });
  page.onDescriptionInput({ detail: { value: '叶'.repeat(520) } });
  assert.equal(page.data.description.length, 500);
  assert.equal(page.data.resultView, null); assert.equal(page.data.error, '');
  page.setData({ resultView: { summary: 'old' }, error: 'old error' }); page.removePhoto();
  assert.equal(page.data.photoPath, ''); assert.equal(page.data.resultView, null); assert.equal(page.data.error, '');
});

test('unload aborts active request and suppresses late progress, success and selection updates', async () => {
  const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
  page.setData({ photoPath: '/photo.jpg' }); const request = page.recognize();
  page.onUnload(); const count = page.updates.length;
  pending.state.options.onProgress(90); pending.resolve(response()); await request;
  assert.equal(pending.state.aborted, 1); assert.equal(page.updates.length, count);
  const other = pageHarness(); other.page.choosePhoto(); other.page.onUnload();
  const before = other.page.updates.length; choose(other.wx);
  assert.equal(other.page.updates.length, before);
});

test('unload suppresses late rejection and no photo means no network call', async () => {
  const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
  await page.recognize(); assert.equal(pending.state.calls, 0); assert.match(page.data.error, /先选择/);
  page.setData({ photoPath: '/photo.jpg' }); const request = page.recognize();
  page.onUnload(); const before = page.updates.length;
  pending.reject(Object.assign(new Error('late'), { code: 'NETWORK_ERROR' })); await request;
  assert.equal(page.updates.length, before);
});

test('every application JSON parses and all production JavaScript passes Node syntax checks', () => {
  for (const file of ['project.config.json', 'miniprogram/app.json', 'miniprogram/sitemap.json', 'miniprogram/pages/index/index.json']) {
    assert.doesNotThrow(() => JSON.parse(fs.readFileSync(path.join(root, file), 'utf8')), file);
  }
  const project = JSON.parse(fs.readFileSync(path.join(root, 'project.config.json'), 'utf8'));
  assert.equal(project.miniprogramRoot, 'miniprogram/'); assert.equal(project.compileType, 'miniprogram'); assert.equal(project.appid, 'touristappid');
  for (const file of ['miniprogram/app.js', 'miniprogram/config.js', 'miniprogram/services/diagnosis.js', 'miniprogram/pages/index/index.js']) {
    const result = spawnSync(process.execPath, ['--check', path.join(root, file)], { encoding: 'utf8' });
    assert.equal(result.status, 0, `${file}: ${result.stderr}`);
  }
});

test('all WXML conditional visibility uses dynamic bindings rather than truthy literal strings', () => {
  const template = fs.readFileSync(path.join(root, 'miniprogram/pages/index/index.wxml'), 'utf8');
  const conditionals = [...template.matchAll(/\bwx:(?:if|elif)\s*=\s*(["'])(.*?)\1/g)];
  assert.ok(conditionals.length >= 10, 'audit must cover photo, busy, errors and result sections');
  for (const conditional of conditionals) {
    assert.match(conditional[2], /^\{\{\s*\S[\s\S]*\}\}$/, `conditional must evaluate page state: ${conditional[0]}`);
  }
});
