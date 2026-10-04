"""Build exact committed app inputs plus private licensed components.

The output is an owner-private generated bundle with bounded input/output hashes.
Paid source and compiled bundles never enter the public Git archive. No service
configuration, credentials, recordings or database is read or modified.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'receiver'))
import ui_assets

INPUT_FILE_LIMIT = 1024 * 1024
INPUT_TOTAL_LIMIT = 8 * 1024 * 1024
INPUT_COUNT_LIMIT = 256
VENDOR_EXTENSIONS = {'.tsx', '.ts', '.css', '.woff2', '.md', '.json'}


def private_root(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('Canonical absolute owner-private directory required')
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError('Canonical absolute owner-private directory required')
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Canonical absolute owner-private directory required')
    return path


def committed_sources(project, revision):
    archive = subprocess.check_output(['git', 'archive', '--format=tar', revision, 'web'],
                                      cwd=project, timeout=15)
    if len(archive) > INPUT_TOTAL_LIMIT:
        raise ValueError('Committed frontend archive exceeds budget')
    result, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for count, member in enumerate(bundle.getmembers(), 1):
            path = Path(member.name)
            if (count > INPUT_COUNT_LIMIT * 2 or not path.parts or path.parts[0] != 'web'
                    or path.is_absolute() or '..' in path.parts
                    or not (member.isdir() or member.isfile())
                    or any(p in ('vendor', 'node_modules', 'dist') for p in path.parts)):
                raise ValueError('Unsafe committed frontend input')
            if member.isfile():
                total += member.size
                if member.size > INPUT_FILE_LIMIT or total > INPUT_TOTAL_LIMIT or len(result) >= INPUT_COUNT_LIMIT:
                    raise ValueError('Committed frontend inputs exceed budget')
                result[path.as_posix()] = bundle.extractfile(member).read()
    if not {'web/package.json', 'web/package-lock.json', 'web/src/main.tsx'} <= result.keys():
        raise ValueError('Complete locked frontend inputs required')
    return result


def licensed_sources(root):
    root = private_root(root)
    result, total = {}, 0
    for count, path in enumerate(sorted(root.rglob('*')), 1):
        if count > INPUT_COUNT_LIMIT * 2 or path.is_symlink():
            raise ValueError('Unsafe licensed component input')
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if (not path.is_file() or (path.suffix not in VENDOR_EXTENSIONS and relative != 'SHA256SUMS') or info.st_uid != os.getuid()
                or not 0 < info.st_size <= INPUT_FILE_LIMIT or any(p in ('node_modules', 'dist') for p in path.parts)):
            raise ValueError('Unsafe licensed component input')
        data = path.read_bytes()
        total += len(data)
        if len(result) >= INPUT_COUNT_LIMIT or total > INPUT_TOTAL_LIMIT:
            raise ValueError('Licensed component inputs exceed budget')
        result[relative] = data
    if not result:
        raise ValueError('Licensed component closure required')
    return result


def _run(command, cwd, log):
    with log.open('wb') as stream:
        subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=300)


def build(project, output_root, vendor_root, runner=None):
    project = Path(project).resolve(strict=True)
    output_root = private_root(output_root)
    if output_root.resolve().is_relative_to(project) and not output_root.resolve().is_relative_to(project / '.tmp'):
        raise ValueError('Generated licensed bundle must stay private and outside tracked source')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=project, timeout=10).decode().strip()
    if not ui_assets.REVISION.fullmatch(revision):
        raise ValueError('Exact source revision required')
    sources = committed_sources(project, revision)
    vendors = licensed_sources(vendor_root)
    destination = output_root / revision
    if destination.exists():
        raise ValueError('Existing private build must not be overwritten')
    run = _run if runner is None else runner
    with tempfile.TemporaryDirectory(prefix='.viewer-build-', dir=output_root) as folder:
        staging = Path(folder)
        for relative, data in sources.items():
            path = staging / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        for relative, data in vendors.items():
            path = staging / 'web/vendor' / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        web = staging / 'web'
        output = staging / 'bundle'
        try:
            run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], web, staging / 'install.log')
            run(['npm', 'run', 'build', '--', '--outDir', str(output)], web, staging / 'build.log')
        except (OSError, subprocess.SubprocessError):
            failure = Path(tempfile.mkdtemp(prefix='viewer-build-failure-', dir=output_root))
            for name in ('install.log', 'build.log'):
                log = staging / name
                if log.is_file() and log.stat().st_size <= 2 * 1024 * 1024:
                    shutil.copyfile(log, failure / name)
                    (failure / name).chmod(0o600)
            raise RuntimeError('Frontend build failed; private diagnostics: ' + str(failure)) from None
        if not output.is_dir() or output.is_symlink():
            raise ValueError('Frontend build produced no bundle')
        assets, total = {}, 0
        for count, path in enumerate(output.rglob('*'), 1):
            if count > ui_assets.MAX_ASSETS * 2 + 16 or path.is_symlink():
                raise ValueError('Unsafe generated frontend output')
            if path.is_dir():
                continue
            name = path.relative_to(output).as_posix()
            size = path.stat().st_size
            total += size
            if (not ui_assets.viewer_asset_name(name) or not 0 < size <= ui_assets.MAX_FILE
                    or total > ui_assets.MAX_TOTAL or len(assets) >= ui_assets.MAX_ASSETS):
                raise ValueError('Generated frontend output exceeds allowed paths or budget')
            assets[name] = path.read_bytes()
        metadata = {'version': 1, 'source_revision': revision,
                    'source_files': {n: ui_assets.digest(d) for n, d in sources.items()},
                    'vendor_files': {n: ui_assets.digest(d) for n, d in vendors.items()},
                    'asset_files': {n: ui_assets.digest(d) for n, d in assets.items()}}
        (output / 'build-provenance.json').write_text(json.dumps(metadata, sort_keys=True, separators=(',', ':')))
        output.chmod(0o700)
        ui_assets.read_build(output, revision)
        # The frozen temporary input snapshot is owned by this build only. Move
        # only the validated output; TemporaryDirectory cleans its own remainder.
        os.rename(output, destination)
        for path in destination.rglob('*'):
            path.chmod(0o500 if path.is_dir() else 0o400)
    return {'source_revision': revision, 'frontend_build': str(destination),
            'source_inputs': len(sources), 'licensed_inputs': len(vendors),
            'asset_count': len(assets), 'asset_bytes': total,
            'source_maps': False, 'source_distribution': 'private_ignored_vendor'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--vendor-root', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(build(args.project, args.output_root, args.vendor_root)))
