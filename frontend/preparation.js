/* Selected settings prepare on the existing worker, never inside a Queue job. */
window.BorasukiPreparation = ({call, translate:t, values, error}) => {
  const el = id => document.getElementById(id);
  const state = {key:null, token:null, timer:null, serial:Promise.resolve(), version:0, failure:null, ready:false};
  let latest = null;

  function schedule(payload, key) {
    clearTimeout(state.timer);
    window.BorasukiWait.render(el('enginePreparationProgress'), null, false, t);
    const previous = state.token;
    state.key = key; state.token = null; state.failure = null;
    el('enginePreparationFailure').open = false;
    const version = ++state.version;
    if (previous) state.serial = state.serial.then(() => call('cancel_preparation', previous)).catch(error);
    if (!payload) return;
    state.timer = setTimeout(() => {
      state.serial = state.serial.then(async () => {
        if (version !== state.version) return;
        try {
          const token = await call('request_preparation', payload.source, payload.upscale, payload.denoise, payload.gpu_id);
          if (version !== state.version) { await call('cancel_preparation', token); return; }
          state.token = token;
        } catch (problem) {
          if (version === state.version) state.failure = problem.failure || {code:problem.message};
        }
      });
    }, 600);
  }

  function render(snapshot) {
    latest = snapshot;
    const payload = snapshot.setup.status === 'ready' ? values() : null;
    const key = payload ? JSON.stringify([payload, snapshot.runtime.gpus.map(gpu => [gpu.id, gpu.name, gpu.driver])]) : null;
    if (key !== state.key) schedule(payload, key);
    const task = snapshot.preparation?.id === state.token ? snapshot.preparation : null;
    const status = state.failure ? 'failed' : task?.status || 'waiting';
    const ready = Boolean(payload && status === 'ready');
    state.ready = ready;
    el('enginePreparation').hidden = !payload || ready;
    const failure = state.failure || task?.failure;
    const phase = task?.progress?.stage;
    const stage = phase === 'encoder_check' ? 0 : ['engine_loading','engine_building'].includes(phase) ? 1 : ['engine_warming','verification'].includes(phase) ? 2 : -1;
    el('enginePreparationSteps').hidden = ['failed','cancelled'].includes(status);
    el('enginePreparationSteps').setAttribute('aria-label', t('preparation.label'));
    ['engineStepCheck','engineStepGPU','engineStepVerify'].forEach((id, index) => {
      el(id).classList.toggle('is-current', stage === index);
      el(id).classList.toggle('is-complete', stage > index);
      if (stage === index) el(id).setAttribute('aria-current', 'step');
      else el(id).removeAttribute('aria-current');
    });
    el('enginePreparation').classList.toggle('has-error', status === 'failed');
    const occupied = snapshot.jobs?.some(job => job.locked) || ['preparing', 'rendering'].includes(snapshot.preview?.status);
    el('enginePreparationStatus').textContent = t(failure?.code || (status === 'preparing' && task?.progress?.stage ?
      'wait.' + task.progress.stage : 'preparation.' + (status === 'waiting' && occupied ? 'waiting_gpu' : status)));
    window.BorasukiWait.render(el('enginePreparationProgress'), task?.progress, ['waiting', 'preparing'].includes(status), t);
    el('enginePreparationRetry').hidden = !['failed', 'cancelled'].includes(status) || failure?.retryable === false;
    el('enginePreparationCancel').hidden = !['waiting', 'preparing'].includes(status) || !state.token;
    el('enginePreparationDetails').hidden = !failure?.detail;
    el('enginePreparationFailure').hidden = !failure?.detail;
    el('enginePreparationTechnical').textContent = failure?.detail || '';
    el('enginePreparationDetails').onclick = event => {
      event?.preventDefault();
      const details = el('enginePreparationFailure');
      details.open = !details.open;
      if (details.open) window.BorasukiUI.reveal(el('enginePreparationTechnical'));
    };
    if (status === 'failed' && !el('page-add').hidden && state.revealedFailure !== state.version) {
      state.revealedFailure = state.version;
      window.BorasukiUI.reveal(el('enginePreparation'));
    }
    return ready;
  }

  el('enginePreparationRetry').onclick = () => { state.key = null; render(latest); };
  el('enginePreparationCancel').onclick = () => {
    if (state.token) call('cancel_preparation', state.token).catch(error);
  };
  return {render, ready:() => state.ready && state.key === JSON.stringify([values(),
    latest.runtime.gpus.map(gpu => [gpu.id, gpu.name, gpu.driver])]),
    retry:() => { state.key = null; if (latest) render(latest); }};
};
