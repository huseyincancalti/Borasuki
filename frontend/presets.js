/* Saved recipes contain no source-specific analysis, paths or GPU selection. */
window.BorasukiPresets = ({call, translate:t, run, confirm, apply, values, changed}) => {
  const el = id => document.getElementById(id);
  const state = {items:[], applied:null};
  const selected = () => state.items.find(item => item.id === el('presetSelect').value);

  function render() {
    el('presetApply').disabled = !selected();
    el('presetDelete').disabled = !selected();
    el('presetStatus').textContent = state.applied ? t('ui.preset_applied', {name:state.applied.name}) : '';
  }

  async function refresh() {
    const previous = el('presetSelect').value;
    state.items = await call('list_presets');
    const empty = document.createElement('option');
    empty.value = ''; empty.dataset.i18n = 'ui.preset_choose'; empty.textContent = t('ui.preset_choose');
    el('presetSelect').replaceChildren(empty);
    for (const item of state.items) {
      const option = document.createElement('option'); option.value = item.id; option.textContent = item.name;
      el('presetSelect').append(option);
    }
    el('presetSelect').value = state.items.some(item => item.id === previous) ? previous : '';
    render();
  }

  function clear() { state.applied = null; render(); }
  el('presetSelect').onchange = render;
  el('presetApply').onclick = () => run(async () => {
    const item = selected();
    if (!item) return;
    await apply(item.values);
    state.applied = item; changed(); render();
  });
  el('presetSave').onclick = () => run(async () => {
    const item = await call('save_preset', el('presetName').value, values());
    await refresh(); el('presetSelect').value = item.id; el('presetName').value = ''; render();
  });
  el('presetDelete').onclick = () => run(async () => {
    const item = selected();
    if (!item || !await confirm('ui.preset_delete', t('confirm.preset_delete', {name:item.name}))) return;
    await call('delete_preset', item.id, true);
    if (state.applied?.id === item.id) clear();
    await refresh();
  });
  return {state, refresh, render, clear, id:() => state.applied?.id || null,
    overrides:() => state.applied?.values.color_overrides || {}};
};
