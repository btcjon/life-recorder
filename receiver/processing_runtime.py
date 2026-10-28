"""Engine readiness and operation diagnostics. Never records tool output or text."""
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import asr
import decoded_audio


@contextlib.contextmanager
def operation(inbox, name):
    started = time.monotonic()
    if inbox is not None:
        inbox.processing_runtime['active_operation'] = name
    try:
        yield
    except Exception as error:
        if name == 'engine_transcription' and isinstance(error, OSError):
            name = 'engine_launch'
        code = getattr(error, 'returncode', None)
        logging.warning('processing operation=%s exception=%s errno=%s exit=%s signal=%s seconds=%.3f',
                        name, type(error).__name__, getattr(error, 'errno', None),
                        code if code is not None and code >= 0 else None,
                        -code if code is not None and code < 0 else None, time.monotonic() - started)
        if isinstance(error, (asr.ProcessingFailure, decoded_audio.EmptyDecodeError)):
            raise
        kind = 'permanent' if name == 'source_access' and isinstance(error, FileNotFoundError) else 'transient'
        raise asr.ProcessingFailure(name, name + '_failed', kind) from error
    finally:
        if inbox is not None:
            inbox.processing_runtime['active_operation'] = None


def preflight(config):
    """Launch required tools before any recording consumes its retry budget."""
    if not config.ffmpeg:
        raise ValueError('engine_unavailable')
    commands = [[str(config.ffmpeg), '-version']]
    if config.engine == 'parakeet':
        cli = Path(config.parakeet_cli or asr.DEFAULT_PARAKEET_CLI)
        pin = os.environ.get('LIFE_RECORDER_ENGINE_MANIFEST_SHA256')
        if pin:
            raw = (cli.parent / 'engine-manifest.json').read_bytes()
            if hashlib.sha256(raw).hexdigest() != pin:
                raise ValueError('engine_manifest_mismatch')
            manifest = json.loads(raw)
            for relative, expected in manifest['files'].items():
                path = cli.parent / relative
                if not path.resolve().is_relative_to(cli.parent.resolve()) or path.is_symlink():
                    raise ValueError('engine_path_invalid')
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError('engine_hash_mismatch')
        model = config.parakeet_model_dir or asr.DEFAULT_PARAKEET_MODEL_DIR
        if not Path(model).is_dir() or not any(Path(model).iterdir()):
            raise ValueError('model_unavailable')
        commands.append([str(cli), '--help'])
    elif config.engine == 'whisper':
        if not config.model or not Path(config.model).is_file() or not config.whisper:
            raise ValueError('model_unavailable')
        commands.append([str(config.whisper), '--help'])
    elif config.engine == 'mlx':
        if not config.mlx_command or not config.mlx_model:
            raise ValueError('model_unavailable')
        commands.append([str(config.mlx_command), '--help'])
    else:
        raise ValueError('engine_unavailable')
    for command in commands:
        subprocess.run(command, check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def next_recording(db, now, recent_run):
    """One worker: up to three new arrivals, then one preserved backlog item."""
    cutoff = now - 24 * 3600
    recent = db.execute("SELECT * FROM chunks WHERE status='pending' AND retry_at<=? AND received>=? ORDER BY received,id LIMIT 1", (now, cutoff)).fetchone()
    older = db.execute("SELECT * FROM chunks WHERE status='pending' AND retry_at<=? AND received<? ORDER BY started,id LIMIT 1", (now, cutoff)).fetchone()
    if older and (not recent or recent_run >= 3):
        return older, 0
    return (recent, recent_run + 1) if recent else (None, recent_run)
