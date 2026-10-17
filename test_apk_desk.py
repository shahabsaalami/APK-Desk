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

if __name__ == '__main__':
    unittest.main()
