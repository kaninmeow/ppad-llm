const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const config = require('../miniprogram/config');
function loadService(wx, timers = { setTimeout, clearTimeout, Date }) {
  const context = { module: { exports: {} }, require: () => config, wx, ...timers };
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
function clock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    Date: { now: () => now },
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    advance(delay) {
      const end = now + delay;
      while (true) {
        const next = [...timers].filter(([, item]) => item.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at; timers.delete(next[0]); next[1].callback();
      }
      now = end;
    },
    get size() { return timers.size; }
  };
}
const queued = (extra = {}) => ({ job_id: 'job_123-abc', job_token: 'token_123-abc', status: 'checking', poll_after_ms: 1500, ...extra });
function transport({ autoLogin = true } = {}) {
  const time = clock();
  const state = { calls: 0, aborted: 0, polls: [], logins: 0 };
  const service = loadService({
    login(options) { state.login = options; state.logins++; if (autoLogin) options.success({ code: 'login-code' }); },
    uploadFile(options) {
      state.calls++; state.options = options;
      return { onProgressUpdate(callback) { state.progress = callback; },
        abort() { state.aborted++; options.fail({ errMsg: 'uploadFile:fail abort' }); } };
    },
    request(options) {
      state.polls.push(options);
      return { abort() { state.aborted++; options.fail({ errMsg: 'request:fail abort' }); } };
    }
  }, time);
  // Existing result-schema cases now exercise final authenticated GET delivery.
  state.respond = (reply) => {
    if (reply.statusCode === 200) {
      state.options.success({ statusCode: 202, data: JSON.stringify(queued()) }); time.advance(1500);
      state.polls.at(-1).success(reply);
    } else state.options.success(reply);
  };
  return { state, service, time };
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
  assert.equal(state.options.url, config.API_BASE_URL.replace(/\/+$/, '') + '/api/diagnose');
  assert.equal(state.options.formData.login_code, 'login-code');
  assert.equal(state.options.name, 'image');
  assert.equal(state.options.filePath, '/tomato.jpg');
  assert.equal(state.options.formData.description, '第三天有斑点');
  assert.equal(state.options.timeout, 90000);
  state.respond({ statusCode: 200, data: JSON.stringify(response()) });
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
    state.respond({ statusCode: 200, data: JSON.stringify(body) });
    assert.equal((await request.promise).result.status, status);
  });
}

test('server error uses stable safe fixed Chinese message', async () => {
  const { state, service } = transport();
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
  state.respond({ statusCode: 429, data: JSON.stringify({ error: { code: 'MODEL_RATE_LIMITED', message: '<b>请求较多</b>\n请稍后重试' } }) });
  await assert.rejects(request.promise, (error) => error.code === 'MODEL_RATE_LIMITED' && error.message === '当前识别请求较多，请稍后重试。');
});

test('unknown or malformed HTTP errors use a stable readable fallback', async () => {
  for (const body of [{ error: { code: 'NEW_ERROR', message: '' } }, {}, null]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.respond({ statusCode: 503, data: JSON.stringify(body) });
    await assert.rejects(request.promise, (error) => ['NEW_ERROR', 'REQUEST_FAILED'].includes(error.code) && error.message.includes('请稍后重试'));
  }
});

test('non-JSON proxy HTML is rejected safely on success and failure responses', async () => {
  for (const statusCode of [200, 502]) {
    const { state, service } = transport();
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.respond({ statusCode, data: '<html>gateway error</html>' });
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
    state.respond({ statusCode: 200, data: JSON.stringify(body) });
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
    state.respond({ statusCode: 200, data: JSON.stringify(body) });
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
    state.respond({ statusCode: 200, data: JSON.stringify(body) });
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
  state.respond({ statusCode: 200, data: JSON.stringify(response()) });
  state.progress({ progress: 60 });
  await request.promise;
  assert.deepEqual(seen, [0, 50, 100]);
});

test('minimal native task mocks and synchronous platform errors are normalized', async () => {
  const time = clock();
  const service = loadService({ login(o) { o.success({ code: 'code' }); },
    uploadFile(o) { o.success({ statusCode: 202, data: JSON.stringify(queued()) }); return {}; },
    request(o) { o.success({ statusCode: 200, data: response() }); return {}; }
  }, time);
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg', onProgress() {} });
  time.advance(1500); await request.promise;
  const broken = loadService({ login(o) { o.success({ code: 'code' }); }, uploadFile() { throw new Error('platform error'); } }, clock());
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

test('new photo clears prior diagnosis and approval; preview requires approval', () => {
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
  assert.equal(page.data.photoApproved, false);
  page.previewPhoto(); assert.equal(wx.preview, undefined);
  page.setData({ photoApproved: true }); page.previewPhoto();
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
  function projectJson(directory) {
    return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
      if (['.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache'].includes(entry.name)) return [];
      const file = path.join(directory, entry.name);
      return entry.isDirectory() ? projectJson(file) : (entry.name.endsWith('.json') ? [file] : []);
    });
  }
  for (const file of projectJson(root)) {
    assert.doesNotThrow(() => JSON.parse(fs.readFileSync(file, 'utf8')), file);
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


test('login precedes upload, failures and malformed codes upload nothing', async () => {
  for (const result of [null, {}, { code: '' }, { code: 'x'.repeat(513) }]) {
    const { state, service } = transport({ autoLogin: false });
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    assert.equal(state.calls, 0);
    if (result === null) state.login.fail({ errMsg: 'unsafe raw reason' }); else state.login.success(result);
    await assert.rejects(request.promise, e => e.code === 'LOGIN_FAILED' && !e.message.includes('unsafe'));
    assert.equal(state.calls, 0);
  }
});

test('202 uses only configured origin and bearer token, stages and repeated polls wait', async () => {
  const { state, service, time } = transport(); const stages = [];
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg', onStage(stage) { stages.push(stage); } });
  state.options.success({ statusCode: 202, data: JSON.stringify(queued({ poll_after_ms: 0 })) });
  assert.deepEqual(stages, ['checking']); time.advance(999); assert.equal(state.polls.length, 0);
  time.advance(1); const poll = state.polls[0];
  assert.equal(poll.url, config.API_BASE_URL.replace(/\/+$/, '') + '/api/diagnose/job_123-abc');
  assert.equal(poll.header.Authorization, 'Bearer token_123-abc'); assert.equal(poll.method, 'GET');
  poll.success({ statusCode: 202, data: { status: 'analysing', poll_after_ms: 999999 } });
  assert.deepEqual(stages, ['checking', 'analysing']); time.advance(4999); assert.equal(state.polls.length, 1);
  time.advance(1); state.polls[1].success({ statusCode: 200, data: response() });
  assert.equal((await request.promise).request_id, 'request-123'); assert.equal(state.calls, 1); assert.equal(time.size, 0);
});

test('upload direct 200 and malformed queued envelopes fail closed without polling', async () => {
  const bodies = [response(), null, {}, queued({ job_id: '../evil' }), queued({ job_token: 'bad token' }),
    queued({ job_id: 'https://evil.test' }), queued({ job_id: 'a'.repeat(513) }), queued({ job_token: '' }),
    queued({ status: 'analysing' }), queued({ poll_after_ms: '1500' }), queued({ poll_after_ms: NaN }),
    queued({ poll_url: 'https://evil.test/job' }), queued({ url: '//evil.test' })];
  for (let i = 0; i < bodies.length; i++) {
    const { state, service } = transport(); const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.success({ statusCode: i === 0 ? 200 : 202, data: JSON.stringify(bodies[i]) });
    await assert.rejects(request.promise, e => e.code === 'INVALID_RESPONSE'); assert.equal(state.polls.length, 0);
  }
});

test('malformed polling state, delay and redirects reject instead of exposing results', async () => {
  for (const body of [{ status: 'pass', poll_after_ms: 1000 }, { status: 'checking' },
    { status: 'analysing', poll_after_ms: true }, { status: 'checking', poll_after_ms: 1000, url: 'https://evil.test' }]) {
    const { state, service, time } = transport(); const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.success({ statusCode: 202, data: JSON.stringify(queued()) }); time.advance(1500);
    state.polls[0].success({ statusCode: 202, data: body });
    await assert.rejects(request.promise, e => e.code === 'INVALID_RESPONSE'); assert.equal(time.size, 0);
  }
});

test('abort in login, upload, poll and delay cancels owned work and ignores all late callbacks', async () => {
  for (const phase of ['login', 'upload', 'delay', 'poll']) {
    const { state, service, time } = transport({ autoLogin: phase !== 'login' }); const stages = [];
    const request = service.uploadDiagnosis({ filePath: '/photo.jpg', onStage: s => stages.push(s) });
    if (['delay', 'poll'].includes(phase)) state.options.success({ statusCode: 202, data: JSON.stringify(queued()) });
    if (phase === 'poll') time.advance(1500);
    request.abort(); request.abort();
    const before = [state.calls, state.polls.length, stages.length];
    state.login.success({ code: 'late-code' });
    if (state.options) state.options.success({ statusCode: 202, data: JSON.stringify(queued()) });
    if (state.polls[0]) state.polls[0].success({ statusCode: 200, data: response() });
    time.advance(32 * 60 * 1000);
    await assert.rejects(request.promise, e => e.code === 'REQUEST_CANCELLED');
    assert.deepEqual([state.calls, state.polls.length, stages.length], before); assert.equal(time.size, 0);
    assert.equal(state.aborted, ['upload', 'poll'].includes(phase) ? 1 : 0);
  }
});

test('overall deadline is 32 minutes from login and aborts an active poll independently of network timeout', async () => {
  const { state, service, time } = transport({ autoLogin: false });
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg' }); time.advance(1000);
  state.login.success({ code: 'code' });
  assert.equal(state.options.timeout, config.UPLOAD_TIMEOUT_MS);
  state.options.success({ statusCode: 202, data: JSON.stringify(queued()) }); time.advance(1500);
  assert.ok(state.polls[0].timeout <= config.UPLOAD_TIMEOUT_MS);
  time.advance(32 * 60 * 1000 - 2501); assert.equal(state.aborted, 0);
  time.advance(1); await assert.rejects(request.promise, e => e.code === 'SECURITY_TIMEOUT');
  assert.equal(state.aborted, 1); assert.equal(time.size, 0); assert.equal(state.calls, 1);
});

test('all moderation, login and job errors use fixed messages rather than upstream text', async () => {
  for (const code of ['SECURITY_NOT_CONFIGURED', 'SECURITY_UNAVAILABLE', 'SECURITY_TIMEOUT', 'LOGIN_FAILED',
    'CONTENT_REJECTED', 'TOO_MANY_REQUESTS', 'QUEUE_FULL', 'JOB_NOT_FOUND', 'NOT_FOUND']) {
    const { state, service } = transport(); const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
    state.options.success({ statusCode: 422, data: JSON.stringify({ error: { code, message: 'unsafe raw content' } }) });
    await assert.rejects(request.promise, e => e.code === code && !e.message.includes('unsafe') &&
      (code !== 'CONTENT_REJECTED' || e.message === '您提交的内容含违规信息。'));
  }
});

test('rejection clears all submitted content and approval; page never displays upstream reason', async () => {
  const pending = pendingUpload(); const { page } = pageHarness(pending.upload);
  page.setData({ photoPath: '/photo.jpg', description: 'input', photoApproved: true }); const request = page.recognize();
  assert.equal(page.data.photoApproved, false);
  pending.state.options.onStage('checking'); assert.equal(page.data.stage, 'checking');
  pending.state.options.onStage('analysing'); assert.equal(page.data.stage, 'analysing');
  pending.reject(Object.assign(new Error('unsafe raw reason'), { code: 'CONTENT_REJECTED' })); await request;
  assert.equal(page.data.photoPath, ''); assert.equal(page.data.description, ''); assert.equal(page.data.resultView, null);
  assert.equal(page.data.photoApproved, false); assert.equal(page.data.rejected, true);
  assert.equal(page.data.error, '您提交的内容含违规信息。');
});

test('successful completion alone approves preview; failures and stale stages cannot approve', async () => {
  const pending = pendingUpload(); const { page, wx } = pageHarness(pending.upload);
  page.setData({ photoPath: '/photo.jpg', photoApproved: true }); const request = page.recognize();
  page.previewPhoto(); assert.equal(wx.preview, undefined);
  pending.resolve(response()); await request; assert.equal(page.data.photoApproved, true);
  page.previewPhoto(); assert.equal(wx.preview.current, '/photo.jpg');
  const before = page.updates.length; pending.state.options.onStage('checking'); pending.state.options.onProgress(99);
  assert.equal(page.updates.length, before);
  const failed = pendingUpload(); const other = pageHarness(failed.upload).page;
  other.setData({ photoPath: '/photo.jpg', photoApproved: true }); const work = other.recognize();
  failed.reject(Object.assign(new Error('service'), { code: 'SECURITY_UNAVAILABLE' })); await work;
  assert.equal(other.data.photoApproved, false); assert.equal(other.data.photoPath, '/photo.jpg');
});

test('WXML gates photo pixels and gives rejection its own fixed-message-only panel', () => {
  const template = fs.readFileSync(path.join(root, 'miniprogram/pages/index/index.wxml'), 'utf8');
  assert.match(template, /<image\s+wx:if="\{\{photoApproved\}\}"/);
  const rejected = template.match(/<view wx:if="\{\{rejected\}\}"[^>]*>([\s\S]*?)<\/view>/);
  assert.ok(rejected); assert.equal(rejected[1], '您提交的内容含违规信息。');
  assert.match(template, /wx:elif="\{\{error\}\}"/);
});


test('poll network failures do not repost and completed native slots ignore duplicate callbacks', async () => {
  const { state, service, time } = transport(); const stages = [];
  const request = service.uploadDiagnosis({ filePath: '/photo.jpg', onStage: s => stages.push(s) });
  state.options.success({ statusCode: 202, data: JSON.stringify(queued()) }); time.advance(1500);
  state.login.success({ code: 'duplicate' });
  state.options.success({ statusCode: 202, data: JSON.stringify(queued()) });
  assert.equal(state.calls, 1); assert.equal(state.polls.length, 1); assert.deepEqual(stages, ['checking']);
  state.polls[0].fail({ errMsg: 'request:fail timeout upstream raw text' });
  await assert.rejects(request.promise, e => e.code === 'NETWORK_TIMEOUT' && !e.message.includes('raw'));
  state.polls[0].success({ statusCode: 200, data: response() }); time.advance(6000);
  assert.equal(state.calls, 1); assert.equal(state.polls.length, 1); assert.equal(time.size, 0);
});

test('terminal rejection returned by polling is fixed and never exposes unchecked content', async () => {
  const { state, service, time } = transport(); const request = service.uploadDiagnosis({ filePath: '/photo.jpg' });
  state.options.success({ statusCode: 202, data: JSON.stringify(queued()) }); time.advance(1500);
  state.polls[0].success({ statusCode: 422, data: { error: { code: 'CONTENT_REJECTED', message: 'unsafe input' } } });
  await assert.rejects(request.promise, e => e.code === 'CONTENT_REJECTED' && e.message === '您提交的内容含违规信息。');
  assert.equal(time.size, 0);
});
