"""Build a private readonly launch release from an exact clean Git commit.

Packages source only, not recordings, credentials or databases. Does not edit
launch configuration, restart jobs, or overwrite any existing release.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


def package(project, output_root):
    project, output_root = Path(project).resolve(), Path(output_root)
    if not output_root.is_absolute() or output_root.is_symlink():
        raise ValueError('Private absolute output root required')
    if any(component.is_symlink() for component in output_root.parents):
        raise ValueError('Use a canonical output path without symlinked parents')
    output_root.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = output_root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Output root must be owner-private')
    if output_root.resolve().is_relative_to(project):
        raise ValueError('Release must stay outside Git')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=project, timeout=10)
    if git('status', '--porcelain', '--untracked-files=all', '--', 'receiver').strip():
        raise ValueError('Commit receiver changes before packaging')
    revision = git('rev-parse', 'HEAD').decode().strip()
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise ValueError('Invalid source revision')
    destination = output_root / revision
    if destination.exists():
        raise ValueError('Release already exists; never overwrite it')
    archive = git('archive', '--format=tar', revision, 'receiver')
    if len(archive) > 6 * 1024 * 1024:
        raise ValueError('Receiver archive exceeds package budget')
    staging = Path(tempfile.mkdtemp(prefix='.receiver-release-', dir=output_root))
    try:
        def write(path, content):
            with path.open('wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        files, total = {}, 0
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            members = bundle.getmembers()
            if len(members) > 256:
                raise ValueError('Receiver archive entry limit')
            for item in members:
                path = Path(item.name)
                if (path.is_absolute() or '..' in path.parts or not path.parts
                        or path.parts[0] != 'receiver' or not (item.isdir() or item.isfile())):
                    raise ValueError('Unsafe source archive')
                target = staging / path
                if item.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if path.suffix != '.py' or item.size > 1024 * 1024:
                    raise ValueError('Only bounded Python source belongs in this release')
                total += item.size
                if total > 4 * 1024 * 1024:
                    raise ValueError('Receiver source limit')
                content = bundle.extractfile(item).read()
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                write(target, content)
                files[path.relative_to('receiver').as_posix()] = hashlib.sha256(content).hexdigest()
        if not 2 <= len(files) <= 128 or not {'health.py', 'receiver.py'} <= files.keys():
            raise ValueError('Incomplete receiver source')
        metadata = {'version': 1, 'source_revision': revision, 'source_files': files}
        raw = json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()
        write(staging / 'receiver/source-manifest.json', raw)
        for item in staging.rglob('*'):
            if item.is_file():
                item.chmod(0o444)
        for item in sorted((p for p in staging.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            item.chmod(0o555)
            descriptor = os.open(item, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        staging.chmod(0o555)
        descriptor = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(staging, destination)
        directory = os.open(output_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'source_revision': revision, 'receiver_script': str(destination/'receiver/receiver.py'),
                'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'source_files': len(files)}
    except Exception:
        # Only this owned staging directory can be removed, never an existing
        # release, project, home directory, or runtime root.
        if staging.exists():
            for item in staging.rglob('*'):
                if item.is_dir():
                    item.chmod(0o700)
            staging.chmod(0o700)
            shutil.rmtree(staging)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.project, args.output_root)))
