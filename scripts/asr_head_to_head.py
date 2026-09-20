#!/usr/bin/env python3
"""Head-to-head ASR benchmark: mlx-whisper large-v3-turbo vs FluidAudio Parakeet TDT v3.

Public labeled audio only. Models/audio stay under work/ (gitignored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

from jiwer import process_words
from jiwer.transforms import (
    Compose,
    ReduceToListOfListOfWords,
    RemoveMultipleSpaces,
    RemovePunctuation,
    Strip,
    ToLowerCase,
)

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "work" / "benchmark"
AUDIO = BENCH / "audio"
REFS = BENCH / "refs"
OUT = BENCH / "out"

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FLUID_CLI = ROOT / "work" / "FluidAudio" / ".build" / "out" / "Products" / "Release" / "fluidaudiocli"
FLUID_MODEL = Path.home() / "Library" / "Application Support" / "FluidAudio" / "Models" / "parakeet-tdt-0.6b-v3"
VENV_PY = ROOT / ".transcription-venv" / "bin" / "python"

SAMPLES = [
    {
        "id": "01_jfk",
        "wav": AUDIO / "01_jfk.wav",
        "ref": REFS / "01_jfk.txt",
        "source": "whisper.cpp public fixture /opt/homebrew/share/whisper.cpp/jfk.wav",
        "notes": "11s historical US political speech; analog-era noise",
    },
    {
        "id": "02_librispeech",
        "wav": AUDIO / "02_librispeech.wav",
        "ref": REFS / "02_librispeech.txt",
        "source": "Hugging Face hf-internal-testing/librispeech_asr_dummy id=1272-128104-0004",
        "notes": "29.4s clean US audiobook (LibriSpeech test-clean speaker 1272)",
    },
    {
        "id": "03_minds14_en_au",
        "wav": AUDIO / "03_minds14_en_au.wav",
        "ref": REFS / "03_minds14_en_au.txt",
        "source": "Hugging Face PolyAI/minds14 en-AU BUSINESS_LOAN/response_20.wav",
        "notes": "13.1s Australian English telephone-quality banking query",
    },
]

NORMALIZE = Compose(
    [
        ToLowerCase(),
        RemovePunctuation(),
        RemoveMultipleSpaces(),
        Strip(),
        ReduceToListOfListOfWords(),
    ]
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def duration_s(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        text=True,
    )
    return float(out.strip())


def normalized_wer(ref: str, hyp: str) -> float:
    out = process_words(
        ref,
        hyp,
        reference_transform=NORMALIZE,
        hypothesis_transform=NORMALIZE,
    )
    return float(out.wer)


def mlx_one(wav: Path) -> dict:
    import mlx_whisper

    t0 = time.perf_counter()
    first = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=MLX_MODEL,
        language="en",
        temperature=0.0,
        verbose=False,
    )
    cold_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    second = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=MLX_MODEL,
        language="en",
        temperature=0.0,
        verbose=False,
    )
    warm_s = time.perf_counter() - t1

    return {
        "text": (second.get("text") or first.get("text") or "").strip(),
        "text_first": (first.get("text") or "").strip(),
        "cold_wall_s": cold_s,
        "warm_wall_s": warm_s,
    }


def run_mlx_sample(wav: Path) -> dict:
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(VENV_PY), str(Path(__file__).resolve()), "--mlx-one", str(wav)],
        check=True,
        capture_output=True,
        text=True,
    )
    proc_wall = time.perf_counter() - t0
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    data["process_wall_s"] = proc_wall
    if proc.stderr:
        data["stderr_tail"] = proc.stderr[-500:]
    return data


def run_fluid_once(wav: Path, json_path: Path) -> dict:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FLUID_CLI),
        "transcribe",
        str(wav),
        "--model-version",
        "v3",
        "--model-dir",
        str(FLUID_MODEL),
        "--language",
        "en",
        "--output-json",
        str(json_path),
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    payload = json.loads(json_path.read_text())
    return {
        "text": (payload.get("text") or proc.stdout.strip() or "").strip(),
        "wall_s": wall,
        "infer_s": payload.get("processingTimeSeconds"),
        "rtfx_cli": payload.get("rtfx"),
        "confidence": payload.get("confidence"),
        "cmd": cmd,
        "stderr_tail": (proc.stderr or "")[-500:],
    }


def sysinfo() -> dict:
    brand = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
    sw = subprocess.check_output(["sw_vers"], text=True).strip()
    return {
        "cpu": brand,
        "mem_gb": round(mem / (1024**3), 1),
        "arch": platform.machine(),
        "sw_vers": sw,
        "python": sys.version.split()[0],
        "venv_python": str(VENV_PY),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mlx-one", type=Path)
    args = parser.parse_args()
    if args.mlx_one:
        print(json.dumps(mlx_one(args.mlx_one)))
        return 0

    OUT.mkdir(parents=True, exist_ok=True)
    results = {
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": sysinfo(),
        "engines": {
            "mlx_whisper": {
                "package": "mlx-whisper 0.4.3",
                "mlx": "0.32.2",
                "model": MLX_MODEL,
                "invocation": "fresh process: mlx_whisper.transcribe language=en temperature=0",
            },
            "fluidaudio_parakeet_tdt_v3": {
                "cli": str(FLUID_CLI),
                "source": "FluidAudio git b68f484, swift build -c release --product fluidaudiocli",
                "model": str(FLUID_MODEL),
                "invocation": "fluidaudiocli transcribe --model-version v3 --language en batch",
            },
        },
        "samples": [],
        "per_engine": {},
    }

    for sample in SAMPLES:
        wav = sample["wav"]
        ref = sample["ref"].read_text().strip()
        dur = duration_s(wav)
        row = {
            "id": sample["id"],
            "source": sample["source"],
            "notes": sample["notes"],
            "wav": str(wav),
            "duration_s": dur,
            "sha256": sha256_file(wav),
            "reference": ref,
            "engines": {},
        }

        print(f"== mlx {sample['id']} ==", flush=True)
        mlx = run_mlx_sample(wav)
        mlx_wer = normalized_wer(ref, mlx["text"])
        row["engines"]["mlx_whisper"] = {
            **mlx,
            "wer": mlx_wer,
            "rtf_cold": mlx["cold_wall_s"] / dur,
            "rtf_warm": mlx["warm_wall_s"] / dur,
        }
        (OUT / f"{sample['id']}.mlx.txt").write_text(mlx["text"] + "\n")

        print(f"== fluid {sample['id']} ==", flush=True)
        cold = run_fluid_once(wav, OUT / f"{sample['id']}.fluid.cold.json")
        warm = run_fluid_once(wav, OUT / f"{sample['id']}.fluid.warm.json")
        fluid_wer = normalized_wer(ref, warm["text"])
        row["engines"]["fluidaudio_parakeet_tdt_v3"] = {
            "text": warm["text"],
            "text_cold": cold["text"],
            "cold_wall_s": cold["wall_s"],
            "warm_wall_s": warm["wall_s"],
            "cold_infer_s": cold["infer_s"],
            "warm_infer_s": warm["infer_s"],
            "cold_rtfx_cli": cold["rtfx_cli"],
            "warm_rtfx_cli": warm["rtfx_cli"],
            "wer": fluid_wer,
            "rtf_cold": cold["wall_s"] / dur,
            "rtf_warm_process": warm["wall_s"] / dur,
            "rtf_warm_infer": (warm["infer_s"] or 0) / dur,
            "confidence": warm["confidence"],
        }
        (OUT / f"{sample['id']}.fluid.txt").write_text(warm["text"] + "\n")
        results["samples"].append(row)
        print(
            f"{sample['id']}: mlx WER={mlx_wer:.4f} cold={mlx['cold_wall_s']:.3f}s warm={mlx['warm_wall_s']:.3f}s | "
            f"fluid WER={fluid_wer:.4f} cold={cold['wall_s']:.3f}s infer={warm['infer_s']}",
            flush=True,
        )

    def agg(engine: str, wall_key: str, rtf_key: str) -> dict:
        wers = [s["engines"][engine]["wer"] for s in results["samples"]]
        walls = [s["engines"][engine][wall_key] for s in results["samples"]]
        rtfs = [s["engines"][engine][rtf_key] for s in results["samples"]]
        durs = [s["duration_s"] for s in results["samples"]]
        hyp = " ".join(s["engines"][engine]["text"] for s in results["samples"])
        ref = " ".join(s["reference"] for s in results["samples"])
        return {
            "mean_wer": sum(wers) / len(wers),
            "concat_wer": normalized_wer(ref, hyp),
            "mean_wall_s": sum(walls) / len(walls),
            "sum_wall_s": sum(walls),
            "audio_s": sum(durs),
            "mean_rtf": sum(rtfs) / len(rtfs),
            "overall_rtf": sum(walls) / sum(durs),
        }

    results["per_engine"] = {
        "mlx_whisper_cold": agg("mlx_whisper", "cold_wall_s", "rtf_cold"),
        "mlx_whisper_warm": agg("mlx_whisper", "warm_wall_s", "rtf_warm"),
        "fluidaudio_cold_process": agg(
            "fluidaudio_parakeet_tdt_v3", "cold_wall_s", "rtf_cold"
        ),
        "fluidaudio_warm_process": agg(
            "fluidaudio_parakeet_tdt_v3", "warm_wall_s", "rtf_warm_process"
        ),
        "fluidaudio_warm_infer": agg(
            "fluidaudio_parakeet_tdt_v3", "warm_infer_s", "rtf_warm_infer"
        ),
    }
    results["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["per_engine"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
