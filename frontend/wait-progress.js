/* One measured wait display for all operation panels. */
window.BorasukiWait = (() => {
  const states = new WeakMap();
  const busyBars = new WeakMap();
  const activeBars = new Map();
  let timer = null;
  const clock = seconds => {
    const value = Math.max(0, Math.ceil(seconds));
    return Math.floor(value / 60) + ':' + String(value % 60).padStart(2, '0');
  };
  const size = bytes => (bytes >= 1e9 ? (bytes / 1e9).toFixed(2) + ' GB' : (bytes / 1e6).toFixed(1) + ' MB');
  function render(bar, progress, active, translate) {
    let state = states.get(bar);
    if (!state) {
      const caption = document.createElement('p');
      caption.className = 'waitMetrics muted';
      // A changing stopwatch should not repeatedly interrupt screen readers.
      caption.setAttribute('aria-live', 'off');
      bar.after(caption);
      state = {caption, started:Date.now()}; states.set(bar, state);
    }
    if (!active) {
      activeBars.delete(bar);
      bar.hidden = state.caption.hidden = true;
      state.started = Date.now();
      bar.removeAttribute('value');
      return;
    }
    activeBars.set(bar, {progress, translate});
    if (!timer) timer = setInterval(() => {
      for (const [control, value] of activeBars) {
        if (!control.isConnected) { activeBars.delete(control); continue; }
        render(control, value.progress, true, value.translate);
      }
      if (!activeBars.size) { clearInterval(timer); timer = null; }
    }, 1000);
    const delta = progress?.updated_at ? Math.max(0, Date.now() / 1000 - progress.updated_at) : 0;
    const elapsed = progress ? (progress.elapsed || 0) + delta : (Date.now() - state.started) / 1000;
    const known = Number.isFinite(progress?.completed) && progress?.total > 0;
    bar.hidden = !known && elapsed >= 10;
    bar.max = known ? progress.total : 1;
    if (known) bar.value = Math.min(progress.completed, progress.total);
    else bar.removeAttribute('value');
    state.caption.hidden = !known && elapsed < 10;
    const parts = [];
    if (known) {
      const values = {percent:Math.floor(100 * Math.min(progress.completed, progress.total) / progress.total),
        done:progress.unit === 'bytes' ? size(progress.completed) : progress.completed,
        total:progress.unit === 'bytes' ? size(progress.total) : progress.total};
      parts.push(translate(progress.unit === 'bytes' ? 'wait.download' : 'wait.percent', values));
    }
    if (elapsed >= 10) parts.push(translate('wait.elapsed', {time:clock(elapsed)}));
    if (!known && elapsed >= 10) parts.push(translate('wait.unmeasured'));
    if (progress?.eta != null && progress.eta > delta) {
      parts.push(translate('wait.eta', {time:clock(progress.eta - delta)}));
    } else if (elapsed >= 10) parts.push(translate('wait.eta_unknown'));
    state.caption.textContent = parts.join(' · ');
    state.caption.title = translate(progress?.eta_basis === 'history' ? 'wait.history_help' : 'wait.phase_help');
  }
  function busy(label, active, translate) {
    let bar = busyBars.get(label);
    if (!bar) {
      bar = document.createElement('progress');
      bar.setAttribute('aria-labelledby', label.id);
      label.after(bar); busyBars.set(label, bar);
    }
    render(bar, null, active, translate);
  }
  return {render, busy};
})();
