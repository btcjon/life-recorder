"""Freeze an existing FluidAudio CLI and adjacent bundles outside sync storage."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output_root.absolute()
    if root.is_symlink() or 'CloudStorage' in root.parts:
        raise ValueError('Private nonsynced runtime required')
    root.mkdir(mode=0o700, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('Owner-private runtime required')
    with tempfile.TemporaryDirectory(prefix='.engine-', dir=root) as folder:
        staging = Path(folder)
        source = args.cli.resolve(strict=True)
        shutil.copyfile(source, staging / 'fluidaudiocli')
        for resource in source.parent.iterdir():
            if resource.suffix in ('.bundle', '.framework'):
                shutil.copytree(resource, staging / resource.name, symlinks=False)
        cli = staging / 'fluidaudiocli'
        cli.chmod(0o500)
        architecture = subprocess.check_output(['lipo', '-archs', str(cli)], timeout=15).decode().strip()
        if platform.machine() not in architecture.split():
            raise ValueError('architecture_mismatch')
        for command in (['codesign', '--verify', '--deep', '--strict', str(cli)], [str(cli), '--help']):
            subprocess.run(command, check=True, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        files = {p.relative_to(staging).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(staging.rglob('*')) if p.is_file()}
        raw = json.dumps({'version': 1, 'architecture': architecture, 'files': files}, sort_keys=True).encode()
        fingerprint = hashlib.sha256(raw).hexdigest()
        destination = root / ('fluidaudio-' + fingerprint[:20])
        if destination.exists():
            raise ValueError('Existing artifact must not be overwritten')
        (staging / 'engine-manifest.json').write_bytes(raw)
        for path in staging.rglob('*'):
            path.chmod(0o500 if path.is_dir() or path.name == 'fluidaudiocli' else 0o400)
        staging.chmod(0o500)
        os.rename(staging, destination)
        print(json.dumps({'directory': str(destination), 'manifest_sha256': fingerprint, 'files': len(files), 'architecture': architecture}))


if __name__ == '__main__':
    main()
