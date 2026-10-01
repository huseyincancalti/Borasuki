"""Shell failures must not corrupt completed jobs or run visible terminals."""

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

import test_queue
from borasuki import windows
from borasuki.i18n import load_catalog, translate


class WindowsTests(unittest.TestCase):
    setUp = test_queue.QueueTests.setUp
    create = test_queue.QueueTests.create

    def test_notification_failure_does_not_fail_output_or_stop_next_job(self):
        first, second = self.create('first'), self.create('second')
        rendered = []

        def render(job, stop, update):
            rendered.append(job['id'])
            update(status='completed')
            if job['id'] == second:
                self.service.closing = True

        self.service.save_settings({'notifications': True})
        with patch('borasuki.service.execute', side_effect=render), patch('borasuki.windows.notify', side_effect=OSError('toast unavailable')) as notify:
            self.service._loop()
        self.assertEqual(rendered, [first, second])
        self.assertEqual(notify.call_count, 2)
        for identifier in rendered:
            job = self.service.jobs[identifier]
            self.assertEqual(job['status'], 'completed')
            self.assertIsNone(job['error'])
            self.assertIn('finished', job)

    def test_context_command_is_quoted_localized_and_per_user(self):
        for language in ('tr', 'en'):
            with patch.object(windows, 'winreg') as registry, patch.object(windows.sys, 'executable', 'C:/Python Space/python.exe'):
                windows.context_menu(True, language)
            commands = [call.args[4] for call in registry.SetValueEx.call_args_list if call.args[1] == '' and '%1' in call.args[4]]
            self.assertEqual(len(commands), len(windows.EXTENSIONS))
            self.assertTrue(all(command.startswith('"C:\\Python Space\\pythonw.exe" ') and command.endswith(' "%1"') for command in commands))
            labels = [call.args[4] for call in registry.SetValueEx.call_args_list if call.args[1] == '' and '%1' not in call.args[4]]
            self.assertEqual(set(labels), {translate(load_catalog(language), 'menu.upscale')})
            self.assertTrue(all(call.args[0] is registry.HKEY_CURRENT_USER for call in registry.CreateKey.call_args_list))

    def test_toast_passes_filename_as_data_not_powershell_code(self):
        filename = 'video & <tag> $env:USERNAME; test.mkv'
        with patch.object(windows, 'winreg'), patch.object(windows.subprocess, 'run', return_value=MagicMock(returncode=0)) as run:
            windows.notify(filename, 'tr', self.service.data)
        payload = json.loads((self.service.data / 'notification.json').read_text(encoding='utf-8'))
        self.assertIn(filename, payload['body'])
        args = run.call_args.args[0]
        self.assertNotIn(filename, args)
        self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 15)

    def test_packaged_context_menu_launches_exe_not_missing_python_or_main(self):
        with patch.object(windows, 'winreg') as registry, \
                patch.object(windows.sys, 'frozen', True, create=True), \
                patch.object(windows.sys, 'executable', 'C:/App Space/Borasuki.exe'):
            windows.context_menu(True, 'en')
        commands = [call.args[4] for call in registry.SetValueEx.call_args_list
                    if call.args[1] == '' and '%1' in call.args[4]]
        self.assertEqual(len(commands), len(windows.EXTENSIONS))
        self.assertEqual(set(commands), {'"C:/App Space/Borasuki.exe" "%1"'})
