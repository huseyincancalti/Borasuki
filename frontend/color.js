/* One video-wide color result; no upscale or denoise decisions. */
window.BorasukiColor = ({call, translate:t, source, editing, changed, error, refresh, recover}) => {
  const el = id => document.getElementById(id);
  const keys = ['contrast', 'brightness', 'saturation'];
  const state = {token:null, version:0, status:'empty', baseline:null, overrides:{}, loaded:null, error:null, failure:null, requests:Promise.resolve()};
  const mode = () => el('colorMode').value;
  const profile = () => el('adaptiveProfile').value;
  const baseline = analysis => ({...analysis, ...analysis?.profiles?.[profile()]});
  const neutral = key => key === 'brightness' ? 0 : 1;
  const factor = key => neutral(key) + Number(el(key).value) / 100;
  const display = (key, value) => { el(key).value = (value - neutral(key)) * 100; };
  const ready = () => mode() !== 'adaptive' || state.status === 'ready' && Boolean(state.token && state.baseline);

  function invalidate() {
    const token = state.token;
    state.version++;
    Object.assign(state, {token:null, status:'empty', baseline:null, analysis:null, overrides:{}, loaded:null, error:null, failure:null});
    el('analysisDetails').open = false;
    window.BorasukiWait.render(el('analysisProgress'), null, false, t);
    if (token) call('cancel_analysis', token).catch(problem => error(problem.message));
    render();
  }

  async function request(saved = false, overrides = {}) {
    invalidate(); changed();
    state.pendingOverrides = {...overrides};
    if (!source()) return;
    const version = state.version;
    state.status = 'waiting'; render();
    try {
      const job = saved ? editing() : null;
      const path = source();
      const pending = state.requests.then(() => version === state.version ?
        call('request_analysis', path, job ? {id:job.id, token:job.edit_token} : null) : null);
      state.requests = pending.catch(() => {});
      const token = await pending;
      if (!token) return;
      if (version !== state.version) { await call('cancel_analysis', token); return; }
      state.token = token;
      await refresh(true);
    } catch (problem) {
      if (version === state.version) { state.status = 'failed'; state.error = problem.failure?.code || problem.message; state.failure = problem.failure; render(); }
    }
  }

  function select(reset = false) {
    if (reset) {
      invalidate();
      if (mode() === 'custom') keys.forEach(key => { el(key).value = 0; });
      if (mode() === 'adaptive') { request(); return; }
    }
    render();
  }

  async function load(job) {
    invalidate();
    keys.forEach(key => display(key, job.grade[key]));
    if (job.color_mode === 'adaptive') await request(true);
    render();
  }

  function render(snapshot) {
    const task = snapshot?.analysis;
    if (state.token && task?.id === state.token) {
      state.status = task.status; state.error = task.error; state.failure = task.failure;
      if (task.status === 'ready' && state.loaded !== task.id) {
        state.loaded = task.id; state.analysis = task.analysis; state.baseline = baseline(task.analysis); state.overrides = {...task.overrides, ...state.pendingOverrides};
        keys.forEach(key => display(key, state.overrides[key] ?? state.baseline[key]));
        if (Object.keys(state.overrides).length) el('colorAdjustments').open = true;
      }
    }
    const adaptive = mode() === 'adaptive';
    const loading = adaptive && ['waiting','analyzing'].includes(state.status);
    el('adaptiveProfileRow').hidden = !adaptive;
    el('adaptiveProfile').disabled = loading;
    el('adaptiveProfileHint').textContent = t('adaptive.' + profile() + '_hint');
    el('custom').hidden = !adaptive && mode() !== 'custom';
    el('colorAdjustments').hidden = !adaptive && mode() !== 'custom';
    if (state.adjustmentMode !== mode()) {
      el('colorAdjustments').open = mode() === 'custom' || adaptive && Object.keys(state.overrides).length > 0;
      state.adjustmentMode = mode();
    }
    el('custom').setAttribute('aria-busy', String(loading));
    el('adaptiveNote').hidden = !adaptive;
    window.BorasukiWait.render(el('analysisProgress'), task?.id === state.token ? task.progress : null, loading, t);
    const failed = adaptive && state.status === 'failed';
    el('adaptiveNote').classList.toggle('has-error', failed);
    el('analysisRetry').hidden = !failed || state.failure?.retryable === false;
    el('analysisDetails').hidden = !failed || !state.failure?.detail;
    el('analysisTechnical').textContent = state.failure?.detail ? `${state.error}\n${source() || ''}\n${state.failure.detail}` : '';
    el('analysisLogs').hidden = !failed;
    el('analysisRecovery').hidden = !failed || !['source', 'setup'].includes(state.failure?.recovery);
    el('analysisRecovery').disabled = state.failure?.recovery === 'source' && Boolean(editing());
    el('analysisRecovery').textContent = t(state.failure?.recovery === 'setup' ? 'ui.setup_open' : editing() ? 'ui.discard_edit_first' : 'ui.choose_another_video');
    el('analysisCancel').hidden = !loading;
    if (adaptive) {
      let status = state.error ? t(state.error) : t(state.status === 'ready' ? 'ui.analysis_ready' : loading ? 'ui.analyzing_wait' : 'ui.analysis_empty');
      if (loading && task?.id === state.token && task.progress?.stage) status = t('wait.' + task.progress.stage);
      el('analysisStatus').textContent = status;
    }
    if (failed && !el('page-add').hidden && state.revealedFailure !== state.version) {
      state.revealedFailure = state.version;
      window.BorasukiUI.reveal(el('adaptiveNote'));
    }
    for (const key of keys) {
      const manual = Object.hasOwn(state.overrides, key);
      el(key + 'OverrideRow').hidden = !adaptive;
      el(key + 'Override').checked = manual;
      el(key + 'Override').disabled = !ready();
      el(key).disabled = adaptive && (!ready() || !manual);
      el(key).setAttribute('aria-readonly', String(el(key).disabled));
      if (adaptive && !ready()) {
        el(key).value = '0'; el(key + 'Value').value = '—';
      } else {
        el(key + 'Value').value = String(Number(Number(el(key).value).toFixed(4)));
      }
    }
    if (!ready()) { el('start').disabled = true; el('openPreview').disabled = true; }
  }

  for (const key of keys) {
    el(key).oninput = () => {
      if (mode() === 'adaptive' && ready() && Object.hasOwn(state.overrides, key)) state.overrides[key] = factor(key);
      render();
    };
    el(key + 'Override').onchange = () => {
      if (!ready() || mode() !== 'adaptive') return;
      if (el(key + 'Override').checked) state.overrides[key] = state.baseline[key];
      else delete state.overrides[key];
      display(key, state.overrides[key] ?? state.baseline[key]);
      changed(); render();
    };
  }
  el('analysisRetry').onclick = () => request(false, state.pendingOverrides);
  el('adaptiveProfile').onchange = () => {
    changed();
    if (!state.analysis?.profiles) { request(); return; }
    state.baseline = baseline(state.analysis); state.overrides = {}; state.pendingOverrides = {};
    keys.forEach(key => display(key, state.baseline[key]));
    render();
  };
  el('analysisRecovery').onclick = () => recover(state.failure.recovery);
  el('analysisLogs').onclick = () => call('open_logs').catch(problem => error(problem));
  el('analysisCancel').onclick = () => {
    invalidate(); changed(); state.status = 'failed'; state.error = 'ui.analysis_cancelled'; render();
  };
  return {state, select, load, render, request, invalidate, ready,
    payload() {
      if (mode() !== 'adaptive') return null;
      if (!ready()) throw new Error('error.analysis_stale');
      return {token:state.token, profile:profile(), overrides:{...state.overrides}};
    }
  };
};
