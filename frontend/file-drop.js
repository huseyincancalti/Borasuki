/* File gestures stay local; pywebview supplies native paths on drop. */
window.BorasukiFileDrop = ({blocked, error, translate:t}) => {
  const overlay = document.getElementById('fileDropOverlay');
  const status = document.getElementById('importStatus');
  let depth = 0, pending = null;
  const files = event => Array.from(event.dataTransfer?.types || []).includes('Files');
  function hide() {
    depth = 0;
    overlay.classList.remove('visible');
  }
  function settled() {
    clearTimeout(pending); pending = null;
    hide();
    status.hidden = true;
    window.BorasukiWait.render(document.getElementById('importProgress'), null, false, t);
  }
  function loading() {
    settled();
    status.hidden = false;
    window.BorasukiWait.render(document.getElementById('importProgress'), null, true, t);
  }
  document.addEventListener('dragenter', event => {
    if (!files(event)) return;
    event.preventDefault();
    if (blocked()) return;
    depth += 1;
    overlay.classList.add('visible');
  }, true);
  document.addEventListener('dragover', event => {
    if (!files(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = blocked() ? 'none' : 'copy';
  }, true);
  document.addEventListener('dragleave', event => {
    if (!files(event) && !depth) return;
    if (--depth <= 0) hide();
  }, true);
  document.addEventListener('drop', event => {
    if (!files(event)) return;
    event.preventDefault();
    hide();
    const reason = blocked();
    if (reason) {
      event.stopImmediatePropagation();
      error(reason);
      return;
    }
    loading();
    // Missing native bridge delivery must not leave an endless spinner.
    pending = setTimeout(() => { settled(); error('error.drop_path'); }, 5000);
  }, true);
  window.addEventListener('blur', hide);
  document.addEventListener('dragend', hide);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });
  return {loading, settled, waiting:() => pending !== null};
};
