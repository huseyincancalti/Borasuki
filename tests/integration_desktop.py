"""Isolated WebView2 checks; briefly visible for media decoding, no GPU inference."""

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import webview

from borasuki.app import API, endpoint
from borasuki.service import Service
from borasuki.storage import ROOT
from borasuki.process import ProcessGroup
from borasuki.process import ProcessError, Interrupted


def main(source=None):
    with tempfile.TemporaryDirectory(prefix="borasuki-ui-check-") as folder:
        fixture = None
        if source is None:
            from test_queue import QueueTests
            fixture = QueueTests()
            fixture.setUp()
            for target, value in (('borasuki.preparation.identity', ('fixture', {})), ('borasuki.preparation.ready', True)):
                mock = patch(target, return_value=value)
                mock.start()
                fixture.addCleanup(mock.stop)
            service, source = fixture.service, fixture.source
        else:
            service = Service(Path(folder), start_worker=False)
        service.save_settings({"notifications": False, "queue_enabled": False})
        ready, failures = threading.Event(), []

        class TestAPI(API):
            @endpoint
            def client_ready(self):
                ready.set()

        api = TestAPI(service)
        window = webview.create_window("Borasuki bridge check", str(ROOT / "frontend/index.html"),
                                       js_api=api, hidden=True, width=1120, height=820)
        api._window = window

        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while not predicate():
                if time.monotonic() >= deadline:
                    raise AssertionError("Bridge operation timed out")
                time.sleep(0.05)

        def checks():
            try:
                assert ready.wait(15), "Frontend did not connect"
                service.updates.result = {'status': 'available', 'current': '1.0.0-beta.3', 'version': '1.0.0-beta.4'}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("!$('updateNotice').hidden && !$('updateSettingsOpen').hidden"))
                assert window.evaluate_js("$('updateNoticeText').textContent.includes('1.0.0-beta.4')")
                window.evaluate_js("$('updateDismiss').click()")
                assert window.evaluate_js("$('updateNotice').hidden")
                service.updates.result = {'status': 'checking', 'current': '1.0.0-beta.3'}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("$('updateCheck').disabled"))
                service.updates.result = {'status': 'error', 'current': '1.0.0-beta.3'}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("!$('updateCheck').disabled && $('updateSettingsOpen').hidden"))
                with patch.object(service.updates, 'request') as requested:
                    window.evaluate_js("$('updateCheck').click()")
                    wait_for(lambda: requested.call_count == 1)
                print('Update notice, dismiss, checking/error states and manual retry passed.', flush=True)
                service.setup = {'status':'failed', 'error':'error.setup_failed',
                                 'failure':{'code':'error.setup_failed', 'detail':'DLL missing <test-marker>'}}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("!$('setupGate').hidden"))
                assert window.evaluate_js("$('start').disabled && $('openPreview').disabled && $('previewRender').disabled")
                assert window.evaluate_js("!$('setupFailure').hidden && $('setupFailureDetail').textContent.includes('<test-marker>') && !$('setupFailureDetail').querySelector('test-marker')")
                service.setup = {'status':'ready', 'signature':{'fixture':True}}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("$('setupGate').hidden"))
                assert window.evaluate_js("$('setupFailure').hidden && $('setupFailureDetail').textContent === ''")
                with patch.object(window, 'create_file_dialog', return_value=[folder]):
                    window.evaluate_js("$('exportDiagnostics').click()")
                    wait_for(lambda: window.evaluate_js("$('diagnosticsStatus').textContent.length > 0"))
                    wait_for(lambda: window.evaluate_js("!$('exportDiagnostics').disabled"))
                assert len(list(Path(folder).glob('Borasuki-diagnostics-*.zip'))) == 1
                with patch.object(window, 'create_file_dialog', return_value=None):
                    window.evaluate_js("$('exportDiagnostics').click()")
                    wait_for(lambda: window.evaluate_js("!$('exportDiagnostics').disabled"))
                assert len(list(Path(folder).glob('Borasuki-diagnostics-*.zip'))) == 1
                assert window.evaluate_js("getComputedStyle($('browse')).minHeight === '40px'")
                assert window.evaluate_js("$('batchPanel').hidden && !document.querySelector('.sidebarNote')")
                window.evaluate_js("page('add');document.querySelector('[data-help-title=\"ui.denoise\"]').focus();document.activeElement.click();")
                wait_for(lambda: window.evaluate_js("$('denoiseTitle-help').matches(':popover-open')"))
                assert window.evaluate_js("$('denoiseTitle-help').textContent.includes(t('ui.denoise_hint'))")
                window.evaluate_js("$('denoiseTitle-help').querySelector('button').click()")
                assert window.evaluate_js("!$('denoiseTitle-help').matches(':popover-open') && document.activeElement.dataset.helpTitle === 'ui.denoise'")
                window.evaluate_js("document.activeElement.click();page('queue');")
                assert window.evaluate_js("!document.querySelector('.infoPopover:popover-open') && $('addMore').textContent === document.querySelector('[data-page=add]').textContent")
                window.evaluate_js("page('add');translate();translate();")
                assert window.evaluate_js("document.querySelectorAll('[data-help-title=\"ui.denoise\"]').length === 1")
                window.evaluate_js("page('settings')")
                assert window.evaluate_js("document.activeElement === $('page-settings').querySelector('h1')")
                window.evaluate_js("$('language').focus();page('queue');page('settings')")
                assert window.evaluate_js("document.activeElement === $('language')")
                window.evaluate_js("saveSettings({theme:'light'});saveSettings({theme:'dark'});saveSettings({sound:true});saveSettings({sound:false});")
                wait_for(lambda: window.evaluate_js("!app.settingsBusy"))
                assert service.settings['theme'] == 'dark' and service.settings['sound'] is False
                with patch.object(window, 'create_file_dialog', return_value=[str(source)]):
                    window.evaluate_js("page('add');$('browse').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy && app.media?.path === " + json.dumps(str(source))))
                assert window.evaluate_js("$('batchPanel').hidden && $('source').textContent === app.media.name")
                media = service.inspect(str(source))
                script = "receiveMedia(" + json.dumps(media) + ");"
                script += "$('folder').value=" + json.dumps(folder) + ";$('filename').value='desktop-check.mkv';"
                script += "$('denoiseStrength').value='3';$('denoiseEnabled').checked=false;updateDenoise();"
                window.evaluate_js(script)
                assert window.evaluate_js("$('drop').classList.contains('hasSource') && $('source').textContent === app.media.name && $('source').title === app.media.path")
                wait_for(lambda: window.evaluate_js("app.preparation.ready() && !$('start').disabled"))
                window.evaluate_js("app.media.streams=['audio','subtitle'];$('outputFormat').value='mp4';$('outputFormat').dispatchEvent(new Event('change'))")
                assert window.evaluate_js("$('filename').value === 'desktop-check.mp4' && !$('outputTrackWarning').hidden")
                window.evaluate_js("confirmOutputTracks()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert window.evaluate_js("$('confirmBody').textContent === t('confirm.mp4_tracks')")
                window.evaluate_js("$('confirmation').querySelector('[value=cancel]').click()")
                wait_for(lambda: window.evaluate_js("!$('confirmation').open"))
                assert window.evaluate_js("!jobValues().drop_tracks_confirmed")
                window.evaluate_js("confirmOutputTracks()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: window.evaluate_js("!$('confirmation').open && jobValues().drop_tracks_confirmed"))
                window.evaluate_js("$('filename').value='another.mp4';$('filename').dispatchEvent(new Event('input',{bubbles:true}))")
                assert window.evaluate_js("!jobValues().drop_tracks_confirmed"), 'MP4 confirmation survived an output change'
                window.evaluate_js("app.media.streams=" + json.dumps(media['streams']) + ";$('filename').value='desktop-check.mp4';$('outputFormat').value='mkv';$('outputFormat').dispatchEvent(new Event('change'))")
                service.engine_task.update(status='preparing', failure=None)
                service.engine_task['progress'] = {'stage':'engine_building', 'elapsed':60,
                    'updated_at':time.time(), 'completed':None, 'total':None, 'eta':120, 'eta_basis':'history'}
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("$('start').disabled && $('openPreview').disabled"))
                assert window.evaluate_js("$('enginePreparationProgress').hidden && $('enginePreparationStatus').textContent === t('wait.engine_building') && $('enginePreparation').querySelector('.waitMetrics').textContent.includes(t('wait.elapsed',{time:'1:00'}).split(':')[0]) && !$('enginePreparation').querySelector('.waitMetrics').textContent.includes('%')")
                assert window.evaluate_js("$('engineStepCheck').classList.contains('is-complete') && $('engineStepGPU').getAttribute('aria-current') === 'step' && !$('engineStepVerify').classList.contains('is-complete') && getComputedStyle($('enginePreparation')).borderLeftWidth === '4px'")
                assert window.evaluate_js("$('enginePreparationCancel').closest('.operationHeader') !== null && $('enginePreparation').querySelector('.waitMetrics').getBoundingClientRect().height < 55 && $('enginePreparationStatus').textContent.length < 75")
                service.engine_task['progress'].update(stage='engine_warming', completed=1, total=4, eta=10)
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("$('enginePreparationProgress').value === 1"))
                assert window.evaluate_js("!$('enginePreparationProgress').hidden && $('enginePreparationProgress').max === 4 && $('enginePreparation').querySelector('.waitMetrics').textContent.includes('25%')")
                assert window.evaluate_js("$('engineStepGPU').classList.contains('is-complete') && !$('engineStepGPU').hasAttribute('aria-current') && $('engineStepVerify').getAttribute('aria-current') === 'step'")
                window.evaluate_js("document.querySelector('[data-page=preview]').click()")
                assert window.evaluate_js("!$('page-add').hidden && document.activeElement === $('enginePreparation')")
                service.engine_task.update(status='failed', failure={'code':'error.internal', 'detail':"KeyError: 'range' <test-marker>", 'retryable':False})
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("!$('enginePreparationDetails').hidden"))
                assert window.evaluate_js("$('enginePreparationStatus').textContent === t('error.internal') && $('start').disabled && $('openPreview').disabled && $('previewRender').disabled")
                assert window.evaluate_js("$('enginePreparationSteps').hidden")
                window.evaluate_js("$('enginePreparationDetails').click()")
                assert window.evaluate_js("$('enginePreparationFailure').open && $('enginePreparation').contains($('enginePreparationTechnical')) && document.activeElement === $('enginePreparationTechnical') && !$('enginePreparationTechnical').querySelector('test-marker')")
                window.evaluate_js("window.scrollTo(0,document.body.scrollHeight);showError({code:'error.internal',detail:'test'})")
                wait_for(lambda: window.evaluate_js("$('error').getBoundingClientRect().top >= -1"))
                assert window.evaluate_js("document.activeElement === $('error')")
                service.engine_task.update(status='ready', error=None, failure=None)
                window.evaluate_js("$('error').hidden=true;refresh(true)")
                wait_for(lambda: window.evaluate_js("!$('start').disabled && $('enginePreparation').hidden"))
                window.evaluate_js("$('filename').focus();page('preview');$('previewBack').click()")
                assert window.evaluate_js("document.activeElement === $('filename') && $('filename').value === 'desktop-check.mkv' && !$('denoiseEnabled').checked && $('previewBack').closest('header') !== null")
                window.evaluate_js("$('browse').focus(); window.requestAppClose();")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert window.evaluate_js("document.activeElement.value === 'cancel' && $('confirmBody').textContent === t('confirm.exit')")
                window.evaluate_js("$('confirmation').dispatchEvent(new Event('cancel', {cancelable:true}));")
                wait_for(lambda: window.evaluate_js("!$('confirmation').open && !closePending"))
                assert not service.closing and not service.stop.is_set()
                assert window.evaluate_js("document.activeElement === $('browse') && !$('closeStatus').offsetHeight")
                with patch.object(service, 'begin_shutdown', side_effect=TimeoutError('error.shutdown_timeout')):
                    window.evaluate_js("window.requestAppClose(); window.requestAppClose();")
                    wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                    window.evaluate_js("$('confirmAccept').click()")
                    wait_for(lambda: window.evaluate_js("!closePending && !$('error').hidden"))
                assert window.evaluate_js("$('errorMessage').textContent === t('error.shutdown_timeout') && $('closeStatus').hidden")
                assert not service.closing
                window.evaluate_js("$('errorDismiss').click()")
                window.evaluate_js("$('colorMode').value='custom';updateColor(true);$('contrast').value='5';$('brightness').value='2';updateColor();$('presetName').value='Desktop preset';$('presetSave').click()")
                wait_for(lambda: window.evaluate_js("!app.busy && $('presetSelect').options.length === 2"))
                preset_id = service.list_presets()[0]['id']
                before_paths = window.evaluate_js("[$('folder').value,$('filename').value,$('gpu').value,app.media.path]")
                window.evaluate_js("$('contrast').value='0';$('denoiseEnabled').checked=true;updateDenoise();$('presetApply').click()")
                wait_for(lambda: window.evaluate_js("!app.busy && app.presets.id() !== null"))
                assert window.evaluate_js("$('contrast').value === '5' && $('brightness').value === '2' && !$('denoiseEnabled').checked")
                assert window.evaluate_js("[$('folder').value,$('filename').value,$('gpu').value,app.media.path]") == before_paths
                window.evaluate_js("$('contrast').dispatchEvent(new Event('input',{bubbles:true}))")
                assert window.evaluate_js("app.presets.id() === null"), 'Manual edit retained stale preset identity'
                window.evaluate_js("$('presetDelete').click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert service.list_presets()[0]['id'] == preset_id
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: window.evaluate_js("!app.busy"))
                assert service.list_presets() == []
                recipe = service.save_preset('Adaptive recipe', {'upscale':{'scale':2}, 'denoise':{'enabled':False,'strength':3},
                    'color_mode':'adaptive', 'color_overrides':{'brightness':0.01}})
                profile_results = {'contrast':1.02,'brightness':0,'saturation':1.01, 'profiles': {
                    'normal': {'contrast':1.02,'brightness':0,'saturation':1.01},
                    'dark': {'contrast':1.02,'brightness':-.02,'saturation':1},
                    'high_saturation': {'contrast':1.02,'brightness':0,'saturation':1.15}}}
                with patch.object(service, '_analyze_config', return_value=profile_results) as fresh_analysis:
                    window.evaluate_js("app.presets.refresh()")
                    wait_for(lambda: window.evaluate_js("$('presetSelect').options.length === 2"))
                    window.evaluate_js("$('presetSelect').value=" + json.dumps(recipe['id']) + ";$('presetSelect').dispatchEvent(new Event('change'));$('presetApply').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy && app.color.ready()"))
                    assert window.evaluate_js("app.color.payload().overrides.brightness === 0.01 && $('brightness').value === '1' && $('contrast').disabled")
                    assert window.evaluate_js("$('colorAdjustments').open")
                    window.evaluate_js("$('colorAdjustments').open=false;app.color.render(app.snapshot)")
                    assert window.evaluate_js("!$('colorAdjustments').open"), 'Polling reopened the manually collapsed color controls'
                    window.evaluate_js('receiveMedia(' + json.dumps(media) + ')')
                    wait_for(lambda: window.evaluate_js("app.color.ready()"))
                    assert fresh_analysis.call_count == 2, 'Preset copied an old analysis instead of analyzing the imported source'
                    assert window.evaluate_js("app.color.payload().overrides.brightness === 0.01")
                    window.evaluate_js("$('adaptiveProfile').value='dark';$('adaptiveProfile').dispatchEvent(new Event('change',{bubbles:true}))")
                    assert window.evaluate_js("$('brightness').value === '-2' && $('brightness').disabled && app.color.payload().profile === 'dark' && Object.keys(app.color.payload().overrides).length === 0 && app.presets.id() === null")
                    window.evaluate_js("$('adaptiveProfile').value='high_saturation';$('adaptiveProfile').dispatchEvent(new Event('change',{bubbles:true}))")
                    assert window.evaluate_js("Math.abs(Number($('saturation').value)-15)<0.00001 && $('saturation').disabled")
                    assert fresh_analysis.call_count == 2, 'Profile selection resampled the video'
                    assert window.evaluate_js("UI.colorLabel({color_mode:'adaptive',adaptive_profile:'dark'},t).includes(t('adaptive.dark'))")
                    window.evaluate_js("$('presetName').value='Vivid preset';$('presetSave').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy"))
                    assert next(p for p in service.list_presets() if p['name'] == 'Vivid preset')['values']['adaptive_profile'] == 'high_saturation'
                window.evaluate_js("app.presets.clear();$('colorMode').value='custom';updateColor(true);$('filename').value='desktop-check.mkv'")
                window.evaluate_js("$('adaptiveProfile').value='normal'")
                # Selection-time analysis and per-value overrides; no inference.
                analyzed, release_analysis = threading.Event(), threading.Event()
                baseline = {'contrast':1.012345, 'brightness':0, 'saturation':1.006789, 'reason':'adjusted', 'version':1}
                def colors(config, stop, update=None):
                    analyzed.set()
                    if not release_analysis.wait(5):
                        raise AssertionError('Analysis UI check did not release worker')
                    return baseline
                with patch.object(service, '_analyze_config', side_effect=colors):
                    window.evaluate_js("$('colorMode').value='adaptive';updateColor(true)")
                    assert analyzed.wait(3)
                    assert window.evaluate_js("!$('custom').hidden && !$('analysisProgress').hidden && $('contrast').disabled && $('contrastOverride').disabled && $('start').disabled && $('openPreview').disabled")
                    window.evaluate_js("$('colorMode').value='custom';updateColor(true);$('colorMode').value='adaptive';updateColor(true)")
                    release_analysis.set()
                    wait_for(lambda: window.evaluate_js("app.color.state.status === 'ready'"))
                    assert window.evaluate_js("$('contrast').disabled && !$('contrastOverride').disabled && $('contrastValue').value === '1.2345' && !$('start').disabled && !$('openPreview').disabled")
                    window.evaluate_js("$('brightnessOverride').click();$('brightness').value='1';$('brightness').dispatchEvent(new Event('input',{bubbles:true}));")
                    payload = window.evaluate_js('jobValues()')
                    assert payload['adaptive']['overrides'] == {'brightness':0.01}
                    assert payload['denoise'] == {'enabled':False, 'strength':3}
                    assert window.evaluate_js("!$('brightness').disabled && $('contrast').disabled && $('saturation').disabled")
                    window.evaluate_js("$('brightnessOverride').click()")
                    assert window.evaluate_js("$('brightness').disabled && $('brightness').value === '0' && Object.keys(app.color.payload().overrides).length === 0")
                cause = 'Source: Frame accurate seeking is not possible in this file <test-marker>'
                with patch.object(service, '_analyze_config', side_effect=ProcessError(cause)):
                    window.evaluate_js('app.color.request()')
                    wait_for(lambda: window.evaluate_js("app.color.state.status === 'failed'"))
                    assert window.evaluate_js("$('analysisRetry').hidden && !$('analysisRecovery').hidden && !$('analysisDetails').hidden && !$('analysisLogs').hidden")
                    assert window.evaluate_js("$('analysisStatus').textContent.includes(t('error.video_seek'))")
                    assert cause in window.evaluate_js("$('analysisTechnical').textContent")
                    assert window.evaluate_js("$('analysisTechnical').children.length === 0"), 'Error detail treated as HTML'
                window.evaluate_js('receiveMedia(' + json.dumps({'import_error':{'code':'error.timestamp','detail':'Video start: 0.125 s'}, 'path':'rejected.mp4'}) + ')')
                assert window.evaluate_js("app.media === null && $('start').disabled && $('openPreview').disabled && !$('errorDetails').hidden")
                assert 'rejected.mp4' in window.evaluate_js("$('source').textContent")
                window.evaluate_js("$('colorMode').value='custom';updateColor(true);receiveMedia(" + json.dumps(media) + ")")
                script = "$('filename').value='desktop-check.mkv';"
                script += "$('colorMode').value='custom';updateColor(true);$('start').click();"
                window.evaluate_js(script)
                wait_for(lambda: len(service.jobs) == 1)
                wait_for(lambda: window.evaluate_js("!app.busy"))
                identifier = next(iter(service.jobs))
                assert service.jobs[identifier]["grade"] == {"contrast": 1, "brightness": 0, "saturation": 1}
                assert service.jobs[identifier]["upscale"] == {"scale": 2}
                assert service.jobs[identifier]["denoise"] == {"enabled": False, "strength": 3}
                assert service.jobs[identifier]["model"]["noise"] == -1
                assert service.jobs[identifier]["color_mode"] == "custom"
                assert service.jobs[identifier]["status"] == "queued"
                assert window.evaluate_js("!$('page-queue').hidden && $('error').hidden")
                assert window.evaluate_js("!$('queue').querySelector('[data-action=\"ui.pause\"]')")
                assert window.evaluate_js("!$('queue').querySelector('[data-action=\"ui.cancel\"]')")
                second = service.create(str(source), str(Path(folder) / 'second.mkv'), 'original', 0, {},
                                        {'scale':2}, {'enabled':False, 'strength':3})
                window.evaluate_js("refresh(true)")
                wait_for(lambda: window.evaluate_js("$('queue').querySelectorAll('.job').length === 2"))
                window.evaluate_js("$('queue').lastElementChild.querySelector('[data-action=\"ui.move_up\"]').focus();document.activeElement.click()")
                wait_for(lambda: service.snapshot()['jobs'][0]['id'] == second)
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert window.evaluate_js("$('queue').firstElementChild.querySelector('[data-action=\"ui.move_up\"]').disabled")
                assert window.evaluate_js("document.activeElement.closest('[data-id]')?.dataset.id") == second
                # Snapshot refresh must not steal focus from an unchanged row.
                window.evaluate_js("$('queue').firstElementChild.querySelector('[data-action=\"ui.details\"]').focus();refresh(true)")
                wait_for(lambda: window.evaluate_js("!app.refreshing"))
                assert window.evaluate_js("document.activeElement.dataset.action === 'ui.details'")
                window.evaluate_js("document.activeElement.click()")
                wait_for(lambda: window.evaluate_js("$('details').open"))
                window.evaluate_js("$('queueToggle').focus()")
                assert window.evaluate_js("$('details').contains(document.activeElement)"), 'Modal focus escaped'
                window.evaluate_js("$('details').querySelector('button').click()")
                wait_for(lambda: window.evaluate_js("!$('details').open"))
                window.evaluate_js("editJob(" + json.dumps(identifier) + ")")
                wait_for(lambda: service.jobs[identifier]["status"] == "editing")
                wait_for(lambda: window.evaluate_js("!app.busy"))
                assert window.evaluate_js("!$('editNotice').hidden && !$('page-add').hidden")
                assert window.evaluate_js("$('denoiseStrength').disabled && $('denoiseStrength').value === '3' && $('colorMode').value === 'custom'")
                window.evaluate_js("$('contrast').value='7';updateColor();$('denoiseEnabled').checked=true;updateDenoise();")
                assert window.evaluate_js("$('contrast').value === '7' && $('upscaleScale').value === '2'")
                window.evaluate_js("$('colorMode').value='original';updateColor(true)")
                assert window.evaluate_js("$('denoiseEnabled').checked && $('denoiseStrength').value === '3'")
                window.evaluate_js("$('discardEdit').click()")
                wait_for(lambda: service.jobs[identifier]["status"] == "queued")
                wait_for(lambda: window.evaluate_js("!app.busy"))
                window.evaluate_js("$('queue').querySelector('[data-id=\"" + identifier + "\"] [data-action=\"ui.remove\"]').focus();document.activeElement.click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert identifier in service.jobs, "Remove happened before confirmation"
                assert window.evaluate_js("document.activeElement.value === 'cancel'")
                window.evaluate_js("$('confirmation').querySelector('[value=\"cancel\"]').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert identifier in service.jobs
                assert window.evaluate_js("document.activeElement.dataset.action === 'ui.remove'")
                window.evaluate_js("document.activeElement.click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: identifier not in service.jobs)
                service.update(second, status='failed', error='error.disk_space')
                service.update(second, failure={'code':'error.disk_space','detail':'disk fixture','recovery':'storage','retryable':True})
                window.evaluate_js("refresh(true);page('history')")
                wait_for(lambda: window.evaluate_js("$('history').querySelectorAll('.job').length === 1"))
                assert window.evaluate_js("!!$('history').querySelector('[data-action=\"ui.retry\"]')")
                assert window.evaluate_js("$('history').querySelector('.jobError').textContent === t('error.disk_space') && !!$('history').querySelector('details pre')")
                assert window.evaluate_js("$('error').hidden")
                window.evaluate_js("$('history').querySelector('[data-action=\"ui.temp_files\"]').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0 && !$('page-settings').hidden"))
                assert window.evaluate_js("document.activeElement === $('storageRefresh')")
                assert not (Path(folder) / 'desktop-check.mkv').exists(), "Unexpected render"
                storage_work = service.data / 'jobs' / second / 'revision-0'
                storage_work.mkdir(parents=True, exist_ok=True)
                (storage_work / 'segment.mkv').write_bytes(b'checkpoint')
                window.evaluate_js("page('settings');$('storageRefresh').click()")
                wait_for(lambda: window.evaluate_js("!!$('storageList').querySelector('button') && app.pendingActions.size === 0"))
                window.evaluate_js("$('storageList').querySelector('button').click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert storage_work.exists(), 'Cleanup happened before confirmation'
                window.evaluate_js("$('confirmation').querySelector('[value=\"cancel\"]').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert storage_work.exists()
                window.evaluate_js("$('storageList').querySelector('button').click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert not storage_work.exists() and second in service.jobs and source.exists()
                cache = service.data / 'engine-cache'
                cache.mkdir(exist_ok=True)
                cache_file = cache / 'f00dcafe.engine'
                cache_file.write_bytes(b'rebuildable engine fixture')
                keep_file = cache / 'personal.txt'
                keep_file.write_text('keep')
                window.evaluate_js("refreshStorage()")
                wait_for(lambda: window.evaluate_js("!!$('storageList').querySelector('[data-storage-id=cache] button')"))
                window.evaluate_js("$('storageList').querySelector('[data-storage-id=cache] button').click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                assert cache_file.exists()
                window.evaluate_js("$('confirmation').querySelector('[value=\"cancel\"]').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert cache_file.exists()
                window.evaluate_js("$('storageList').querySelector('[data-storage-id=cache] button').click()")
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0"))
                assert not cache_file.exists() and keep_file.exists() and source.exists()
                # Real browser media decoding; inference is replaced by a short CPU fixture.
                playback = Path(folder) / 'source-playback.mp4'
                with ProcessGroup(threading.Event()) as group:
                    group.capture([shutil.which('ffmpeg'), '-v', 'error', '-n', '-f', 'lavfi', '-i',
                                   'testsrc2=size=480x270:rate=24:duration=10', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(playback)])
                if fixture:
                    shutil.copyfile(playback, source)
                displays = {}
                single_frames = {}
                for name, size in [('original','160x90'), ('enhanced','320x180')]:
                    path = Path(folder) / (name + '.mp4')
                    with ProcessGroup(threading.Event()) as group:
                        group.capture([shutil.which('ffmpeg'), '-v', 'error', '-n', '-f', 'lavfi', '-i',
                                       f'testsrc2=size={size}:rate=24:duration=0.5', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
                    displays[name] = str(path)
                    single = Path(folder) / (name + '-frame.mp4')
                    with ProcessGroup(threading.Event()) as group:
                        group.capture([shutil.which('ffmpeg'), '-v', 'error', '-n', '-i', str(path),
                                       '-frames:v', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(single)])
                    single_frames[name] = str(single)
                def sample(job, start, duration, stop, update):
                    return {'files':single_frames if duration == 'frame' else displays, 'fps':24,
                            'total_frames':1 if duration == 'frame' else 12,
                            'width':160, 'height':90, 'start_frame':round(start * 24)}
                with patch('borasuki.service.render_preview', side_effect=sample):
                    service.worker.start()
                    window.show()
                    window.evaluate_js("page('add');$('filename').value='preview-final.mkv';refresh(true)")
                    wait_for(lambda: window.evaluate_js("!$('openPreview').disabled"))
                    window.evaluate_js("$('openPreview').click()")
                    wait_for(lambda: window.evaluate_js("!$('page-preview').hidden && !$('previewRender').disabled"))
                    wait_for(lambda: window.evaluate_js("app.compare.state.sourceUrl?.startsWith('http://127.0.0.1:')"))
                    assert window.evaluate_js("$('sourceVideo').src.startsWith('http://127.0.0.1:') && !$('useSourcePosition')")
                    assert service.preview_task is None, 'Opening Preview must not start rendering'
                    assert window.evaluate_js("!!app.compare.state.checks && $('draftCheckStatus').textContent.includes(t('ui.preflight_ready'))")
                    assert window.evaluate_js("$('previewStart').closest('.page').id === 'page-preview' && !$('jobForm').contains($('previewDuration'))")
                    window.evaluate_js("$('sourceRetry').click()")
                    wait_for(lambda: window.evaluate_js("$('sourceVideo').readyState >= 2 && !$('previewNext').disabled"))
                    assert window.evaluate_js("[...$('previewDuration').options].map(item=>item.value).join(',') === 'frame,1,2,5'")
                    window.evaluate_js("$('sourceTimeline').value='0';$('sourceTimeline').dispatchEvent(new Event('input'))")
                    wait_for(lambda: window.evaluate_js("!$('sourceVideo').seeking"))
                    window.evaluate_js("$('previewNext').click()")
                    wait_for(lambda: window.evaluate_js("!$('sourceVideo').seeking"))
                    assert window.evaluate_js("Math.abs(Number($('previewStart').value)-1/24)<0.000001")
                    window.evaluate_js("$('previewStep').value='2';$('previewNext').click()")
                    wait_for(lambda: window.evaluate_js("!$('sourceVideo').seeking"))
                    assert window.evaluate_js("Math.abs(Number($('previewStart').value)-49/24)<0.000001")
                    assert service.preview_task is None, 'Source stepping must not trigger GPU processing'
                    window.evaluate_js("$('previewPlay').click()")
                    wait_for(lambda: window.evaluate_js("!$('sourceVideo').paused && Number($('sourceTimeline').value)>49"))
                    window.evaluate_js("$('previewPlay').click();$('sourceTimeline').value='0';$('sourceTimeline').dispatchEvent(new Event('input'));$('previewRender').click()")
                    wait_for(lambda: window.evaluate_js("!!app.compare.state.loaded && !$('previewCommit').disabled && !$('compareViewport').classList.contains('is-seeking')"))
                    assert service.preview_task['duration'] == 'frame' and service.preview_task['total_frames'] == 1
                    assert window.evaluate_js("$('compareFrame').max === '0' && $('previewPlay').disabled && $('sourceVideo').hidden && !$('compareReady').hidden")
                    assert window.evaluate_js("$('compareOriginalFrame').width === 160 && $('compareEnhancedFrame').width === 320 && app.compare.state.mediaReady && $('compareEnhancedFrame').getContext('2d').getImageData(0,0,1,1).data[3] === 255")
                    window.evaluate_js("$('previewDuration').value='1';$('previewDuration').dispatchEvent(new Event('input'))")
                    wait_for(lambda: window.evaluate_js("!$('previewRender').disabled"))
                    window.evaluate_js("$('previewStart').value='1';$('previewStart').dispatchEvent(new Event('input'));$('previewRender').click()")
                    wait_for(lambda: window.evaluate_js("!!app.compare.state.loaded && !$('previewCommit').disabled"))
                    assert service.preview_task['start'] == 1
                    window.evaluate_js("$('previewStart').value='2';$('previewStart').dispatchEvent(new Event('input'))")
                    assert window.evaluate_js("$('previewCommit').disabled && $('compareReady').hidden")
                    wait_for(lambda: window.evaluate_js("!$('previewRender').disabled"))
                    window.evaluate_js("$('previewRender').click()")
                    wait_for(lambda: window.evaluate_js("!!app.compare.state.loaded && !$('previewCommit').disabled"))
                    assert service.preview_task['start'] == 2
                    wait_for(lambda: window.evaluate_js("$('compareOriginal').readyState >= 2 && $('compareEnhanced').readyState >= 2"))
                    window.evaluate_js("$('compareFrame').value='5';$('compareFrame').dispatchEvent(new Event('input'));$('compareViewport').dispatchEvent(new KeyboardEvent('keydown',{key:'+'}));")
                    wait_for(lambda: window.evaluate_js("!$('compareViewport').classList.contains('is-seeking')"))
                    assert window.evaluate_js("Math.abs($('compareOriginal').currentTime-$('compareEnhanced').currentTime)<0.0001")
                    assert window.evaluate_js("$('compareOriginal').style.transform === $('compareEnhanced').style.transform && app.compare.state.scale>1")
                    window.evaluate_js("$('compareLine').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',shiftKey:true,bubbles:true}))")
                    assert window.evaluate_js("$('compareSplit').value === '60' && $('compareLine').getAttribute('aria-valuenow') === '60' && $('compareFrame').value === '5'")
                    window.evaluate_js("""(() => {
                      const box = $('compareViewport').getBoundingClientRect(), viewport = $('compareViewport');
                      const capture = viewport.setPointerCapture; viewport.setPointerCapture = () => {};
                      $('compareLine').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:7,clientX:box.left+box.width*.6,clientY:box.top+100}));
                      viewport.dispatchEvent(new PointerEvent('pointermove',{pointerId:7,clientX:box.left+box.width*.25,clientY:box.top+100}));
                      viewport.dispatchEvent(new PointerEvent('pointerup',{pointerId:7}));
                      viewport.dispatchEvent(new PointerEvent('pointerdown',{button:0,pointerId:8,clientX:box.left+100,clientY:box.top+100}));
                      viewport.dispatchEvent(new PointerEvent('pointermove',{pointerId:8,clientX:box.left+110,clientY:box.top+100}));
                      viewport.dispatchEvent(new PointerEvent('pointermove',{pointerId:8,clientX:box.left+120,clientY:box.top+100}));
                      viewport.dispatchEvent(new PointerEvent('pointercancel',{pointerId:8}));
                      viewport.setPointerCapture = capture;
                    })()""")
                    assert window.evaluate_js("Math.abs(Number($('compareSplit').value)-25)<1 && app.compare.state.x === 20 && app.compare.state.drag === null")
                    window.evaluate_js("$('previewPlay').click()")
                    wait_for(lambda: window.evaluate_js("Number($('compareFrame').value)>5"))
                    wait_for(lambda: window.evaluate_js("!app.compare.state.playing && $('compareFrame').value === '11' && !$('compareViewport').classList.contains('is-seeking')"))
                    assert window.evaluate_js("Math.abs($('compareOriginal').currentTime-$('compareEnhanced').currentTime)<0.0001")
                    window.evaluate_js("$('compareFrame').value='5';$('compareFrame').dispatchEvent(new Event('input'))")
                    wait_for(lambda: window.evaluate_js("!$('compareViewport').classList.contains('is-seeking')"))
                    assert window.evaluate_js("$('compareViewport').clientHeight < innerHeight*.6 && $('compareViewport').contains($('sourceVideo')) && $('compareViewport').contains($('compareOriginal')) && document.documentElement.scrollWidth <= innerWidth")
                    preview_id = service.preview_task['id']
                    window.evaluate_js("showError({code:'error.disk_space',recovery:'storage'});UI.message($('diagnosticsStatus'),t,'ui.diagnostics_saved',{path:'C:/test/{sample}.zip'});")
                    for language in ('en', 'tr'):
                        window.evaluate_js("saveSettings({language:" + json.dumps(language) + "})")
                        wait_for(lambda: window.evaluate_js("!app.settingsBusy && document.documentElement.lang === " + json.dumps(language)))
                        assert window.evaluate_js("[...document.querySelectorAll('nav [data-page]')].every(button => button.textContent === t(button.dataset.i18n))")
                        assert window.evaluate_js("$('previewTitle').textContent === t('nav.preview') && $('previewBack').textContent.includes(t('nav.add'))")
                        assert window.evaluate_js("$('previewSummary').textContent.includes(t('ui.denoise')) && $('compareFrameValue').textContent === t('ui.preview_frame',{frame:6,total:app.compare.state.loaded.total_frames})")
                        assert window.evaluate_js("$('draftCheckStatus').textContent.includes(t('ui.preflight_ready')) && $('previewDisk').textContent.includes(t('ui.disk_values',{free:(app.compare.state.loaded.preflight.volumes[0].free/1024**3).toFixed(1),estimate:(app.compare.state.loaded.preflight.volumes[0].estimated/1024**3).toFixed(1)}))")
                        assert window.evaluate_js("$('errorMessage').textContent === t('error.disk_space') && $('errorRecovery').querySelector('button').textContent === t('ui.temp_files')")
                        assert window.evaluate_js("$('diagnosticsStatus').textContent === t('ui.diagnostics_saved',{path:'C:/test/{sample}.zip'}) && $('denoiseTitle-help').textContent.includes(t('ui.denoise_hint'))")
                        assert window.evaluate_js("$('storageStatus').textContent.includes(t('ui.storage_total',{size:(app.storage.bytes/1024**2).toFixed(1)}))")
                        assert window.evaluate_js("$('compareFrame').value === '5' && app.compare.state.scale>1 && !$('previewCommit').disabled")
                        assert service.preview_task['id'] == preview_id, 'Language change triggered a new render'
                    window.evaluate_js("$('errorDismiss').click();page('preview')")
                    window.hide()
                    token = service.preview_task['id']
                    config = service.preview_task['job']['configuration']
                    window.evaluate_js('app.batch.receive(' + json.dumps({'paths':[str(source)], 'failures':[]}) + ')')
                    window.evaluate_js("$('previewCommit').click()")
                    wait_for(lambda: token in service.jobs)
                    self_config = service.jobs[token]['configuration']
                    assert self_config == config
                    wait_for(lambda: window.evaluate_js("!$('page-queue').hidden"))
                    assert window.evaluate_js("app.batch.state.paths.length === 0"), 'Preview commit left the source in batch selection'
                with patch('borasuki.service.render_preview', side_effect=ProcessError('Error: fwrite() call failed <test-marker>')):
                    window.evaluate_js("$('filename').value='preview-failure.mkv';app.compare.invalidate();app.compare.open()")
                    wait_for(lambda: window.evaluate_js("!$('previewRender').disabled"))
                    window.evaluate_js("$('previewRender').click()")
                    wait_for(lambda: service.preview_task['status'] == 'failed')
                    wait_for(lambda: window.evaluate_js("!$('previewFailure').hidden"))
                    assert window.evaluate_js("$('previewStatus').textContent === t('error.frame_transfer') && $('previewFailureDetail').textContent.includes('<test-marker>') && !$('previewFailureDetail').querySelector('test-marker')")
                    assert window.evaluate_js("!!$('previewRecovery').querySelector('[data-action=\"ui.export_diagnostics\"]')")
                    window.evaluate_js("showError({code:'error.denoise_model_missing',recovery:'setup',detail:'missing model'});$('errorRecovery').querySelector('[data-action=\"ui.setup_open\"]').click()")
                    wait_for(lambda: window.evaluate_js("app.pendingActions.size === 0 && !$('page-settings').hidden"))
                    assert window.evaluate_js("document.activeElement === $('setupCheck')")
                # Batch selection never queues or starts a preview automatically.
                batch_paths = []
                for name in ('batch-a.mp4', 'batch-b.mp4'):
                    path = Path(folder) / name
                    shutil.copyfile(source, path)
                    batch_paths.append(str(path))
                before_jobs = set(service.jobs)
                previous_preview = service.preview_task['id']
                with patch.object(window, 'create_file_dialog', return_value=batch_paths):
                    window.evaluate_js("page('add');$('colorMode').value='custom';updateColor(true);app.presets.clear();$('browse').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy && $('batchSelect').options.length === 2"))
                assert set(service.jobs) == before_jobs and service.preview_task['id'] == previous_preview
                with patch.object(window, 'create_file_dialog', return_value=None):
                    window.evaluate_js("$('browse').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy"))
                assert window.evaluate_js("$('batchSelect').options.length === 2")
                window.evaluate_js("$('batchOpen').click()")
                wait_for(lambda: window.evaluate_js("!app.busy && app.media.path.endsWith('batch-a.mp4')"))
                assert set(service.jobs) == before_jobs
                wait_for(lambda: window.evaluate_js("!$('openPreview').disabled"))
                window.evaluate_js("$('openPreview').click()")
                wait_for(lambda: window.evaluate_js("!$('page-preview').hidden && !$('previewRender').disabled"))
                assert service.preview_task['id'] == previous_preview, 'Batch preview opened but rendered without consent'
                window.evaluate_js("page('add');$('contrast').value='2';$('brightness').value='1';$('saturation').value='1';$('gpu').value='1';$('gpu').dispatchEvent(new Event('change',{bubbles:true}))")
                wait_for(lambda: window.evaluate_js("!$('batchQueue').disabled"))
                window.evaluate_js("$('batchQueue').click()")
                wait_for(lambda: service.batch_task and service.batch_task['status'] == 'complete')
                wait_for(lambda: window.evaluate_js("!app.busy && app.batch.state.task?.status === 'complete' && !$('batchReport').hidden"))
                assert [item['status'] for item in service.batch_task['items']] == ['added', 'added']
                assert len(set(service.jobs) - before_jobs) == 2
                for identifier in set(service.jobs) - before_jobs:
                    job = service.jobs[identifier]
                    assert job['grade'] == {'contrast':1.02, 'brightness':0.01, 'saturation':1.01} and job['gpu_id'] == 1
                assert window.evaluate_js("$('batchSelect').options.length === 0 && $('batchResults').children.length === 2")
                # A deliberate re-selection is not erased by polling an old result.
                window.evaluate_js('receiveBatch(' + json.dumps({'paths':[batch_paths[0]], 'failures':[]}) + ')')
                window.evaluate_js('refresh(true)')
                wait_for(lambda: window.evaluate_js("!app.refreshing"))
                assert window.evaluate_js("$('batchSelect').options.length === 1")
                window.evaluate_js("app.color.state.status='ready';app.color.state.pendingOverrides={brightness:0.02};app.color.state.overrides={};")
                assert window.evaluate_js("Object.keys(processingRecipe(true).color_overrides).length === 0"), 'Removed Adaptive override was restored'
                entered = threading.Event()
                def preparing(*args, **kwargs):
                    entered.set()
                    assert kwargs['stop'].wait(10)
                    raise Interrupted()
                jobs_before_cancel = set(service.jobs)
                with patch.object(service, '_prepare', side_effect=preparing):
                    window.evaluate_js("$('batchQueue').click()")
                    assert entered.wait(3)
                    wait_for(lambda: window.evaluate_js("!app.busy && !$('batchCancel').hidden"))
                    window.evaluate_js("$('batchCancel').click()")
                    wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                    window.evaluate_js("$('confirmation').querySelector('[value=\"cancel\"]').click()")
                    wait_for(lambda: window.evaluate_js("!$('confirmation').open"))
                    assert not service.batch_task['stop'].is_set()
                    window.evaluate_js("$('batchCancel').click()")
                    wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                    window.evaluate_js("$('confirmAccept').click()")
                    wait_for(lambda: window.evaluate_js("app.batch.state.task?.status === 'cancelled'"))
                assert set(service.jobs) == jobs_before_cancel
                selected_folder = Path(folder) / 'folder-pick'
                selected_folder.mkdir()
                shutil.copyfile(source, selected_folder / 'folder-video.mp4')
                with patch.object(window, 'create_file_dialog', return_value=[str(selected_folder)]):
                    window.evaluate_js("$('batchFolder').click()")
                    wait_for(lambda: window.evaluate_js("!app.busy && app.media.path.endsWith('folder-video.mp4')"))
                assert window.evaluate_js("$('batchSelect').options.length === 2 && !$('batchPanel').hidden")
                window.events.closing += api._on_closing
                window.destroy()
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmation').dispatchEvent(new Event('cancel', {cancelable:true}));")
                wait_for(lambda: window.evaluate_js("!closePending"))
                assert not service.closing and not api._closed_safely
                window.destroy()
                wait_for(lambda: window.evaluate_js("$('confirmation').open"))
                window.evaluate_js("$('confirmAccept').click()")
                wait_for(lambda: api._closed_safely)
                assert service.closing and not service.worker.is_alive()
                print(json.dumps({"passed": True, "batch_selection_preview_and_queue": True, "native_close_confirmation": True, "close_error_recovery": True, "bridge": True, "custom_neutral": True,
                                  "independent_processing_groups": True, "denoise_off_model": True,
                                  "edit_reservation": True, "remove_confirmation": True, "history": True,
                                  "navigation_focus":True, "modal_focus":True, "keyboard_move":True,
                                  "serialized_settings":True, "native_controls":True,
                                  "setup_gate":True, "setup_error_details":True, "adaptive_loading_readonly_override":True,
                                  "actionable_decoder_error":True, "failed_import_clears_source":True,
                                  "preparation_gate_and_inline_details":True, "error_scroll_and_focus":True,
                                  "measured_wait_progress":True,
                                  "manual_color_disclosure":True, "preview_media_decode":True, "compare_frame_sync":True,
                                  "source_timeline_and_steps":True, "single_frame_preview":True, "compare_playback":True,
                                  "wipe_drag_and_pan":True, "preview_snapshot_commit":True}))
            except Exception as exc:
                failures.append(exc)
                print(ascii(window.evaluate_js("JSON.stringify({position:$('previewStart').value,time:$('sourceVideo').currentTime,step:$('previewStep').value,media:app.media,view:app.compare.state.view,ready:$('sourceVideo').readyState,nextDisabled:$('previewNext').disabled})")))
                print(ascii(window.evaluate_js("JSON.stringify({preparation:app.snapshot.preparation,setup:app.snapshot.setup.status,startDisabled:$('start').disabled,prepareText:$('enginePreparationStatus').textContent,previewDisabled:$('openPreview').disabled})")))
                print(ascii(window.evaluate_js("JSON.stringify({error:$('errorMessage').textContent,dialog:$('confirmation').open,result:$('confirmation').returnValue,jobs:app.snapshot.jobs.map(j=>[j.id,j.status]),pending:[...app.pendingActions],refreshing:!!app.refreshing,busy:app.busy,focus:document.activeElement.tagName+':'+document.activeElement.id,media:[$('compareOriginal'),$('compareEnhanced')].map(v=>({ready:v.readyState,network:v.networkState,error:v.error?.message,time:v.currentTime,seeking:v.seeking,visibility:document.visibilityState}))})")))
            finally:
                if not api._closed_safely:
                    api._closed_safely = True
                    window.destroy()

        try:
            webview.start(checks, gui="edgechromium", debug=False)
        finally:
            service.shutdown()
            if fixture:
                fixture.doCleanups()
        if failures:
            raise failures[0]


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True) if len(sys.argv) > 1 else None)
