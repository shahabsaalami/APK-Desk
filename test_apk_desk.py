import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import apk_desk as app

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = dict(app.DEFAULT['profiles']['Default'], archive=str(self.root/'archive'))
        self.source = self.root/'original.apk'
        self.source.write_bytes(b'APK test bytes')
        self.info = dict(path=str(self.source), app='Test: App', package='com.test', version='1.0',
                         code='1', bytes=self.source.stat().st_size, sha256=app.digest(self.source),
                         debuggable=False, permissions=[], min_sdk='23', target_sdk='35', architectures='arm64')
        self.sig = dict(verified=True, certificates=['abc'], debug_certificate=False, details='OK')
        self.sig_patch = patch.object(app, 'signature', return_value=self.sig)
        self.sig_patch.start()

    def tearDown(self):
        self.sig_patch.stop()
        self.temp.cleanup()

    def test_windows_names_and_unknown_fields(self):
        self.assertEqual(app.filename(self.info,self.profile), 'Test__App_1.0_1.apk')
        self.assertEqual(app.safe_component('CON'), '_CON')
        self.assertNotIn('/', app.safe_component('../escape'))
        with self.assertRaises(ValueError):
            app.filename(self.info, dict(self.profile, pattern='{missing}'))

    def test_archive_preserves_source_and_checksum(self):
        folder = app.archive_apk(self.info,self.profile,'Release note')
        stored = json.loads((folder/'release.json').read_text(encoding='utf-8'))
        self.assertEqual(app.digest(stored['archived_file']),self.info['sha256'])
        self.assertTrue(self.source.exists())
        self.assertEqual(stored['notes'],'Release note')
        self.assertIn('قبلاً', app.release_check(self.info,self.profile)['warnings'][0])
        newer = dict(self.info, code='0', bytes=100, debuggable=True)
        warnings = app.release_check(newer,self.profile)['warnings']
        self.assertEqual(len(warnings),3)

    def test_archive_rolls_back_failed_copy(self):
        changed = dict(self.info,sha256='wrong')
        with self.assertRaises(RuntimeError):
            app.archive_apk(changed,self.profile)
        self.assertEqual(list((self.root/'archive'/'com.test').iterdir()),[])
        self.assertTrue(self.source.exists())

    def test_permission_and_package_comparison(self):
        second = dict(self.info, package='com.other',permissions=['android.permission.CAMERA'])
        result = app.compare_apks(self.info, second)
        self.assertIn('package',result)
        self.assertEqual(result['permissions_added'],['android.permission.CAMERA'])
        self.assertTrue(result['same_certificates'])

    def test_worker_collision_and_undo(self):
        window = app.APKDesk([str(self.source)])
        window.withdraw()
        try:
            target = self.source.with_name(app.filename(self.info,self.profile))
            target.write_bytes(b'keep me')
            with patch.object(app,'inspect_apk',return_value=self.info), patch.object(app,'DATA',self.root/'data'):
                window.work('rename',[str(self.source)],self.profile,'')
                self.assertTrue(self.source.exists())
                self.assertEqual(target.read_bytes(),b'keep me')
                target.unlink()
                window.work('rename',[str(self.source)],self.profile,'')
                window.poll()
                self.assertFalse(self.source.exists())
                self.assertTrue(target.exists())
                window.undo()
                self.assertTrue(self.source.exists())
                self.assertFalse(target.exists())
        finally:
            window.pool.shutdown(wait=True)
            window.destroy()

    def test_adb_lists_only_ready_devices(self):
        output = ('List of devices attached\n'
                  'phone-1 device product:demo model:Pixel_Test transport_id:1\n'
                  'phone-2 unauthorized usb:1\n'
                  'emulator-5554 device product:sdk model:sdk_gphone64\n'
                  'phone-3 offline\n')
        with patch.object(app, 'run', return_value=output), patch.object(app, 'tool', return_value=Path('adb.exe')):
            devices = app.connected_devices()
        self.assertEqual([d['serial'] for d in devices], ['phone-1', 'emulator-5554'])
        self.assertIn('Pixel Test', devices[0]['label'])

    def test_adb_no_device_and_unauthorized_errors(self):
        for output in ('List of devices attached\n', 'List of devices attached\nphone unauthorized\n'):
            with patch.object(app, 'run', return_value=output), patch.object(app, 'tool', return_value=Path('adb.exe')):
                with self.assertRaisesRegex(RuntimeError, 'USB debugging'):
                    app.connected_devices()

    def test_install_uses_explicit_device_and_preserves_data(self):
        with patch.object(app, 'tool', return_value=Path('adb.exe')), patch.object(app, 'run', return_value='Performing Streamed Install\nSuccess\n') as command:
            result = app.install_apk(self.source, 'phone-1')
            command.assert_called_once_with([Path('adb.exe'), '-s', 'phone-1', 'install', '-r', self.source.resolve()], timeout=300)
        self.assertIn('انجام شد', result)

    def test_install_failure_is_not_reported_as_success(self):
        with patch.object(app, 'tool', return_value=Path('adb.exe')), patch.object(app, 'run', return_value='Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]'):
            with self.assertRaisesRegex(RuntimeError, 'INSTALL_FAILED'):
                app.install_apk(self.source, 'phone-1')

    def test_install_worker_does_not_require_aapt(self):
        window = app.APKDesk()
        window.withdraw()
        try:
            with patch.object(app, 'inspect_apk', side_effect=AssertionError('aapt must not run')), patch.object(app, 'install_apk', return_value='Success') as install:
                window.work('install', [str(self.source)], {}, '', 'phone-1')
                install.assert_called_once_with(str(self.source), 'phone-1')
                events = []
                while not window.events.empty():
                    events.append(window.events.get_nowait())
                self.assertFalse(any('خطا' in str(value) for _,value in events))
                self.assertEqual(events[-1][0], 'done')
        finally:
            window.pool.shutdown(wait=True)
            window.destroy()

if __name__ == '__main__':
    unittest.main()
