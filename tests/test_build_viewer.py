import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import ui_assets
spec = importlib.util.spec_from_file_location('build_viewer', Path(__file__).resolve().parents[1] / 'scripts/build-viewer.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class FrozenBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        (self.project/'web/src').mkdir(parents=True)
        for name, data in {'web/package.json': b'{}', 'web/package-lock.json': b'{}',
                           'web/src/main.tsx': b'// committed own source'}.items():
            (self.project/name).write_bytes(data)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', 'web')
        self.git('commit', '-qm', 'initial own source')
        self.vendor = self.root / 'private-vendor'
        self.vendor.mkdir(mode=0o700)
        (self.vendor/'button.tsx').write_bytes(b'// synthetic licensed component')
        self.output = self.root / 'private-builds'
        self.output.mkdir(mode=0o700)
        self.commands = []

    def tearDown(self):
        for path in self.root.rglob('*'):
            if path.is_dir():
                path.chmod(0o700)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.project, stderr=subprocess.DEVNULL).decode().strip()

    def runner(self, command, cwd, log):
        self.commands.append(command)
        self.assertEqual((cwd/'src/main.tsx').read_bytes(), b'// committed own source')
        self.assertEqual((cwd/'vendor/button.tsx').read_bytes(), b'// synthetic licensed component')
        log.write_bytes(b'fixture log')
        if command[1] == 'run':
            root = Path(command[-1])
            (root/'assets').mkdir(parents=True)
            (root/'index.html').write_bytes(b'<html>synthetic own app</html>')
            (root/'assets/app-123.js').write_bytes(b'console.log("synthetic fixture")')

    def test_frozen_committed_inputs_lockfile_vendor_and_artifacts_are_reconciled(self):
        (self.project/'web/src/main.tsx').write_bytes(b'// dirty checkout must not become deployed source')
        result = builder.build(self.project, self.output, self.vendor, self.runner)
        self.assertEqual(result['source_inputs'], 3)
        self.assertEqual(result['licensed_inputs'], 1)
        self.assertEqual(result['asset_count'], 2)
        self.assertEqual(self.commands[0], ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'])
        root = Path(result['frontend_build'])
        metadata, files = ui_assets.read_build(root, self.git('rev-parse', 'HEAD'))
        self.assertEqual(metadata['source_files']['web/src/main.tsx'], ui_assets.digest(b'// committed own source'))
        self.assertEqual(metadata['vendor_files']['button.tsx'], ui_assets.digest(b'// synthetic licensed component'))
        self.assertEqual(root.stat().st_mode & 0o077, 0)
        self.assertFalse((root/'vendor').exists())
        self.assertFalse(list(self.output.glob('.viewer-build-*')))
        with self.assertRaises(ValueError):
            builder.build(self.project, self.output, self.vendor, self.runner)

    def test_unsafe_source_or_vendor_inputs_are_rejected_before_build(self):
        (self.vendor/'button.tsx').unlink()
        (self.vendor/'button.tsx').symlink_to(self.project/'web/src/main.tsx')
        with self.assertRaises(ValueError):
            builder.build(self.project, self.output, self.vendor, self.runner)
        self.assertEqual(self.commands, [])
        (self.vendor/'button.tsx').unlink()
        (self.vendor/'button.tsx').write_bytes(b'component')
        (self.project/'web/vendor').mkdir()
        (self.project/'web/vendor/paid.tsx').write_bytes(b'not for Git distribution')
        self.git('add', 'web')
        self.git('commit', '-qm', 'unsafe synthetic input')
        with self.assertRaises(ValueError):
            builder.build(self.project, self.output, self.vendor, self.runner)

    def test_generated_maps_unlisted_paths_and_missing_entry_fail_closed(self):
        def with_map(command, cwd, log):
            self.runner(command, cwd, log)
            if command[1] == 'run':
                (Path(command[-1])/'assets/app.js.map').write_bytes(b'synthetic source map')
        with self.assertRaises(ValueError):
            builder.build(self.project, self.output, self.vendor, with_map)
        self.assertFalse((self.output/self.git('rev-parse', 'HEAD')).exists())

    def test_install_failure_keeps_only_private_diagnostics(self):
        def failure(command, cwd, log):
            log.write_bytes(b'synthetic dependency installation error')
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaisesRegex(RuntimeError, 'private diagnostics'):
            builder.build(self.project, self.output, self.vendor, failure)
        retained = list(self.output.glob('viewer-build-failure-*'))
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0]/'install.log').read_bytes(), b'synthetic dependency installation error')
        self.assertEqual(retained[0].stat().st_mode & 0o077, 0)
        self.assertFalse(list(self.output.glob('.viewer-build-*')))


if __name__ == '__main__':
    unittest.main()
