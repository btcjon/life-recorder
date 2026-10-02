import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import health
spec = importlib.util.spec_from_file_location('package_release', Path(__file__).resolve().parents[1] / 'scripts/package-receiver-release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.project = self.base/'project'
        (self.project/'receiver').mkdir(parents=True)
        (self.project/'receiver/health.py').write_text('VALUE = "startup"\n')
        (self.project/'receiver/receiver.py').write_text('import health\n')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('add', 'receiver')
        self.git('commit', '-qm', 'initial')
        self.output = self.base/'releases'
        self.output.mkdir(mode=0o700)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in self.base.rglob('*'):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.project, stderr=subprocess.DEVNULL).decode().strip()

    def packaged(self):
        result = release.package(self.project, self.output)
        root = Path(result['receiver_script']).parent
        return result, root

    def test_verified_release_is_git_independent_and_no_bytecode(self):
        result, root = self.packaged()
        with patch.object(subprocess, 'run', side_effect=AssertionError('Git must not run')):
            identity = health.source_identity(root, result['manifest_sha256'])
        self.assertEqual(identity['state'], 'verified')
        self.assertEqual(identity['revision'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(identity['source_file_count'], 2)
        self.assertFalse(list(root.rglob('*.pyc')))
        self.assertEqual(root.stat().st_mode & 0o222, 0)
        with self.assertRaises(ValueError):
            release.package(self.project, self.output)

    def test_mutable_dirty_untracked_and_deleted_inputs_fail_closed(self):
        (self.project/'receiver/health.py').write_text('changed\n')
        with self.assertRaises(ValueError):
            release.package(self.project, self.output)
        self.git('restore', 'receiver/health.py')
        (self.project/'receiver/extra.py').write_text('untracked\n')
        with self.assertRaises(ValueError):
            release.package(self.project, self.output)
        (self.project/'receiver/extra.py').unlink()
        (self.project/'receiver/health.py').unlink()
        with self.assertRaises(ValueError):
            release.package(self.project, self.output)

    def test_manifest_pin_mutability_bytes_additions_deletions_and_symlinks(self):
        result, root = self.packaged()
        pin = result['manifest_sha256']
        self.assertEqual(health.source_identity(root, '0'*64)['state'], 'unavailable')
        source = root/'health.py'
        source.chmod(0o644)
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_source_invalid')
        source.write_text('changed\n')
        source.chmod(0o444)
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_source_changed')
        source.chmod(0o644)
        source.write_text('VALUE = "startup"\n')
        source.chmod(0o444)
        root.chmod(0o755)
        (root/'extra.py').write_text('extra\n')
        (root/'extra.py').chmod(0o444)
        root.chmod(0o555)
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_source_changed')
        root.chmod(0o755)
        (root/'extra.py').unlink()
        (root/'receiver.py').unlink()
        root.chmod(0o555)
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_source_changed')
        root.chmod(0o755)
        (root/'receiver.py').symlink_to(source)
        root.chmod(0o555)
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_symlink')

    def test_checkout_head_moving_during_package_does_not_change_release(self):
        initial = self.git('rev-parse', 'HEAD')
        actual = subprocess.check_output
        def moving(args, **kwargs):
            if args[1] == 'archive':
                (self.project/'receiver/health.py').write_text('VALUE = "later"\n')
                actual(['git','add','receiver'], cwd=self.project)
                actual(['git','commit','-qm','later'], cwd=self.project)
            return actual(args, **kwargs)
        with patch.object(subprocess, 'check_output', side_effect=moving):
            result, root = self.packaged()
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), initial)
        self.assertEqual(result['source_revision'], initial)
        self.assertIn('startup', (root/'health.py').read_text())
        self.assertEqual(health.source_identity(root, result['manifest_sha256'])['revision'], initial)

    def test_package_failure_timeout_and_unmanaged_sources_never_attest(self):
        with patch.object(subprocess, 'check_output', side_effect=subprocess.TimeoutExpired('git', 10)):
            with self.assertRaises(subprocess.TimeoutExpired):
                release.package(self.project, self.output)
        self.assertEqual(health.source_identity(self.project/'receiver', '')['state'], 'unavailable')
        result, root = self.packaged()
        (root/'source-manifest.json').chmod(0o644)
        (root/'source-manifest.json').write_text('{}')
        (root/'source-manifest.json').chmod(0o444)
        pin = hashlib.sha256(b'{}').hexdigest()
        self.assertEqual(health.source_identity(root, pin)['reason'], 'release_manifest_invalid')

    def test_bytecode_cache_and_read_failure_are_not_loaded_source_proof(self):
        result, root = self.packaged()
        with patch.object(Path, 'read_bytes', side_effect=OSError('private path')):
            self.assertEqual(health.source_identity(root, result['manifest_sha256'])['reason'], 'release_manifest_unavailable')
        root.chmod(0o755)
        (root/'health.pyc').write_bytes(b'cache')
        (root/'health.pyc').chmod(0o444)
        root.chmod(0o555)
        self.assertEqual(health.source_identity(root, result['manifest_sha256'])['reason'], 'release_bytecode_cache')
        root.chmod(0o755)
        (root/'health.pyc').unlink()
        (root/'health.so').write_bytes(b'not part of the source release')
        (root/'health.so').chmod(0o444)
        root.chmod(0o555)
        self.assertEqual(health.source_identity(root, result['manifest_sha256'])['reason'], 'release_extra_file')

    def test_default_module_path_rejects_symlinked_release_and_resolution_failure(self):
        result, root = self.packaged()
        pin = result['manifest_sha256']
        with patch.object(health, '__file__', str(root/'health.py')):
            self.assertEqual(health.source_identity(manifest_pin=pin)['state'], 'verified')
            with patch.object(Path, 'resolve', side_effect=OSError('private filesystem error')):
                identity = health.source_identity(manifest_pin=pin)
                self.assertEqual(identity['state'], 'unavailable')
                self.assertNotIn('private', json.dumps(identity))
        alias = self.base/'release-link'
        alias.symlink_to(root.parent, target_is_directory=True)
        with patch.object(health, '__file__', str(alias/'receiver/health.py')):
            self.assertEqual(health.source_identity(manifest_pin=pin)['reason'], 'release_symlink')
        direct = self.base/'receiver-link'
        direct.symlink_to(root, target_is_directory=True)
        with patch.object(health, '__file__', str(direct/'health.py')):
            self.assertEqual(health.source_identity(manifest_pin=pin)['reason'], 'release_symlink')
