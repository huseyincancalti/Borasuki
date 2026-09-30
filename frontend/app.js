const $ = id => document.getElementById(id);
const UI = window.BorasukiUI;
const node = UI.element;
// Backend snapshots are read-only to views; everything else here is transient.
const app = {
  snapshot: null, catalog: {}, media: null, editing: null, dragged: null,
  busy: false, started: false, connected: false, refreshing: null,
  settingsBusy: false, settingsWrites: Promise.resolve(), settingsPending: 0,
  pendingActions: new Set()
};
const navigation = UI.shell(name => {
  if (name !== 'preview' || !$('start').disabled) return true;
  UI.reveal(!$('enginePreparation').hidden ? $('enginePreparation') : app.media ? $('adaptiveNote') : $('drop'));
  return false;
});

const terminal = ['completed', 'failed', 'cancelled'];
const metricKeys = ['progress', 'eta', 'speed', 'elapsed', 'vram', 'frames'];
const t = (key, values = {}) => {
  let value = app.catalog[key] || key;
  for (const [name, replacement] of Object.entries(values)) value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
const clock = seconds => {
  if (seconds == null) return '—';
  const total = Math.max(0, Math.floor(seconds));
  return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60].map(n => String(n).padStart(2, '0')).join(':');
};
const latest = id => app.snapshot.jobs.find(job => job.id === id);
function syncJobActions() {
  const ready = Boolean(!app.busy && app.media && app.snapshot?.runtime.ready &&
    app.snapshot?.setup?.status === 'ready' && (!app.color || app.color.ready()) && app.preparation?.ready() &&
    !['waiting','preparing','rendering'].includes(app.snapshot.preview?.status));
  $('start').disabled = !ready;
  $('openPreview').disabled = !ready;
  app.batch?.render();
}

function showError(message) {
  const failure = message?.failure || (message?.code ? message : null);
  const key = failure?.code || message?.message || message;
  UI.message($('errorMessage'), t, key);
  app.failure = failure;
  $('errorDetails').hidden = !failure?.detail;
  $('errorTechnical').textContent = failure?.detail || '';
  $('errorDetails').open = false;
  $('error').hidden = false;
  UI.recoveryActions($('errorRecovery'), failure, t, recoverFailure);
  UI.reveal($('error'));
}
window.showError = showError;

let closePending = false;
window.closeFailed = failure => {
  closePending = false;
  $('closeStatus').hidden = true;
  showError(failure);
};
window.requestAppClose = async () => {
  if (closePending || document.querySelector('dialog[open]')) return;
  closePending = true;
  try {
    await app.settingsWrites;
    const context = await call('close_context');
    const needsConfirmation = !context.closing && (context.busy || app.media || app.editing || app.busy || app.batch?.state.paths.length);
    if (needsConfirmation && !await UI.confirm($('confirmation'), {
      title:t('ui.exit'), body:t('confirm.exit'), accept:t('ui.exit'), safe:t('ui.keep_open')
    })) { closePending = false; return; }
    UI.message($('closeStatus'), t, 'ui.closing');
    $('closeStatus').hidden = false;
    $('closeStatus').scrollIntoView({block:'nearest'});
    await call('close_app', Boolean(needsConfirmation || context.closing));
  } catch (error) { window.closeFailed(error.failure || {code:error.message}); }
};

async function recoverFailure(kind, control, job = null) {
  await perform(async () => {
    if (kind === 'setup') { page('settings'); $('setupCheck').focus(); }
    else if (kind === 'preparation') { page('add'); app.preparation.retry(); }
    else if (kind === 'storage') { page('settings'); await refreshStorage(); $('storageRefresh').focus(); }
    else if (kind === 'output') {
      if (job) await call('open_output', job.id);
      else { page('add'); $('folder').focus(); }
    } else if (kind === 'format') {
      page('add'); $('outputFormat').focus();
    } else if (kind === 'source') {
      page('add');
      if (app.editing) throw new Error('error.finish_edit');
      if (app.busy) throw new Error('error.form_busy');
      await run(chooseVideos, 'ui.importing');
    } else if (kind === 'diagnostics') {
      const path = await call('export_diagnostics');
      if (path) { page('settings'); UI.message($('diagnosticsStatus'), t, 'ui.diagnostics_saved', {path}); $('exportDiagnostics').focus(); }
    }
  }, control);
}

async function call(name, ...args) {
  const result = await window.pywebview.api[name](...args);
  if (!result.ok) throw Object.assign(new Error(result.error), {failure:result.failure, code:result.error});
  return result.data;
}

function page(name) {
  navigation.navigate(name);
}

function translate() {
  document.querySelectorAll('[data-i18n]').forEach(el => UI.localize(el, t));
  UI.help(t);
  document.documentElement.lang = app.snapshot.settings.language;
  $('start').textContent = t(app.editing ? 'ui.save_job' : 'ui.start');
  $('addTitle').textContent = t(app.editing ? 'ui.edit_job' : 'nav.add');
  if (app.media) $('source').textContent = app.media.name;
  $('browse').textContent = t('ui.browse');
  updateOutputWarning();
  app.presets?.render();
  if (!$('error').hidden) UI.recoveryActions($('errorRecovery'), app.failure, t, recoverFailure);
}

function updateColor(reset = false) {
  const mode = $('colorMode').value;
  if (reset) app.compare?.invalidate();
  document.querySelectorAll('[data-color]').forEach(el => {
    el.checked = el.dataset.color === mode;
  });
  app.color?.select(reset);
  syncJobActions();
}
$('colorMode').onchange = () => updateColor(true);
document.querySelectorAll('[data-color]').forEach(el => { el.onchange = () => { if (el.checked) { $('colorMode').value = el.dataset.color; updateColor(true); } }; });

function updateDenoise() {
  $('denoiseStrength').disabled = !$('denoiseEnabled').checked;
  $('denoiseValue').value = $('denoiseEnabled').checked ? t('ui.denoise_level', {level:$('denoiseStrength').value}) : t('ui.off');
}
$('denoiseEnabled').onchange = updateDenoise;
$('denoiseStrength').oninput = updateDenoise;
const denoiseLabel = job => job.denoise.enabled ? t('ui.denoise_level', {level:job.denoise.strength}) : t('ui.off');

function applyMedia(value) {
  if (!value) return;
  if (value.import_error) {
    app.compare?.invalidate(); app.color?.invalidate();
    app.media = null;
    $('drop').classList.remove('hasSource');
    $('browse').textContent = t('ui.browse');
    $('source').removeAttribute('title');
    UI.message($('source'), t, 'ui.import_failed_source', {path:value.path});
    $('mediaInfo').textContent = '';
    $('colorWarning').hidden = true;
    $('start').disabled = true; $('openPreview').disabled = true;
    showError(value.import_error); page('add');
    return;
  }
  app.compare?.invalidate();
  app.media = value;
  app.color?.invalidate();
  if (!app.editing && $('colorMode').value === 'adaptive') app.color?.request(false, app.presets?.overrides());
  $('drop').classList.add('hasSource');
  $('browse').textContent = t('ui.browse');
  $('source').removeAttribute('data-i18n');
  $('source').textContent = value.name;
  $('source').title = value.path;
  $('mediaInfo').textContent = value.width + ' × ' + value.height + ' → ' + value.width * 2 + ' × ' + value.height * 2 + ' · ' + value.fps.toFixed(3) + ' FPS · ' + clock(value.duration);
  $('colorWarning').hidden = !value.assumed_color;
  if (!$('folder').value) $('folder').value = value.path.slice(0, Math.max(value.path.lastIndexOf('\\'), value.path.lastIndexOf('/')));
  $('filename').value = value.name.replace(/\.[^.]+$/, '') + '_borasuki_2x.' + $('outputFormat').value;
  updateOutputWarning();
  $('error').hidden = true;
  app.color?.render();
  syncJobActions();
  if (app.snapshot) renderSetup();
  page('add');
}
window.receiveMedia = value => {
  if (app.editing) { showError('error.finish_edit'); page('add'); return; }
  if (app.busy) { showError('error.form_busy'); return; }
  applyMedia(value);
};
window.receiveBatch = selection => {
  if (app.editing) { showError('error.finish_edit'); page('add'); return; }
  if (app.busy) { showError('error.form_busy'); return; }
  app.batch.receive(selection); page('add');
};
async function chooseVideos() {
  const selection = await call('choose_batch', false);
  if (!selection) return;
  const paths = selection.paths || [];
  if (paths.length === 1 && !app.batch.state.paths.length && !selection.failures.length) {
    applyMedia(await call('inspect_file', paths[0]));
    return;
  }
  if (!app.batch.receive(selection)) return;
  if (paths.length) applyMedia(await call('inspect_file', paths[0]));
}

function confirmAction(title, body) {
  return UI.confirm($('confirmation'), {title:t(title), body, accept:t(title === 'ui.retention' ? 'ui.confirm' : title), safe:t('ui.cancel_dialog')});
}

async function perform(action, button) {
  const view = button?.closest('.page');
  const hadFocus = document.activeElement === button;
  const key = button?.closest('[data-id]')?.dataset.id || button?.id || 'queue-command';
  if (app.pendingActions.has(key)) return;
  app.pendingActions.add(key);
  UI.busy(button, true);
  $('error').hidden = true;
  try { await action(); await refresh(true); }
  catch (error) { showError(error); }
  finally {
    app.pendingActions.delete(key);
    UI.busy(button, false);
    renderJobs();
    if (view && !view.hidden && (document.activeElement === view.querySelector('h1') || hadFocus &&
        (document.activeElement === document.body || document.activeElement.dataset.id === key))) {
      const row = [...view.querySelectorAll('[data-id]')].find(item => item.dataset.id === key);
      const target = row ? [...row.querySelectorAll('[data-action]')].find(control => control.dataset.action === button.dataset.action) : button;
      if (target?.isConnected && !target.disabled) target.focus({preventScroll:true});
      else row?.focus({preventScroll:true});
    }
  }
}

function formBusy(active, status = 'ui.working') {
  app.busy = active;
  $('jobForm').disabled = active;
  $('jobForm').setAttribute('aria-busy', String(active));
  $('discardEdit').disabled = active;
  UI.message($('formStatus'), t, active && status !== 'ui.importing' ? status : '');
  window.BorasukiWait.busy($('formStatus'), active && status !== 'ui.importing', t);
  if (active && status === 'ui.importing') app.fileDrop.loading();
  if (!active) {
    app.fileDrop.settled();
    $('browse').disabled = Boolean(app.editing);
    app.color?.render();
    syncJobActions();
    if (app.snapshot) { renderSetup(); app.preparation?.render(app.snapshot); }
  }
}

async function run(action, status = 'ui.working') {
  if (app.busy) return;
  formBusy(true, status);
  try { await action(); }
  catch (error) { showError(error); }
  finally { formBusy(false); }
}

let nativeImport = false;
const importBlocked = () => app.editing ? 'error.finish_edit' :
  (app.busy || document.querySelector('dialog[open]') || !app.started) ? 'error.form_busy' : null;
window.beginFileImport = () => {
  const reason = importBlocked();
  if (reason) { if (!nativeImport) app.fileDrop.settled(); showError(reason); return false; }
  nativeImport = true;
  page('add'); $('error').hidden = true;
  formBusy(true, 'ui.importing');
  return true;
};
window.finishFileImport = result => {
  if (!nativeImport) return;
  try {
    if (result.failure && result.path != null) applyMedia({import_error:result.failure, path:result.path});
    else if (result.failure) showError(result.failure);
    else if (result.batch) app.batch.receive(result.batch);
    else if (result.media) applyMedia(result.media);
  } finally { nativeImport = false; formBusy(false); }
};

async function editJob(id) {
  if (app.editing || app.busy) throw new Error('error.finish_edit');
  await run(async () => {
    app.editing = await call('begin_edit', id);
    app.presets.clear();
    applyMedia({...app.editing.media, path: app.editing.source, name: app.editing.name});
    const output = app.editing.output, split = Math.max(output.lastIndexOf('\\'), output.lastIndexOf('/'));
    $('folder').value = output.slice(0, split);
    $('filename').value = output.slice(split + 1);
    $('outputFormat').value = output.toLowerCase().endsWith('.mp4') ? 'mp4' : 'mkv';
    updateOutputWarning();
    $('colorMode').value = app.editing.color_mode;
    $('adaptiveProfile').value = app.editing.adaptive_profile || 'normal';
    $('upscaleScale').value = app.editing.upscale.scale;
    $('denoiseEnabled').checked = app.editing.denoise.enabled;
    $('denoiseStrength').value = app.editing.denoise.strength;
    $('gpu').value = app.editing.gpu_id;
    await app.color.load(app.editing);
    $('editNotice').hidden = false;
    updateColor(); updateDenoise(); translate(); await refresh(true);
  });
}

function resetEditor() {
  app.editing = null; $('editNotice').hidden = true;
  $('browse').disabled = false;
  translate();
}
$('discardEdit').onclick = () => run(async () => {
  await call('end_edit', app.editing.id, app.editing.edit_token);
  app.compare.invalidate(); app.color.invalidate();
  resetEditor(); await refresh(true); page('queue');
});

function jobDetails(job) {
  app.detailJob = job;
  $('detailTitle').textContent = job.name;
  const list = $('detailBody'); list.replaceChildren();
  const add = (key, value) => { list.append(node('dt', '', t(key)), node('dd', '', value == null ? '—' : String(value))); };
  add('ui.source', job.source); add('ui.output', job.output);
  add('ui.job_status', t('status.' + job.status));
  add('ui.upscale', job.upscale.scale + '×'); add('ui.denoise', denoiseLabel(job));
  add('ui.color', t('color.' + job.color_mode));
  if (job.color_mode === 'adaptive') add('ui.adaptive_profile', t('adaptive.' + (job.adaptive_profile || 'normal')));
  if (job.preset) add('ui.presets', job.preset.name);
  add('ui.gpu', job.runtime.gpus.find(g => g.id === job.gpu_id)?.name || job.gpu_id);
  add('ui.model_version', job.model ? job.model.name + ' · SHA-256 ' + job.model.version : t('ui.legacy_snapshot'));
  for (const key of ['contrast', 'brightness', 'saturation']) add('ui.' + key, job.grade[key]);
  add('metric.elapsed', clock(job.elapsed));
  if (job.resume_expires) add('ui.resume_expires', new Date(job.resume_expires * 1000).toLocaleString(app.snapshot.settings.language));
  if (job.resume_deleted) add('ui.retention', t(job.status === 'paused' ? 'error.resume_deleted' : 'ui.temp_cleaned'));
  if (job.error) add('ui.error_details', job.failure?.detail || job.error);
  if (job.preflight) add('ui.disk_estimate', job.preflight.volumes.map(v => v.paths.join('\n') + '\n' + t('ui.disk_values', {free:(v.free / 1024 ** 3).toFixed(1), estimate:(v.estimated / 1024 ** 3).toFixed(1)})).join('\n\n'));
  UI.showDialog($('details'));
}

function buildJob(job) {
  const item = node('article', 'job' + (job.locked ? ' active' : ''));
  item.dataset.id = job.id;
  item.tabIndex = -1;
  item.setAttribute('aria-label', job.name + ' · ' + t('status.' + job.status));
  const title = node('div', 'jobTitle');
  title.append(node('strong', '', job.name), node('span', 'badge ' + job.status, t('status.' + job.status)));
  item.append(title, node('p', 'jobPath', job.media.width + ' × ' + job.media.height + ' → ' + job.media.width * job.upscale.scale + ' × ' + job.media.height * job.upscale.scale));
  item.append(node('p', 'jobPath', t('ui.upscale') + ': ' + job.upscale.scale + '× · ' + t('ui.denoise') + ': ' + denoiseLabel(job) + ' · ' + t('ui.color') + ': ' + UI.colorLabel(job, t)));
  item.append(node('p', 'jobPath', job.output));
  const stage = node('p', 'jobPath jobStage'); item.append(stage);
  const progress = node('progress'); progress.setAttribute('aria-label', t('metric.progress')); item.append(progress);
  item.append(node('div', 'metrics'));
  if (job.error) {
    item.append(node('p', 'jobError', t(job.failure?.code || (job.error.startsWith('error.') ? job.error : 'error.operation'))));
    const details = node('details');
    details.append(node('summary', '', t('ui.error_details')), node('pre', 'technicalDetails', job.failure?.detail || job.error));
    item.append(details);
    const recovery = node('div', 'actions');
    UI.recoveryActions(recovery, job.failure || {code:job.error}, t, (kind, control) => recoverFailure(kind, control, job));
    item.append(recovery);
  }
  if (job.warning) item.append(node('p', 'warning', t(job.warning)));
  if (job.resume_deleted) item.append(node('p', job.status === 'paused' ? 'warning' : 'muted', t(job.status === 'paused' ? 'error.resume_deleted' : 'ui.temp_cleaned')));
  if (job.resume_expires) item.append(node('p', 'jobPath', t('ui.resume_expires') + ': ' + new Date(job.resume_expires * 1000).toLocaleString(app.snapshot.settings.language)));
  const actions = node('div', 'jobActions');
  const add = (key, handler, danger=false) => {
    const button = UI.button(key, t, control => perform(handler, control), {danger, compact:true, disabled:app.pendingActions.has(job.id)});
    actions.append(button);
  };
  const available = new Set(job.actions);
  if (job.locked) {
    for (const key of ['ui.edit_job', 'ui.open_preview']) {
      const control = UI.button(key, t, () => {}, {compact:true, disabled:true});
      control.title = t('ui.active_job_lock');
      actions.append(control);
    }
    item.append(node('p', 'muted', t('ui.active_job_lock')));
  }
  if (available.has('edit')) add('ui.edit_job', () => editJob(job.id));
  if (available.has('preview')) add('ui.open_preview', async () => {
    if (['waiting','preparing','rendering'].includes(app.snapshot.preview?.status)) throw new Error('error.preview_busy');
    await editJob(job.id);
    if (app.editing?.id === job.id) app.compare.open(true);
  });
  for (const action of ['pause', 'resume', 'retry']) if (available.has(action)) add('ui.' + action, () => call('job_action', job.id, action));
  if (available.has('cancel')) add('ui.cancel', async () => {
    if (await confirmAction('ui.cancel', t('confirm.cancel', {name:job.name}))) await call('job_action', job.id, 'cancel', true);
  }, true);
  if (available.has('open_output')) add('ui.open_output', () => call('open_output', job.id));
  if (available.has('reorder')) {
    for (const [key, direction] of [['ui.move_up', -1], ['ui.move_down', 1]]) add(key, async () => {
      await call('move_job', job.id, direction);
      UI.message($('announcement'), t, 'ui.reordered', {name:job.name});
    });
  }
  if (available.has('remove')) add('ui.remove', async () => {
    if (await confirmAction('ui.remove', t('confirm.remove', {name:job.name}))) await call('remove_jobs', terminal.includes(job.status) ? 'history' : 'queue', job.id, true);
  }, true);
  add('ui.details', () => jobDetails(latest(job.id)));
  if (available.has('cleanup_resume')) add('ui.cleanup_resume', async () => {
    if (await confirmAction('ui.cleanup_resume', t('confirm.cleanup', {name:job.name}))) await call('cleanup_resume', job.id, true);
  }, true);
  item.append(actions);
  item.draggable = available.has('reorder') && !app.pendingActions.has(job.id);
  item.addEventListener('dragstart', event => {
    if (!item.draggable || event.target.closest('button')) { event.preventDefault(); return; }
    app.dragged = job.id;
    event.dataTransfer.setData('text/plain', job.id);
    event.dataTransfer.effectAllowed = 'move';
    item.classList.add('dragging');
  });
  item.addEventListener('dragend', () => { app.dragged = null; item.classList.remove('dragging'); renderJobs(); });
  item.addEventListener('dragover', event => { if (app.dragged && item.draggable) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } });
  item.addEventListener('drop', event => {
    if (!app.dragged || !item.draggable) return;
    event.preventDefault(); event.stopPropagation();
    const ids = app.snapshot.jobs.filter(j => j.status === 'queued' && !j.locked).map(j => j.id);
    const moved = app.dragged; app.dragged = null;
    if (!ids.includes(moved) || moved === job.id) return;
    ids.splice(ids.indexOf(moved), 1); ids.splice(ids.indexOf(job.id), 0, moved);
    perform(() => call('reorder', ids));
  });
  return item;
}

function updateMetrics(item, job) {
  const preparing = job.locked && job.stage === 'engine_preparing';
  item.querySelector('.jobStage').textContent = job.locked ?
    t(preparing && job.progress?.stage ? 'wait.' + job.progress.stage : 'stage.' + job.stage) : '';
  const progress = item.querySelector('progress');
  const unknown = job.locked && !job.total_frames;
  window.BorasukiWait.render(progress, preparing ? job.progress : unknown ?
    {elapsed:job.elapsed, updated_at:Date.now() / 1000} : null, preparing || unknown, t);
  if (!preparing && !unknown) {
    progress.hidden = !app.snapshot.settings.metrics.progress || !job.locked;
    progress.max = job.total_frames || 1;
    if (job.total_frames) progress.value = job.frames;
    else progress.removeAttribute('value');
  }
  const metrics = item.querySelector('.metrics'); metrics.replaceChildren();
  if (!job.locked) return;
  const values = {progress:job.total_frames ? (100 * job.frames / job.total_frames).toFixed(1) + '%' : '—',
    eta:job.eta != null && job.eta_provisional ? t('metric.eta_initial', {time:clock(job.eta)}) : clock(job.eta), speed:job.speed == null ? '—' : job.speed.toFixed(2), elapsed:clock(job.elapsed),
    frames:job.frames + ' / ' + (job.total_frames || '—'),
    vram:job.vram ? (job.vram.used / 1024).toFixed(1) + ' / ' + (job.vram.total / 1024).toFixed(1) + ' GB' : '—'};
  for (const key of metricKeys) if (app.snapshot.settings.metrics[key]) {
    const metric = node('span', 'metric'); metric.append(node('small', '', t('metric.' + key)), node('span', '', values[key])); metrics.append(metric);
  }
}

function reconcileList(root, jobs) {
  if (!jobs.length) {
    if (root.firstElementChild?.dataset.empty === app.snapshot.settings.language) return;
    const empty = node('div', 'emptyState');
    empty.dataset.empty = app.snapshot.settings.language;
    empty.append(node('p', '', t(root.id === 'queue' ? 'ui.empty_queue' : 'ui.empty_history')));
    if (root.id === 'queue') empty.append(UI.button('ui.add_more', t, () => page('add')));
    root.replaceChildren(empty);
    return;
  }
  const ids = new Set(jobs.map(job => job.id));
  [...root.children].forEach(item => { if (!ids.has(item.dataset.id)) item.remove(); });
  jobs.forEach((job, index) => {
    let item = [...root.children].find(el => el.dataset.id === job.id);
    const signature = JSON.stringify([job.actions,job.status,job.locked,job.revision,job.error,job.failure,job.warning,job.resume_deleted,job.resume_expires,app.snapshot.settings.language,app.pendingActions.has(job.id)]);
    if (!item || item.dataset.signature !== signature) {
      const next = buildJob(job); next.dataset.signature = signature;
      if (item) item.replaceWith(next); else root.append(next);
      item = next;
    }
    if (root.children[index] !== item) root.insertBefore(item, root.children[index] || null);
    updateMetrics(item, job);
  });
}

function renderList(root, jobs) {
  UI.preserveFocus(root, () => reconcileList(root, jobs));
}

function renderJobs() {
  if (!app.snapshot || app.dragged) return;
  const queue = app.snapshot.jobs.filter(job => !terminal.includes(job.status));
  const history = app.snapshot.jobs.filter(job => terminal.includes(job.status) && ($('historyFilter').value === 'all' || $('historyFilter').value === job.status)).sort((a,b) => (b.finished || b.created) - (a.finished || a.created));
  renderList($('queue'), queue); renderList($('history'), history);
  const waiting = queue.filter(job => job.actions.includes('reorder'));
  for (const [index, job] of waiting.entries()) {
    const row = [...$('queue').children].find(item => item.dataset.id === job.id);
    row.querySelector('[data-action="ui.move_up"]').disabled = index === 0 || app.pendingActions.has(job.id);
    row.querySelector('[data-action="ui.move_down"]').disabled = index === waiting.length - 1 || app.pendingActions.has(job.id);
  }
  $('queueCount').textContent = t('ui.queue_count', {count:queue.length});
  $('queueToggle').textContent = t(app.snapshot.settings.queue_enabled ? 'ui.hold_queue' : 'ui.start_queue');
  $('queueState').textContent = t(app.snapshot.settings.queue_enabled ? 'ui.queue_running' : 'ui.queue_held');
}
$('historyFilter').onchange = renderJobs;

async function refresh(fresh = false) {
  if (app.refreshing) { await app.refreshing; return fresh ? refresh() : undefined; }
  app.refreshing = (async () => {
    const language = app.snapshot?.settings.language;
    app.snapshot = await call('state'); app.catalog = app.snapshot.catalog;
    const languageChanged = language && language !== app.snapshot.settings.language;
    if (!app.started) {
      $('folder').value = app.snapshot.settings.output_folder;
      for (const id of ['gpu','default_gpu']) {
        app.snapshot.runtime.gpus.forEach(gpu => { const option = node('option', '', gpu.name + ' · ' + Math.round(gpu.total / 1024) + ' GB'); option.value = gpu.id; $(id).append(option); });
        if (app.snapshot.settings.default_gpu != null) $(id).value = app.snapshot.settings.default_gpu;
      }
      metricKeys.forEach(key => {
        const label = node('label','check'), input = node('input'), text = node('span');
        input.type = 'checkbox'; input.id = 'metric-' + key; text.dataset.i18n = 'metric.' + key;
        input.onchange = () => saveSettings({metrics:Object.fromEntries(metricKeys.map(name => [name,$('metric-' + name).checked]))});
        label.append(input,text); $('metricSettings').append(label);
      });
      app.started = true;
      app.presets.refresh().catch(showError);
    }
    if (!app.settingsBusy) {
      for (const id of ['language','theme']) $(id).value = app.snapshot.settings[id];
      for (const id of ['sound','notifications','context_menu']) $(id).checked = app.snapshot.settings[id];
      metricKeys.forEach(key => { $('metric-' + key).checked = app.snapshot.settings.metrics[key]; });
      $('resume_retention').value = app.snapshot.settings.resume_retention === 30 && !app.snapshot.settings.retention_consent ? 'pending' : String(app.snapshot.settings.resume_retention);
      if (app.snapshot.settings.default_gpu != null) $('default_gpu').value = app.snapshot.settings.default_gpu;
    }
    document.body.classList.toggle('light', app.snapshot.settings.theme === 'light');
    translate(); updateColor(); updateDenoise();
    if (languageChanged) {
      if (app.storage) renderStorage(app.storage);
      if ($('details').open && app.detailJob) jobDetails(latest(app.detailJob.id) || app.detailJob);
    }
    $('runtime').textContent = app.snapshot.runtime.ready ? t('ui.runtime_ready') + ' · ' + t('ui.runtime_existing') : t('ui.runtime_missing') + ': ' + app.snapshot.runtime.missing.join(', ');
    renderJobs();
    app.color?.render(app.snapshot);
    renderSetup();
    app.preparation?.render(app.snapshot);
    syncJobActions();
    app.compare?.render(app.snapshot);
  })().finally(() => { app.refreshing = null; });
  return app.refreshing;
}

function renderSetup() {
  app.batch?.render(app.snapshot.batch);
  const setup = app.snapshot.setup;
  const checking = ['waiting', 'checking'].includes(setup.status);
  $('setupGate').hidden = setup.status === 'ready';
  window.BorasukiWait.render($('setupProgress'), setup.progress, checking, t);
  $('setupFailure').hidden = setup.status !== 'failed' || !setup.failure?.detail;
  $('setupFailureDetail').textContent = setup.status === 'failed' ? setup.failure?.detail || '' : '';
  $('setupStatus').textContent = setup.error ? t(setup.error) :
    t(checking ? 'setup.' + (setup.stage || 'files') : 'setup.' + setup.status);
  $('setupPackage').hidden = !checking || setup.stage !== 'download' || !setup.package;
  if (setup.package) $('setupPackage').textContent = t('setup.package', {name:setup.package});
  $('setupCheck').textContent = t(app.snapshot.runtime.ready ? 'ui.setup_check' : 'ui.setup_install');
  $('setupCheck').disabled = checking || app.snapshot.jobs.some(job => job.locked) ||
    ['waiting', 'preparing', 'rendering'].includes(app.snapshot.preview?.status) ||
    ['waiting', 'preparing', 'stopping'].includes(app.snapshot.batch?.status);
  syncJobActions();
}
$('setupOpen').onclick = () => { page('settings'); $('setupCheck').focus(); };
$('setupCheck').onclick = () => perform(() => call('check_setup'), $('setupCheck'));
$('exportDiagnostics').onclick = () => perform(async () => {
  UI.message($('diagnosticsStatus'), t, '');
  const path = await call('export_diagnostics');
  if (path) UI.message($('diagnosticsStatus'), t, 'ui.diagnostics_saved', {path});
}, $('exportDiagnostics'));

async function refreshStorage() {
  const storage = await call('storage_usage');
  app.storage = storage;
  renderStorage(storage);
}

function renderStorage(storage) {
  UI.preserveFocus($('storageList'), () => {
  $('storageStatus').textContent = t('ui.storage_total', {size:(storage.bytes / 1024 ** 2).toFixed(1)}) +
    (storage.incomplete ? ' ' + t('error.storage_scan') : '');
  const rows = storage.jobs.filter(job => job.bytes !== 0);
  $('storageList').replaceChildren();
  for (const job of rows) {
    const row = node('article', 'job');
    row.dataset.id = job.id;
    row.append(node('strong', '', job.name), node('p', '', t('status.' + job.status)),
      node('p', '', job.bytes == null ? t('error.storage_scan') : (job.bytes / 1024 ** 2).toFixed(1) + ' MB'));
    if (job.cleanable) row.append(UI.button('ui.cleanup_temp', t, control => perform(async () => {
      if (await confirmAction('ui.cleanup_temp', t('confirm.cleanup_temp', {name:job.name}))) {
        await call('cleanup_job_files', job.id, true);
        await refreshStorage();
      }
    }, control), {danger:true, compact:true}));
    $('storageList').append(row);
  }
  for (const item of storage.extras || []) {
    if (item.bytes === 0) continue;
    const row = node('article', 'job');
    row.dataset.id = item.id;
    row.dataset.storageId = item.id;
    row.append(node('strong', '', t('storage.' + item.kind)), node('p', '', item.bytes == null ? t('error.storage_scan') : (item.bytes / 1024 ** 2).toFixed(1) + ' MB'));
    if (item.name) row.append(node('p', '', item.name));
    if (item.location) row.append(node('p', 'path', item.location));
    if (item.error) row.append(node('p', 'warning', t(item.error)));
    else if (!item.cleanable) row.append(node('p', 'muted', t('ui.temp_protected')));
    if (item.cleanable) row.append(UI.button('ui.cleanup_temp', t, control => perform(async () => {
      if (await confirmAction('ui.cleanup_temp', t('confirm.cleanup_extra', {name:t('storage.' + item.kind) + ' — ' + item.location}))) {
        await call('cleanup_extra_files', item.id, true);
        await refreshStorage();
      }
    }, control), {danger:true, compact:true}));
    $('storageList').append(row);
  }
  });
}
$('storageRefresh').onclick = () => perform(refreshStorage, $('storageRefresh'));

async function saveSettings(values, confirmed=false) {
  app.settingsPending += 1;
  app.settingsBusy = true;
  app.settingsWrites = app.settingsWrites.then(async () => {
    try { await call('settings', values, confirmed); }
    catch (error) { showError(error); }
    finally {
      app.settingsBusy = --app.settingsPending > 0;
      try { await refresh(true); } catch (error) { showError(error); }
    }
  });
  return app.settingsWrites;
}
for (const id of ['language','theme','sound','notifications','context_menu','default_gpu']) $(id).onchange = () => saveSettings({[id]:$(id).type === 'checkbox' ? $(id).checked : id === 'default_gpu' ? Number($(id).value) : $(id).value});
$('resume_retention').onchange = async () => {
  const value = $('resume_retention').value === '30' ? 30 : 'never';
  if (value === 30 && !app.snapshot.settings.retention_consent && !await confirmAction('ui.retention', t('confirm.retention'))) { await refresh(); return; }
  await saveSettings({resume_retention:value}, value === 30);
};
$('queueToggle').onclick = () => perform(() => call('settings', {queue_enabled:!app.snapshot.settings.queue_enabled}), $('queueToggle'));
$('clearQueue').onclick = () => perform(async () => {
  if (await confirmAction('ui.clear_queue', t('confirm.clear_queue'))) await call('remove_jobs', 'queue', null, true);
}, $('clearQueue'));
$('clearHistory').onclick = () => perform(async () => {
  if (await confirmAction('ui.clear_history', t('confirm.clear_history'))) await call('remove_jobs', 'history', null, true);
}, $('clearHistory'));
$('addMore').onclick = () => {
  if (app.editing) { showError('error.finish_edit'); page('add'); return; }
  page('add');
};
$('browse').onclick = () => { if (!app.editing) return run(chooseVideos, 'ui.importing'); };
$('folderPick').onclick = () => run(async () => { const folder = await call('choose_folder'); if (folder) { $('folder').value = folder; app.compare.invalidate(); if (!app.editing) await call('settings', {output_folder:folder}); } });
$('logs').onclick = () => perform(() => call('open_logs'));
$('errorLogs').onclick = () => perform(() => call('open_logs'), $('errorLogs'));
$('errorDismiss').onclick = () => { $('error').hidden = true; document.querySelector('.page:not([hidden]) h1').focus(); };
function jobValues() {
  if (!app.media) throw new Error('error.no_video');
  const filename = $('filename').value.trim();
  if (!filename || /[\\/]/.test(filename)) throw new Error('error.invalid_output');
  return {source:app.media.path, output:$('folder').value.replace(/[\\/]+$/, '') + '\\' + filename,
    drop_tracks_confirmed:mp4Consent === outputConsentKey(),
    color_mode:$('colorMode').value, upscale:{scale:Number($('upscaleScale').value)},
    denoise:{enabled:$('denoiseEnabled').checked, strength:Number($('denoiseStrength').value)},
    gpu_id:Number($('gpu').value), preset:app.presets.id(), adaptive:app.color.payload(), custom:{
      contrast:1 + Number($('contrast').value) / 100, brightness:Number($('brightness').value) / 100, saturation:1 + Number($('saturation').value) / 100}};
}
let mp4Consent = null;
function outputExtension() { return $('filename').value.trim().toLowerCase().split('.').pop(); }
function outputConsentKey() { return JSON.stringify([app.media?.path, $('folder').value, $('filename').value.trim(), app.media?.streams]); }
function outputDropsTracks() {
  return outputExtension() === 'mp4' && app.media?.streams?.some(kind => kind === 'subtitle' || kind === 'attachment');
}
function updateOutputWarning() { $('outputTrackWarning').hidden = !outputDropsTracks(); }
async function confirmOutputTracks() {
  if (!outputDropsTracks() || mp4Consent === outputConsentKey()) return true;
  const confirmed = await UI.confirm($('confirmation'), {
    title:t('ui.output_format'), body:t('confirm.mp4_tracks'), accept:t('ui.confirm'), safe:t('ui.cancel_dialog')
  });
  if (confirmed) mp4Consent = outputConsentKey();
  return confirmed;
}
app.compare = window.BorasukiCompare({call, translate:t, values:jobValues, source:() => app.media?.path, metadata:() => app.media, edit:() => app.editing,
  navigate:page, error:showError, refresh, confirmOutput:confirmOutputTracks, committed:source => {
    if (!app.editing && source) app.batch?.markAdded(source);
    resetEditor();
  }, recover:recoverFailure});
app.color = window.BorasukiColor({call, translate:t, source:() => app.media?.path, editing:() => app.editing,
  changed:() => app.compare.invalidate(), error:showError, refresh,
  recover:kind => { if (kind === 'setup') page('settings'); else $('browse').click(); }});
function processingRecipe(batch = false) {
  return {upscale:{scale:Number($('upscaleScale').value)},
    denoise:{enabled:$('denoiseEnabled').checked, strength:Number($('denoiseStrength').value)},
    color_mode:$('colorMode').value, adaptive_profile:$('adaptiveProfile').value,
    color_overrides:batch ? {...(app.color.ready() ? app.color.state.overrides : app.color.state.pendingOverrides)} : app.color.payload()?.overrides || {},
    custom:{contrast:1 + Number($('contrast').value)/100, brightness:Number($('brightness').value)/100, saturation:1 + Number($('saturation').value)/100}};
}
app.presets = window.BorasukiPresets({call, translate:t, run, confirm:confirmAction,
  changed:() => app.compare.invalidate(), values:processingRecipe,
  apply:async values => {
    $('upscaleScale').value = values.upscale.scale;
    $('denoiseEnabled').checked = values.denoise.enabled; $('denoiseStrength').value = values.denoise.strength;
    $('colorMode').value = values.color_mode; $('adaptiveProfile').value = values.adaptive_profile || 'normal'; app.color.invalidate(); updateDenoise(); updateColor();
    if (values.color_mode === 'custom') await app.color.load({grade:values.custom, color_mode:'custom'});
    if (values.color_mode === 'adaptive') await app.color.request(false, values.color_overrides);
  }});
app.batch = window.BorasukiBatch({call, translate:t, run, open:applyMedia, refresh, error:showError,
  confirm:confirmAction, confirmOutput:format => format !== 'mp4' || UI.confirm($('confirmation'), {
    title:t('ui.output_format'), body:t('confirm.mp4_batch'), accept:t('ui.confirm'), safe:t('ui.cancel_dialog')
  }), recover:recoverFailure, locked:() => Boolean(app.busy || app.editing),
  ready:() => app.snapshot?.setup.status === 'ready' && app.snapshot?.runtime.ready &&
    app.color.ready() && app.preparation?.ready(),
  values:() => ({folder:$('folder').value, recipe:processingRecipe(true), gpu:Number($('gpu').value),
    preset:app.presets.id(), format:$('outputFormat').value})});
app.preparation = window.BorasukiPreparation({call, translate:t, error:showError,
  values:() => app.media ? {source:app.media.path, gpu_id:Number($('gpu').value),
    upscale:{scale:Number($('upscaleScale').value)},
    denoise:{enabled:$('denoiseEnabled').checked, strength:Number($('denoiseStrength').value)}} : null});
app.fileDrop = window.BorasukiFileDrop({blocked:importBlocked, error:showError, translate:t});
for (const name of ['input', 'change']) $('jobForm').addEventListener(name, event => {
  if (event.target.closest('.processingGroup')) app.presets.clear();
  if (app.snapshot) app.preparation.render(app.snapshot);
  syncJobActions();
  if (event.target === $('filename')) {
    if (['mp4','mkv'].includes(outputExtension())) $('outputFormat').value = outputExtension();
    updateOutputWarning();
  }
});
$('outputFormat').onchange = () => {
  const filename = $('filename').value.trim();
  $('filename').value = /\.[^./\\]+$/i.test(filename) ? filename.replace(/\.[^./\\]+$/i, '.' + $('outputFormat').value) : filename + '.' + $('outputFormat').value;
  updateOutputWarning();
};
$('jobForm').addEventListener('input', () => app.compare.invalidate());
$('jobForm').addEventListener('change', () => app.compare.invalidate());
$('start').onclick = async () => {
  if (!await confirmOutputTracks()) return;
  return run(async () => {
    const values = jobValues();
    if (app.editing) { await call('end_edit', app.editing.id, app.editing.edit_token, values); resetEditor(); }
    else {
      await call('create_job', values.source, values.output, values.color_mode, values.gpu_id, values.custom, values.upscale,
                 values.denoise, values.adaptive, values.preset, values.drop_tracks_confirmed);
      app.batch.markAdded(values.source);
    }
    app.compare.invalidate();
    await refresh(true); page('queue');
  });
};

async function poll() {
  try {
    await refresh();
    if (!app.connected) {
      if (app.snapshot?.setup.status === 'missing') { page('settings'); $('setupCheck').focus(); }
      await call('client_ready'); app.connected = true;
    }
  }
  catch (error) { showError(error); }
  setTimeout(poll, 1200);
}
window.addEventListener('pywebviewready', poll, {once:true});
