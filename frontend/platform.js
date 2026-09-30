/* Shared desktop interaction primitives. */
window.BorasukiUI = (() => {
  const dialogFocus = new WeakMap();
  const messageValues = new WeakMap();
  function localize(control, translate) {
    control.textContent = translate(control.dataset.i18n, messageValues.get(control) || {});
  }
  function message(control, translate, key, values = {}) {
    control.dataset.i18n = key;
    messageValues.set(control, values);
    localize(control, translate);
  }
  function element(tag, className = '', text) {
    const result = document.createElement(tag);
    result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function button(key, translate, action, {danger = false, compact = false, disabled = false} = {}) {
    const control = element('button', [compact && 'quiet', danger && 'danger'].filter(Boolean).join(' '), translate(key));
    control.type = 'button';
    control.dataset.action = key;
    control.disabled = disabled;
    control.onclick = () => action(control);
    return control;
  }

  function reveal(target) {
    target.tabIndex = -1;
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'nearest', behavior:'auto'});
    target.classList.add('attention');
    setTimeout(() => target.classList.remove('attention'), 1500);
  }

  function shell(canNavigate = () => true) {
    const routes = new Map([...document.querySelectorAll('nav [data-page]')].map(control => {
      const name = control.dataset.page;
      const view = document.getElementById('page-' + name);
      if (!view) throw new Error('Missing route: ' + name);
      const heading = view.querySelector('h1');
      heading.tabIndex = -1;
      return [name, {control, view, heading, focus: null, scroll: 0}];
    }));
    let current = null;
    function navigate(name, focus = true) {
      if (!routes.has(name) || name === current) return;
      if (!canNavigate(name)) return;
      closeHelp();
      const previous = routes.get(current);
      if (previous) {
        if (previous.view.contains(document.activeElement)) previous.focus = document.activeElement;
        previous.scroll = window.scrollY;
      }
      current = name;
      for (const [route, entry] of routes) {
        const selected = route === name;
        entry.view.hidden = !selected;
        entry.control.classList.toggle('selected', selected);
        if (selected) entry.control.setAttribute('aria-current', 'page');
        else entry.control.removeAttribute('aria-current');
      }
      const next = routes.get(name);
      if (focus) {
        const target = next.focus?.isConnected && !next.focus.disabled && next.focus.getClientRects().length ? next.focus : next.heading;
        target.focus({preventScroll: true});
        window.scrollTo(0, next.scroll);
      }
      document.dispatchEvent(new CustomEvent('borasuki:navigate', {detail:{page:name}}));
    }
    for (const [name, entry] of routes) entry.control.onclick = () => navigate(name);
    navigate('add', false);
    return {navigate, get current() { return current; }};
  }

  function closeHelp() {
    document.querySelectorAll('.infoPopover:popover-open').forEach(panel => panel.hidePopover());
  }

  function help(translate) {
    for (const content of document.querySelectorAll('[data-help-for]')) {
      const heading = document.getElementById(content.dataset.helpFor);
      let panel = document.getElementById(heading.id + '-help');
      if (!panel) {
        const row = element('div', 'sectionHeading');
        heading.before(row);
        row.append(heading);
        const trigger = element('button', 'infoButton', 'i');
        trigger.type = 'button';
        trigger.dataset.helpTitle = heading.dataset.i18n;
        panel = element('div', 'infoPopover');
        panel.id = heading.id + '-help';
        panel.popover = 'auto';
        panel.setAttribute('role', 'note');
        panel.setAttribute('aria-labelledby', heading.id);
        trigger.setAttribute('popovertarget', panel.id);
        const close = element('button', 'quiet');
        close.type = 'button';
        close.dataset.i18n = 'ui.close';
        close.setAttribute('popovertarget', panel.id);
        close.setAttribute('popovertargetaction', 'hide');
        close.onclick = () => trigger.focus({preventScroll:true});
        panel.append(close);
        row.append(trigger, panel);
        panel.addEventListener('toggle', () => {
          if (!panel.matches(':popover-open')) return;
          const anchor = trigger.getBoundingClientRect();
          const gap = parseFloat(getComputedStyle(panel).getPropertyValue('--space-2'));
          const left = Math.max(gap, Math.min(anchor.left, innerWidth - panel.offsetWidth - gap));
          const below = anchor.bottom + gap;
          const top = below + panel.offsetHeight <= innerHeight - gap ? below : Math.max(gap, anchor.top - panel.offsetHeight - gap);
          panel.style.left = left + 'px';
          panel.style.top = top + 'px';
        });
      }
      panel.insertBefore(content, panel.lastElementChild);
      content.removeAttribute('data-help-for');
    }
    document.querySelectorAll('[data-help-title]').forEach(control => {
      control.setAttribute('aria-label', translate('ui.help_for', {topic:translate(control.dataset.helpTitle)}));
    });
    document.querySelectorAll('.infoPopover [data-i18n]').forEach(control => {
      control.textContent = translate(control.dataset.i18n);
    });
  }
  window.addEventListener('resize', closeHelp);
  window.addEventListener('scroll', event => {
    if (!event.target.closest?.('.infoPopover')) closeHelp();
  }, true);

  function showDialog(dialog) {
    if (dialog.open) return false;
    const trigger = document.activeElement;
    const restore = () => {
      if (dialog.open) return;
      dialog.removeEventListener('close', restore);
      dialog.removeEventListener('submit', dismiss);
      dialog.removeEventListener('cancel', dismiss);
      dialogFocus.delete(dialog);
      if (trigger?.isConnected && !trigger.disabled && trigger.getClientRects().length) trigger.focus({preventScroll: true});
      else document.querySelector('.page:not([hidden]) h1')?.focus({preventScroll: true});
    };
    const dismiss = event => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      closeDialog(dialog, event.type === 'submit' ? event.submitter?.value || '' : 'cancel');
    };
    dialogFocus.set(dialog, restore);
    dialog.addEventListener('close', restore, {once: true});
    dialog.addEventListener('submit', dismiss);
    dialog.addEventListener('cancel', dismiss);
    dialog.showModal();
    return true;
  }

  function closeDialog(dialog, result = '') {
    dialog.close(result);
    dialogFocus.get(dialog)?.();
  }

  function confirm(dialog, {title, body, accept, safe}) {
    if (dialog.open) return Promise.resolve(false);
    dialog.querySelector('[data-dialog-title]').textContent = title;
    dialog.querySelector('[data-dialog-body]').textContent = body;
    dialog.querySelector('[value="confirm"]').textContent = accept;
    const cancel = dialog.querySelector('[value="cancel"]');
    cancel.textContent = safe;
    dialog.returnValue = 'cancel';
    return new Promise(resolve => {
      const finish = result => {
        dialog.removeEventListener('submit', onSubmit);
        dialog.removeEventListener('cancel', onCancel);
        dialog.removeEventListener('close', onClose);
        closeDialog(dialog, result ? 'confirm' : 'cancel');
        resolve(result);
      };
      const onSubmit = event => { event.preventDefault(); finish(event.submitter?.value === 'confirm'); };
      const onCancel = event => { event.preventDefault(); finish(false); };
      const onClose = () => { if (!dialog.open) finish(dialog.returnValue === 'confirm'); };
      dialog.addEventListener('submit', onSubmit);
      dialog.addEventListener('cancel', onCancel);
      dialog.addEventListener('close', onClose, {once: true});
      showDialog(dialog);
      cancel.focus();
    });
  }

  function preserveFocus(root, render) {
    const focused = document.activeElement;
    const row = focused?.closest('[data-id]');
    const identity = root.contains(focused) && row ? {id: row.dataset.id, action: focused.dataset.action} : null;
    render();
    if (!identity || focused.isConnected) return;
    const replacement = [...root.querySelectorAll('[data-id]')].find(item => item.dataset.id === identity.id);
    const action = replacement && [...replacement.querySelectorAll('[data-action]')].find(control => control.dataset.action === identity.action && !control.disabled);
    (action || replacement || root.closest('.page')?.querySelector('h1'))?.focus({preventScroll: true});
  }

  function busy(control, active) {
    if (!control) return;
    control.setAttribute('aria-busy', String(active));
    control.classList.toggle('is-busy', active);
    if ('disabled' in control) control.disabled = active;
  }

  function recoveryActions(root, failure, translate, action) {
    const keys = {source:'ui.choose_another_video', setup:'ui.setup_open', storage:'ui.temp_files', output:'ui.review_output', format:'ui.review_format', preparation:'ui.open_preparation'};
    const key = keys[failure?.recovery];
    const signature = JSON.stringify([failure?.code, failure?.recovery, key && translate(key), translate('ui.export_diagnostics')]);
    if (root.dataset.recovery === signature) return;
    root.dataset.recovery = signature;
    root.replaceChildren();
    root.hidden = !failure;
    if (!failure) return;
    if (key) root.append(button(key, translate, control => action(failure.recovery, control)));
    root.append(button('ui.export_diagnostics', translate, control => action('diagnostics', control)));
  }

  function colorLabel(job, translate) {
    const label = translate('color.' + job.color_mode);
    return job.color_mode === 'adaptive' ? label + ' — ' + translate('adaptive.' + (job.adaptive_profile || 'normal')) : label;
  }

  return {element, button, shell, help, message, localize, showDialog, closeDialog, confirm, preserveFocus, busy, recoveryActions, colorLabel, reveal};
})();
