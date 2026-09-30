/* Import drafts reuse the normal form and backend queue commands. */
window.BorasukiBatch = ({call, translate:t, run, open, values, locked, ready, refresh, error, confirm, confirmOutput = async () => true, recover}) => {
  const UI = window.BorasukiUI, el = id => document.getElementById(id);
  const state = {paths:[], failures:[], task:null, signature:null, token:null};
  const consumed = new Set();
  const busy = () => ['waiting','preparing','stopping'].includes(state.task?.status);
  const key = path => path.toLocaleLowerCase('en-US');
  const selected = () => el('batchSelect').value;

  function markAdded(path) {
    state.paths = state.paths.filter(item => key(item) !== key(path));
    render();
  }

  function receive(selection) {
    if (!selection) return;
    const paths = [...state.paths];
    for (const path of selection.paths) if (!paths.some(item => key(item) === key(path))) paths.push(path);
    if (paths.length > 200) { error('error.batch_limit'); return false; }
    state.paths = paths;
    state.failures = selection.failures;
    render();
    if (!selection.paths.length && !selection.failures.length) error('error.batch_empty');
    return true;
  }

  function render(task = state.task) {
    if (task?.id !== state.task?.id) consumed.clear();
    state.task = task;
    if (task) {
      for (const item of task.items) if (item.status === 'added' && !consumed.has(item.job_id)) {
        consumed.add(item.job_id);
        state.paths = state.paths.filter(path => key(path) !== key(item.path));
      }
    }
    const old = selected();
    const choices = JSON.stringify(state.paths);
    if (el('batchSelect').dataset.choices !== choices) {
      el('batchSelect').replaceChildren(...state.paths.map(path => { const option = UI.element('option', '', path); option.value = path; return option; }));
      if (state.paths.includes(old)) el('batchSelect').value = old;
      el('batchSelect').dataset.choices = choices;
    }
    el('batchSelection').hidden = !state.paths.length;
    el('batchPanel').hidden = !state.paths.length && !state.failures.length;
    el('batchFolder').disabled = locked();
    el('batchOpen').disabled = el('batchRemove').disabled = locked() || busy() || !state.paths.length;
    el('batchQueue').disabled = locked() || busy() || !ready() || !state.paths.length;
    el('batchReport').hidden = !task && !state.failures.length;
    el('batchCancel').hidden = !busy(); el('batchCancel').disabled = task?.status === 'stopping';
    const done = task?.items.filter(item => !['pending','preparing'].includes(item.status)).length || 0;
    if (task) {
      window.BorasukiWait.render(el('batchProgress'), {...task.progress, completed:done, total:task.items.length}, busy(), t);
      el('batchStatus').textContent = t('ui.batch_summary', {done, total:task.items.length}) + ' ' + t('batch.' + task.status);
    } else { el('batchStatus').textContent = t('ui.batch_import_issues'); window.BorasukiWait.render(el('batchProgress'), null, false, t); }
    const signature = JSON.stringify([task?.items, state.failures, t('ui.batch_title')]);
    if (state.signature === signature) return;
    state.signature = signature;
    const expanded = new Set([...el('batchResults').querySelectorAll('details[open]')].map(details => details.closest('[data-id]').dataset.id));
    UI.preserveFocus(el('batchResults'), () => {
    el('batchResults').replaceChildren();
    for (const item of [...state.failures.map(item => ({...item,status:'failed'})), ...(task?.items || [])]) {
      const row = UI.element('div', 'inset');
      row.dataset.id = item.job_id || item.path; row.tabIndex = -1;
      row.append(UI.element('p', 'path', item.path), UI.element('p', '', t('batch.' + item.status)));
      if (item.failure) {
        row.append(UI.element('p', 'warning', t(item.failure.code)));
        const details = UI.element('details');
        const summary = UI.element('summary', '', t('ui.error_details')); summary.dataset.action = 'details';
        details.open = expanded.has(row.dataset.id);
        details.append(summary, UI.element('pre', 'technicalDetails', item.failure.detail)); row.append(details);
        const actions = UI.element('div', 'actions'); UI.recoveryActions(actions, item.failure, t, recover); row.append(actions);
      }
      el('batchResults').append(row);
    }
    });
  }

  el('batchFolder').onclick = () => run(async () => {
    const selection = await call('choose_batch', true);
    if (!selection || !receive(selection) || !selection.paths.length) return;
    await open(await call('inspect_file', selection.paths[0]));
  }, 'ui.importing');
  el('batchOpen').onclick = () => run(async () => open(await call('inspect_file', selected())), 'ui.importing');
  el('batchRemove').onclick = () => { if (!locked() && !busy()) markAdded(selected()); };
  el('batchQueue').onclick = () => run(async () => {
    if (busy() || !state.paths.length || !ready()) return;
    const snapshot = values();
    if (!await confirmOutput(snapshot.format)) return;
    state.token = await call('request_batch', [...state.paths], snapshot.folder, snapshot.recipe, snapshot.gpu,
                             snapshot.preset, snapshot.format, snapshot.format === 'mp4');
    await refresh(true);
  });
  el('batchCancel').onclick = async () => {
    if (!busy() || !await confirm('ui.batch_cancel', t('confirm.batch_cancel'))) return;
    try { await call('cancel_batch', state.task.id); await refresh(true); } catch (problem) { error(problem); }
  };
  return {state, receive, render, markAdded};
};
