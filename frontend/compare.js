/* Frame-synchronized comparison; processing remains in Service. */
window.BorasukiCompare = ({call, translate: t, values, source: sourcePath, metadata, edit, navigate, error, refresh, committed, recover, confirmOutput = async () => true}) => {
  const el = id => document.getElementById(id);
  const state = {token:null, version:0, loaded:null, fetching:null, failedMedia:null, requesting:false, committing:false, targetTime:0, scale:1, x:0, y:0, drag:null, sourceUrl:null, sourceLoading:null, sourcePath:null, sourceStatus:null, pending:false, view:'source', playing:false, playTimer:null};
  const videos = [el('compareOriginal'), el('compareEnhanced')];
  const frames = [el('compareOriginalFrame'), el('compareEnhancedFrame')];
  const sourceVideo = el('sourceVideo');
  const viewport = el('compareViewport');
  const sourceFps = () => Number(metadata()?.fps) || Number(metadata()?.fps_num) / Number(metadata()?.fps_den) || 24;
  const sourceFrames = () => Math.max(1, Math.round((Number(metadata()?.duration) || sourceVideo.duration || 0) * sourceFps()));
  const currentFrame = () => Math.max(0, Math.min(sourceFrames() - 1, Math.floor(sourceVideo.currentTime * sourceFps() + .001)));

  const sourceClock = seconds => {
    const milliseconds = Math.round(Math.max(0, Number(seconds) || 0) * 1000);
    return String(Math.floor(milliseconds / 60000)).padStart(2, '0') + ':' + ((milliseconds % 60000) / 1000).toFixed(3).padStart(6, '0');
  };

  function sourcePosition(value = sourceVideo.currentTime, seek = false) {
    const seconds = Math.max(0, Number(value) || 0);
    el('sourceTime').textContent = sourceClock(seconds);
    el('sourceTimeline').value = Math.floor(seconds * sourceFps() + 1e-6);
    if (seek && sourceVideo.readyState >= 1 && Math.abs(sourceVideo.currentTime - seconds) > 1e-6) {
      sourceVideo.currentTime = seconds;
    }
  }

  function selectPosition() {
    if (state.view !== 'source' || state.pending || state.requesting || sourceVideo.readyState < 1 || sourceVideo.seeking) return;
    const seconds = currentFrame() / sourceFps();
    const value = String(seconds);
    sourcePosition(seconds);
    if (Number(el('previewStart').value) === Number(value)) return;
    el('previewStart').value = value;
    if (state.token) {
      const resume = !sourceVideo.paused;
      invalidate(true);
      if (resume) play();
    }
  }

  function loadSource(force = false) {
    const source = sourcePath();
    if (!source) {
      if (state.sourcePath) { sourceVideo.pause(); sourceVideo.removeAttribute('src'); sourceVideo.load(); }
      state.sourcePath = state.sourceUrl = state.sourceStatus = null;
      return;
    }
    if (state.sourceLoading === source || (!force && state.sourcePath === source)) return;
    if (state.sourcePath !== source) { el('previewStart').value = '0'; sourcePosition(0); }
    sourceVideo.pause();
    pause();
    state.sourceLoading = source;
    state.sourcePath = source;
    state.sourceStatus = 'ui.source_playback_loading';
    el('sourcePlaybackStatus').textContent = t(state.sourceStatus);
    el('sourceRetry').hidden = true;
    call('source_media', source).then(media => {
      if (source !== sourcePath()) return;
      state.sourceUrl = media.url;
      sourceVideo.src = media.url;
      sourceVideo.load();
    }).catch(problem => {
      if (source === sourcePath()) {
        state.sourceStatus = problem.failure?.code || problem.message;
        el('sourcePlaybackStatus').textContent = t(state.sourceStatus);
        el('sourceRetry').hidden = false;
        error(problem);
      }
    }).finally(() => { if (state.sourceLoading === source) state.sourceLoading = null; });
  }

  function transform() {
    const maxX = viewport.clientWidth * (state.scale - 1) / 2;
    const maxY = viewport.clientHeight * (state.scale - 1) / 2;
    state.x = Math.max(-maxX, Math.min(maxX, state.x));
    state.y = Math.max(-maxY, Math.min(maxY, state.y));
    for (const video of [...videos, ...frames, sourceVideo]) video.style.transform = `translate(${state.x}px,${state.y}px) scale(${state.scale})`;
    el('previewZoom').textContent = Math.round(state.scale * 100) + '%';
    el('previewZoomOut').disabled = state.scale <= 1;
    el('previewZoomIn').disabled = state.scale >= 8;
    viewport.style.cursor = state.scale > 1 ? 'grab' : 'default';
  }

  function pause() {
    clearTimeout(state.playTimer); state.playTimer = null; state.playing = false;
    sourceVideo.pause();
    el('previewPlay').textContent = t('ui.play');
  }

  function view(mode) {
    pause();
    state.view = mode === 'compare' && state.loaded && state.failedMedia !== state.token ? 'compare' : 'source';
    el('previewView').value = state.view;
    el('previewView').querySelector('[value="compare"]').disabled = !state.loaded || state.failedMedia === state.token;
    sourceVideo.hidden = state.view !== 'source';
    el('compareReady').hidden = state.view !== 'compare';
    el('sourceTransport').hidden = state.view !== 'source';
    el('compareTransport').hidden = el('compareSplitControl').hidden = state.view !== 'compare';
    el('sourceDuration').textContent = '/ ' + sourceClock(Number(metadata()?.duration) || sourceVideo.duration);
    if (state.view === 'source') sourcePosition();
  }

  function split(value) {
    value = Math.max(0, Math.min(100, value));
    el('compareSplit').value = value;
    el('compareBefore').style.clipPath = `inset(0 ${100 - value}% 0 0)`;
    el('compareLine').style.left = value + '%';
    el('compareLine').setAttribute('aria-valuenow', String(Math.round(value)));
    el('compareLine').setAttribute('aria-valuetext', t('ui.split_value', {value:Math.round(value)}));
  }

  function step(direction, oneFrame = false) {
    if (state.pending || state.requesting) return;
    pause();
    const fps = state.view === 'compare' ? state.loaded.fps : sourceFps();
    const amount = oneFrame || el('previewStep').value === 'frame' ? 1 : Math.max(1, Math.round(Number(el('previewStep').value) * fps));
    if (state.view === 'compare') {
      el('compareFrame').value = Math.max(0, Math.min(state.loaded.total_frames - 1, Number(el('compareFrame').value) + direction * amount));
      seek();
    } else {
      const frame = Math.max(0, Math.min(sourceFrames() - 1, Number(el('sourceTimeline').value) + direction * amount));
      selectSourceFrame(frame);
    }
  }

  function selectSourceFrame(frame) {
    if (state.pending || state.requesting || sourceVideo.readyState < 1) return;
    pause();
    const seconds = Math.max(0, Math.min(sourceFrames() - 1, frame)) / sourceFps();
    el('previewStart').value = String(seconds);
    sourcePosition(seconds, true);
    if (state.token) invalidate(true);
    refresh(true);
  }

  function play() {
    if (state.pending || state.requesting) return;
    if (state.playing || !sourceVideo.paused && state.view === 'source') { pause(); return; }
    if (state.view === 'source') {
      sourceVideo.play().catch(() => { pause(); error('error.source_playback'); });
    } else if (state.loaded.total_frames > 1) {
      state.playing = true;
      el('previewPlay').textContent = t('ui.pause');
      if (Number(el('compareFrame').value) >= state.loaded.total_frames - 1) el('compareFrame').value = 0;
      seek();
    }
  }

  function clearMedia() {
    pause();
    state.loaded = null;
    state.mediaReady = false; state.drawnFrame = null;
    for (const canvas of frames) canvas.width = canvas.height = 0;
    for (const video of videos) { video.pause(); video.removeAttribute('src'); video.load(); }
    el('compareReady').hidden = true;
    el('previewCommit').disabled = true;
    viewport.classList.remove('is-seeking'); viewport.setAttribute('aria-busy', 'false');
    view('source');
  }

  function invalidate(keepSaved = false) {
    if (!keepSaved) {
      state.saved = false;
      state.checkVersion = (state.checkVersion || 0) + 1;
      state.checking = false;
      state.checks = null;
      state.checkFailure = null;
      el('draftCheckStatus').textContent = '';
    }
    state.version += 1;
    const token = state.token;
    state.token = null;
    clearMedia();
    if (token) call('cancel_preview', token).catch(error);
  }

  function open(saved = false) {
    state.saved = saved;
    const alreadyOpen = !el('page-preview').hidden;
    navigate('preview');
    if (alreadyOpen) enter();
  }

  function enter() {
    loadSource();
    if (sourcePath() && !state.checking && !state.checks && !state.pending) checkDraft();
  }
  document.addEventListener('borasuki:navigate', event => {
    if (event.detail.page === 'preview') enter();
    else pause();
  });

  async function checkDraft() {
    if (!await confirmOutput()) return false;
    const version = state.checkVersion = (state.checkVersion || 0) + 1;
    state.checking = true; state.checks = null; state.checkFailure = null;
    el('draftCheckStatus').hidden = false;
    el('draftCheckStatus').textContent = t('ui.preflight_checking');
    el('previewRender').disabled = true; el('draftCheck').disabled = true;
    try {
      const editing = edit();
      const result = await call('check_draft', values(), editing ? {id:editing.id, token:editing.edit_token} : null, state.saved || false);
      if (version !== state.checkVersion) return false;
      state.checks = result;
      el('draftCheckStatus').textContent = t('ui.preflight_ready') + ' ' + result.volumes.map(volume => t('ui.disk_values', {
        free:(volume.free / 1024 ** 3).toFixed(1), estimate:(volume.estimated / 1024 ** 3).toFixed(1)
      })).join(' · ');
      return true;
    } catch (problem) {
      if (version === state.checkVersion) {
        state.checkFailure = problem.failure?.code || problem.message;
        el('draftCheckStatus').textContent = t(state.checkFailure);
        error(problem);
      }
      return false;
    } finally {
      if (version === state.checkVersion) { state.checking = false; el('draftCheck').disabled = false; await refresh(true); }
    }
  }

  async function request(saved = state.saved || false) {
    if (state.requesting) return;
    if (!el('previewStart').reportValidity()) return;
    if (state.checking) return;
    if (!state.checks && !await checkDraft()) return;
    sourceVideo.pause();
    selectPosition();
    state.requesting = true;
    const version = ++state.version;
    window.BorasukiUI.busy(el('previewRender'), true);
    try {
      const payload = values();
      const editing = edit();
      const duration = el('previewDuration').value === 'frame' ? 'frame' : Number(el('previewDuration').value);
      const token = await call('request_preview', payload, Number(el('previewStart').value), duration,
                               editing ? {id:editing.id, token:editing.edit_token} : null, saved);
      if (version !== state.version) { await call('cancel_preview', token); return; }
      state.token = token;
      state.failedMedia = null;
      clearMedia();
      state.scale = 1; state.x = 0; state.y = 0;
      navigate('preview');
      await refresh(true);
    } catch (problem) { error(problem); }
    finally { state.requesting = false; window.BorasukiUI.busy(el('previewRender'), false); }
  }

  function seek() {
    if (!state.loaded) return;
    const frame = Number(el('compareFrame').value);
    const time = (frame + .25) / state.loaded.fps;
    state.targetTime = time;
    state.seekStarted = performance.now();
    state.mediaReady = false;
    el('previewCommit').disabled = true;
    viewport.classList.add('is-seeking');
    viewport.setAttribute('aria-busy', 'true');
    for (const video of videos) if (video.readyState >= 1) video.currentTime = time;
    el('compareFrameValue').textContent = t('ui.preview_frame', {frame:frame + 1, total:state.loaded.total_frames});
    el('sourceTime').textContent = sourceClock((state.loaded.start_frame + frame) / state.loaded.fps);
    revealFrame();
  }
  function revealFrame() {
    if (!state.loaded) return;
    const frame = Number(el('compareFrame').value);
    if (videos.every(item => item.readyState >= 2 && !item.seeking && Math.floor(item.currentTime * state.loaded.fps + .001) === frame)) {
      if (state.drawnFrame !== frame) {
        frames.forEach((canvas, index) => {
          const video = videos[index];
          if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
            canvas.width = video.videoWidth; canvas.height = video.videoHeight;
          }
          canvas.getContext('2d').drawImage(video, 0, 0);
        });
        state.drawnFrame = frame;
      }
      state.mediaReady = true;
      el('previewCommit').disabled = state.loaded.id !== state.token || state.failedMedia === state.token || state.committing;
      viewport.classList.remove('is-seeking');
      viewport.setAttribute('aria-busy', 'false');
      if (state.playing && !state.playTimer) {
        if (frame >= state.loaded.total_frames - 1) { pause(); return; }
        state.playTimer = setTimeout(() => {
          state.playTimer = null;
          if (!state.playing || !state.loaded) return;
          el('compareFrame').value = frame + 1; seek();
        }, Math.max(0, 1000 / state.loaded.fps - (performance.now() - state.seekStarted)));
      }
    }
  }
  videos.forEach(video => {
    video.addEventListener('loadedmetadata', seek);
    for (const event of ['seeked', 'loadeddata', 'canplay']) video.addEventListener(event, revealFrame);
    video.addEventListener('error', () => {
      if (state.token && video.getAttribute('src')) { pause(); state.failedMedia = state.token; state.mediaReady = false; el('previewCommit').disabled = true; error('error.preview_decode'); }
    });
  });

  sourceVideo.addEventListener('loadedmetadata', () => {
    el('sourceTimeline').max = sourceFrames() - 1;
    sourcePosition(Number(el('previewStart').value), true);
    state.sourceStatus = null;
    el('sourcePlaybackStatus').textContent = '';
  });
  sourceVideo.addEventListener('play', () => { el('previewPlay').textContent = t('ui.pause'); });
  sourceVideo.addEventListener('pause', () => { if (!state.playing) el('previewPlay').textContent = t('ui.play'); });
  for (const event of ['timeupdate', 'seeked', 'pause']) sourceVideo.addEventListener(event, selectPosition);
  sourceVideo.addEventListener('error', () => {
    if (!sourceVideo.getAttribute('src')) return;
    state.sourceStatus = sourceVideo.error?.code === 4 ? 'error.source_codec' : 'error.source_playback';
    el('sourcePlaybackStatus').textContent = t(state.sourceStatus);
    el('sourceRetry').hidden = false;
  });
  el('sourceRetry').onclick = () => loadSource(true);

  function render(snapshot) {
    el('sourcePlaybackStatus').textContent = state.sourceStatus ? t(state.sourceStatus) : '';
    window.BorasukiWait.busy(el('sourcePlaybackStatus'), state.sourceStatus === 'ui.source_playback_loading', t);
    window.BorasukiWait.busy(el('draftCheckStatus'), state.checking, t);
    el('draftCheckStatus').hidden = !state.checking && !state.checkFailure;
    const diskText = checks => checks.volumes.map(volume => t('ui.disk_values', {
      free:(volume.free / 1024 ** 3).toFixed(1), estimate:(volume.estimated / 1024 ** 3).toFixed(1)
    })).join(' · ');
    el('draftCheckStatus').textContent = state.checking ? t('ui.preflight_checking') : state.checks ?
      t('ui.preflight_ready') + ' ' + diskText(state.checks) : state.checkFailure ? t(state.checkFailure) : '';
    if (state.loaded) {
      const config = state.loaded.configuration;
      el('previewSummary').textContent = config.upscale.scale + '× · ' + t('ui.denoise') + ': ' +
        (config.denoise.enabled ? t('ui.denoise_level', {level:config.denoise.strength}) : t('ui.off')) + ' · ' + window.BorasukiUI.colorLabel(config, t);
      el('previewDisk').textContent = diskText(state.loaded.preflight);
      el('compareFrameValue').textContent = t('ui.preview_frame', {frame:Number(el('compareFrame').value) + 1, total:state.loaded.total_frames});
    }
    const task = snapshot.preview;
    const ours = task && task.id === state.token;
    const status = ours ? task.status : 'empty';
    const displayLoading = status === 'ready' && state.failedMedia !== state.token && (!state.loaded || state.drawnFrame === null);
    el('previewStatus').hidden = !displayLoading && !['waiting','preparing','rendering','failed'].includes(status);
    el('previewStatus').textContent = t(displayLoading ? 'ui.preview_display_loading' : status === 'rendering' && task.stage ? 'stage.' + task.stage : 'preview.' + status);
    el('previewCancel').hidden = !ours || !['waiting','preparing','rendering'].includes(status);
    window.BorasukiWait.render(el('previewProgress'), displayLoading ? null : task?.progress, displayLoading || ours && ['waiting','preparing','rendering'].includes(status), t);
    if (ours && ['preparing','rendering'].includes(status) && task.progress?.stage) el('previewStatus').textContent = t('wait.' + task.progress.stage);
    const pending = task && ['waiting','preparing','rendering'].includes(task.status);
    state.pending = Boolean(pending);
    sourceVideo.controls = false;
    if (state.pending) sourceVideo.pause();
    el('openPreview').disabled = Boolean(pending) || !snapshot.runtime.ready || state.requesting || el('start').disabled;
    el('previewRender').disabled = Boolean(pending) || !snapshot.runtime.ready || state.requesting || state.checking || el('start').disabled;
    el('draftCheck').disabled = Boolean(pending) || state.checking || state.requesting;
    el('previewRange').disabled = Boolean(pending) || state.requesting;
    const canSeek = !state.pending && !state.requesting && (state.view === 'compare' ? Boolean(state.loaded && state.failedMedia !== state.token) : sourceVideo.readyState >= 1);
    for (const id of ['previewPrevious','previewNext','previewStep','sourceTimeline','compareFrame']) el(id).disabled = !canSeek;
    el('previewPlay').disabled = !canSeek || state.view === 'compare' && state.loaded.total_frames <= 1;
    el('previewPlay').textContent = t(state.playing || state.view === 'source' && !sourceVideo.paused ? 'ui.pause' : 'ui.play');
    el('previewPrevious').setAttribute('aria-label', t('ui.previous_position'));
    el('previewNext').setAttribute('aria-label', t('ui.next_position'));
    el('previewZoomIn').setAttribute('aria-label', t('ui.zoom_in'));
    el('previewZoomOut').setAttribute('aria-label', t('ui.zoom_out'));
    el('sourceTimeline').setAttribute('aria-label', t('ui.source_timeline'));
    el('compareFrame').setAttribute('aria-label', t('ui.preview_frame_label'));
    el('compareLine').setAttribute('aria-label', t('ui.compare_split'));
    split(Number(el('compareSplit').value));
    el('previewRender').classList.toggle('primary', status !== 'ready');
    el('previewFailure').hidden = status !== 'failed' || !task.failure?.detail;
    el('previewFailureDetail').textContent = status === 'failed' ? task.failure?.detail || '' : '';
    window.BorasukiUI.recoveryActions(el('previewRecovery'), status === 'failed' ? task.failure || {code:task.error} : null, t, recover);
    if (status === 'failed') el('previewStatus').textContent = t(task.failure?.code || task.error);
    el('previewCommit').disabled = status !== 'ready' || state.loaded?.id !== state.token || !state.mediaReady || state.failedMedia === state.token || state.committing;
    if (status !== 'ready' || state.loaded?.id === task.id || state.fetching === task.id || state.failedMedia === task.id) return;
    const token = task.id;
    state.fetching = token;
    call('preview_media', token).then(media => {
      if (token !== state.token) return;
      state.loaded = task;
      videos[0].src = media.original; videos[1].src = media.enhanced;
      el('compareFrame').max = task.total_frames - 1;
      el('compareFrame').value = '0';
      view('compare');
      render(snapshot);
      transform(); seek();
    }).catch(problem => {
      if (token === state.token) { state.failedMedia = token; error(problem); }
    }).finally(() => { if (state.fetching === token) state.fetching = null; });
  }

  el('openPreview').onclick = () => open();
  el('draftCheck').onclick = checkDraft;
  el('previewRender').onclick = () => request();
  el('previewStart').oninput = () => { sourcePosition(Number(el('previewStart').value), true); invalidate(true); refresh(true); };
  el('previewDuration').oninput = () => { invalidate(true); refresh(true); };
  el('previewBack').onclick = () => navigate('add');
  el('previewCancel').onclick = async () => {
    try { await call('cancel_preview', state.token); await refresh(true); } catch (problem) { error(problem); }
  };
  el('previewCommit').onclick = async () => {
    if (state.committing || !state.token || !state.loaded || !state.mediaReady || state.failedMedia) return;
    state.committing = true;
    const token = state.token;
    const source = state.loaded.configuration?.source;
    window.BorasukiUI.busy(el('previewCommit'), true);
    try { await call('commit_preview', token); state.token = null; clearMedia(); committed(source); await refresh(true); navigate('queue'); }
    catch (problem) { error(problem); }
    finally { state.committing = false; window.BorasukiUI.busy(el('previewCommit'), false); el('previewCommit').disabled = !state.token || !state.mediaReady || state.failedMedia === state.token; }
  };
  el('sourceTimeline').oninput = () => selectSourceFrame(Number(el('sourceTimeline').value));
  el('compareFrame').oninput = () => { pause(); seek(); };
  el('compareSplit').oninput = () => split(Number(el('compareSplit').value));
  el('previewPrevious').onclick = () => step(-1);
  el('previewNext').onclick = () => step(1);
  el('previewPlay').onclick = play;
  el('previewView').onchange = () => { view(el('previewView').value); if (state.view === 'compare') seek(); refresh(true); };
  el('previewZoomIn').onclick = () => { state.scale = Math.min(8,state.scale * 1.25); transform(); };
  el('previewZoomOut').onclick = () => { state.scale = Math.max(1,state.scale / 1.25); transform(); };
  el('previewFit').onclick = () => { state.scale = 1; state.x = state.y = 0; transform(); };
  el('compareLine').onkeydown = event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault(); event.stopPropagation();
    const delta = event.shiftKey ? 10 : 1;
    split(event.key === 'Home' ? 0 : event.key === 'End' ? 100 : Number(el('compareSplit').value) + (event.key === 'ArrowLeft' ? -delta : delta));
  };
  viewport.addEventListener('wheel', event => {
    event.preventDefault();
    const box = viewport.getBoundingClientRect(), previous = state.scale;
    state.scale = Math.max(1, Math.min(8, previous * Math.exp(-event.deltaY * .001)));
    const ratio = state.scale / previous;
    state.x = (event.clientX - box.left - box.width/2) * (1-ratio) + state.x * ratio;
    state.y = (event.clientY - box.top - box.height/2) * (1-ratio) + state.y * ratio;
    transform();
  }, {passive:false});
  viewport.onpointerdown = event => {
    if (event.button !== 0 || state.drag) return;
    const wipe = event.target.closest('#compareLine');
    if (!wipe && state.scale <= 1) return;
    event.preventDefault();
    state.drag = {mode:wipe ? 'split' : 'pan', x:event.clientX, y:event.clientY, pointer:event.pointerId};
    viewport.setPointerCapture(event.pointerId);
  };
  viewport.onpointermove = event => {
    if (!state.drag || event.pointerId !== state.drag.pointer) return;
    if (state.drag.mode === 'split') {
      const box = viewport.getBoundingClientRect();
      split(100 * (event.clientX - box.left) / box.width);
      return;
    }
    state.x += event.clientX - state.drag.x; state.y += event.clientY - state.drag.y;
    state.drag.x = event.clientX; state.drag.y = event.clientY; transform();
  };
  viewport.onpointerup = viewport.onpointercancel = viewport.onlostpointercapture = event => {
    if (!state.drag || event.pointerId !== state.drag.pointer) return;
    state.drag = null;
  };
  viewport.onkeydown = event => {
    if (!['+','=','-','0','ArrowLeft','ArrowRight','ArrowUp','ArrowDown',' '].includes(event.key)) return;
    event.preventDefault();
    if (event.key === ' ') { play(); return; }
    if (['ArrowLeft','ArrowRight'].includes(event.key) && !event.altKey) { step(event.key === 'ArrowLeft' ? -1 : 1, !event.shiftKey); return; }
    if (event.key === '0') { state.scale = 1; state.x = state.y = 0; }
    else if (['+','='].includes(event.key)) state.scale = Math.min(8,state.scale * 1.25);
    else if (event.key === '-') state.scale = Math.max(1,state.scale / 1.25);
    else { state.x += event.key === 'ArrowLeft' ? 24 : event.key === 'ArrowRight' ? -24 : 0; state.y += event.key === 'ArrowUp' ? 24 : event.key === 'ArrowDown' ? -24 : 0; }
    transform();
  };
  return {state, render, invalidate, request, open};
};
