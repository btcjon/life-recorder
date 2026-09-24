"""Local ASR adapters. Standard library only; no cloud calls."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import decoded_audio
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PARAKEET_CLI = (
    Path(__file__).resolve().parents[1]
    / "work/FluidAudio/.build/out/Products/Release/fluidaudiocli"
)
DEFAULT_PARAKEET_MODEL_DIR = Path.home() / (
    "Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3"
)

# Exact parser messages. Anything else raised as ValueError stays retryable.
_MALFORMED_MESSAGES = {
    "Malformed Parakeet output": "asr_malformed_output",
    "Missing Parakeet output": "asr_missing_output",
    "Unexpected MLX output": "asr_unexpected_output",
    "Unexpected Whisper output": "asr_unexpected_output",
}
_PERMANENT_MESSAGES = {
    "ffmpeg is required": "engine_unavailable",
    "Unsupported engine": "engine_unavailable",
    "MLX engine requires command and model": "engine_unavailable",
    "Whisper engine requires model and whisper-cli": "engine_unavailable",
}


class ProcessingFailure(Exception):
    """Sanitized processing failure. The message is the stable code, never tool output."""

    def __init__(self, stage: str, code: str, kind: str):
        super().__init__(code)
        self.stage = stage
        self.code = code
        self.kind = kind


def classify_processing_failure(error: BaseException) -> tuple[str, str, str]:
    """Return stage, safe code, and kind: transient, malformed, or permanent.

    ValueError is permanent only for a missing local engine. Malformed model
    output and every other ValueError stay retryable.
    """
    if isinstance(error, ProcessingFailure):
        return error.stage, error.code, error.kind
    if isinstance(error, subprocess.TimeoutExpired):
        return "asr", "asr_timeout", "transient"
    if isinstance(error, subprocess.CalledProcessError):
        return "asr", "asr_failed", "transient"
    if isinstance(error, FileNotFoundError):
        return "decode", "source_missing", "permanent"
    if isinstance(error, TimeoutError):
        return "asr", "asr_timeout", "transient"
    if isinstance(error, sqlite3.OperationalError):
        return "commit", "storage_busy", "transient"
    if isinstance(error, OSError):
        return "decode", "decode_failed", "transient"
    if isinstance(error, json.JSONDecodeError):
        return "asr", "asr_malformed_output", "malformed"
    if isinstance(error, ValueError):
        message = str(error)
        if message in _PERMANENT_MESSAGES:
            return "asr", _PERMANENT_MESSAGES[message], "permanent"
        if message in _MALFORMED_MESSAGES:
            return "asr", _MALFORMED_MESSAGES[message], "malformed"
        return "processing", "processing_failed", "transient"
    return "processing", "processing_failed", "transient"


def _wav_usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


@dataclass(frozen=True)
class AsrConfig:
    engine: str
    ffmpeg: str | None
    model: Path | None = None
    whisper: str | None = None
    mlx_command: str | None = None
    mlx_model: str | None = None
    parakeet_cli: Path | None = None
    parakeet_model_dir: Path | None = None

    @property
    def model_label(self) -> str:
        if self.engine == "parakeet":
            return str(self.parakeet_model_dir or DEFAULT_PARAKEET_MODEL_DIR)
        if self.engine == "mlx":
            return str(self.mlx_model or "")
        return str(self.model or "")


def resolve_engine(engine: str | None, mlx_command: str | None, mlx_model: str | None) -> str:
    if engine:
        if engine not in ("parakeet", "mlx", "whisper"):
            raise ValueError("Unsupported engine")
        return engine
    if mlx_command and mlx_model:
        return "mlx"
    return "whisper"


def config_from_args(args) -> AsrConfig:
    engine = resolve_engine(
        getattr(args, "engine", None),
        getattr(args, "mlx_command", None),
        getattr(args, "mlx_model", None),
    )
    parakeet_cli = getattr(args, "parakeet_cli", None) or DEFAULT_PARAKEET_CLI
    parakeet_model_dir = getattr(args, "parakeet_model_dir", None) or DEFAULT_PARAKEET_MODEL_DIR
    return AsrConfig(
        engine=engine,
        ffmpeg=getattr(args, "ffmpeg", None),
        model=getattr(args, "model", None),
        whisper=getattr(args, "whisper", None),
        mlx_command=getattr(args, "mlx_command", None),
        mlx_model=getattr(args, "mlx_model", None),
        parakeet_cli=Path(parakeet_cli) if parakeet_cli else None,
        parakeet_model_dir=Path(parakeet_model_dir) if parakeet_model_dir else None,
    )


def _optional_number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Malformed Parakeet output")
    return float(value)


def parse_parakeet_output(path: Path) -> tuple[str, dict]:
    if not path.is_file():
        raise ValueError("Missing Parakeet output")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise ValueError("Malformed Parakeet output") from error
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        raise ValueError("Malformed Parakeet output")
    summary = {
        "modelVersion": data.get("modelVersion") if isinstance(data.get("modelVersion"), str) else None,
        "mode": data.get("mode") if isinstance(data.get("mode"), str) else None,
        "durationSeconds": _optional_number(data.get("durationSeconds")),
        "processingTimeSeconds": _optional_number(data.get("processingTimeSeconds")),
        "rtfx": _optional_number(data.get("rtfx")),
        "confidence": _optional_number(data.get("confidence")),
        "timingsConfirmed": data.get("timingsConfirmed") if isinstance(data.get("timingsConfirmed"), bool) else None,
        "wordCount": None,
    }
    words = data.get("wordTimings")
    parsed_words = []
    if words is not None:
        if not isinstance(words, list):
            raise ValueError("Malformed Parakeet output")
        for item in words:
            if not isinstance(item, dict) or not isinstance(item.get("word"), str):
                raise ValueError("Malformed Parakeet output")
            parsed_words.append({
                "word": item["word"],
                "startTime": _optional_number(item.get("startTime")),
                "endTime": _optional_number(item.get("endTime")),
            })
        summary["wordCount"] = len(parsed_words)
        summary["wordTimings"] = parsed_words[:10000]
    return data["text"], {key: value for key, value in summary.items() if value is not None}


def transcribe_chunk(row, config: AsrConfig, work: Path, clean_transcript) -> tuple[str, dict]:
    if not config.ffmpeg:
        raise ValueError("ffmpeg is required")
    token = uuid.uuid4().hex
    result_file = work / f"{row['id']}-{token}.json"
    prefix = work / f"{row['id']}-{token}"
    engine = config.engine
    lease = None
    try:
        try:
            lease = decoded_audio.acquire(
                config.ffmpeg, Path(row["path"]), str(row["id"]), work, run=subprocess.run,
            )
        except decoded_audio.EmptyDecodeError as error:
            raise ProcessingFailure("decode", "decode_empty", "transient") from error
        if not _wav_usable(lease.path):
            raise ProcessingFailure("decode", "decode_empty", "transient")
        wav = lease.path
        if engine == "parakeet":
            cli = config.parakeet_cli or DEFAULT_PARAKEET_CLI
            model_dir = config.parakeet_model_dir or DEFAULT_PARAKEET_MODEL_DIR
            argv = [
                str(cli), "transcribe", str(wav),
                "--model-version", "v3",
                "--model-dir", str(model_dir),
                "--language", "en",
                "--output-json", str(result_file),
            ]
            subprocess.run(
                argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
            )
            text, summary = parse_parakeet_output(result_file)
            cleaned = clean_transcript(text).strip()
            provenance = {
                "engine": "parakeet",
                "model": str(model_dir),
                "language": "en",
                "summary": summary,
            }
            return cleaned, provenance
        if engine == "mlx":
            if not config.mlx_command or not config.mlx_model:
                raise ValueError("MLX engine requires command and model")
            mlx_json = prefix.with_suffix(".json")
            subprocess.run(
                [config.mlx_command, str(wav), "--model", config.mlx_model,
                 "--output-dir", str(work), "--output-name", prefix.name,
                 "--output-format", "json", "--verbose", "False"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
            )
            output = json.loads(mlx_json.read_text(encoding="utf-8"))
            segments = output["segments"]
            if not isinstance(segments, list):
                raise ValueError("Unexpected MLX output")
            text = clean_transcript(" ".join(s["text"] for s in segments if isinstance(s, dict))).strip()
            mlx_json.unlink(missing_ok=True)
            return text, {"engine": "mlx", "model": str(config.mlx_model), "summary": {}}
        if not config.model or not config.whisper:
            raise ValueError("Whisper engine requires model and whisper-cli")
        whisper_json = prefix.with_suffix(".json")
        subprocess.run(
            [config.whisper, "-m", str(config.model), "-f", str(wav), "-l", "auto",
             "-oj", "-of", str(prefix), "-nt"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600,
        )
        output = json.loads(whisper_json.read_bytes().decode("utf-8", errors="replace"))
        segments = output["transcription"]
        if not isinstance(segments, list):
            raise ValueError("Unexpected Whisper output")
        text = clean_transcript(" ".join(s["text"] for s in segments if isinstance(s, dict))).strip()
        whisper_json.unlink(missing_ok=True)
        return text, {"engine": "whisper", "model": str(config.model), "summary": {}}
    finally:
        if lease is not None:
            lease.release()
        result_file.unlink(missing_ok=True)
        prefix.with_suffix(".json").unlink(missing_ok=True)
