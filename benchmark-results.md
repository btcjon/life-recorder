# ASR head-to-head: mlx-whisper large-v3-turbo vs FluidAudio Parakeet TDT v3

Date: 2026-09-19. Host: Apple M2, 24 GB unified memory, arm64, macOS 27.0 (Build 26A428). One complete public-audio run. No Life Recorder recordings or transcripts were used.

## Outcome

FluidAudio Parakeet TDT v3 (batch CoreML) was faster and slightly more accurate on this 3-clip, 53.54 s set. Concatenated normalized WER: **3.23% Parakeet vs 4.84% mlx-whisper**. Overall real-time factor (wall / audio): **cold 0.050 vs 0.372**, **warm-process 0.032 vs 0.250**. Parakeet in-process inference after model load was **0.018 RTF (55x)**. Errors were only on the LibriSpeech clip; JFK and the Australian telephone clip were 0 WER for both.

## Hardware and software

| Item | Value |
| --- | --- |
| CPU/GPU | Apple M2 |
| Memory | 24.0 GB |
| OS | macOS 27.0 Build 26A428 |
| Python | `.transcription-venv` CPython 3.12.12 |
| mlx / mlx-metal | 0.32.2 |
| mlx-whisper | 0.4.3 |
| mlx model | `mlx-community/whisper-large-v3-turbo` snapshot `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` |
| FluidAudio | git `b68f484789d81fda21efbf81e2ca9fcfd9dc22aa` (clone 2026-09-19) |
| FluidAudio CLI | `swift build -c release --product fluidaudiocli` -> `work/FluidAudio/.build/out/Products/Release/fluidaudiocli` |
| Parakeet model | `~/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3` (Preprocessor/Encoder/Decoder/JointDecisionv3 CoreML) |
| ffmpeg | 9.0.1 |
| jiwer | 4.0.0 |
| Swift | 6.4 (swiftlang-6.4.0.34.1) |

Homebrew `parakeet-cli` is whisper.cpp 1.9.4 ggml, not FluidAudio. It was not used.

## Commands

```sh
# FluidAudio CLI (gitignored clone)
git clone --depth 1 https://github.com/FluidInference/FluidAudio.git work/FluidAudio
cd work/FluidAudio && swift build -c release --product fluidaudiocli

# Benchmark (from repo root; uses project .transcription-venv)
.transcription-venv/bin/python scripts/asr_head_to_head.py
```

mlx-whisper per sample, fresh process, English, temperature 0:

```text
mlx_whisper.transcribe(wav, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en", temperature=0.0)
# first call = cold (includes model load); second call = warm (ModelHolder cache)
```

FluidAudio per sample, two CLI processes, batch TDT v3:

```sh
work/FluidAudio/.build/out/Products/Release/fluidaudiocli transcribe AUDIO.wav \
  --model-version v3 \
  --model-dir "$HOME/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3" \
  --language en \
  --output-json OUT.json
```

First CLI process = cold wall (CoreML load + infer). Second CLI process = warm-process wall (still reloads models). JSON `processingTimeSeconds` = in-process inference after load.

WER: jiwer 4 `process_words` with lowercase, strip punctuation, collapse spaces. RTF = wall_s / audio_s. RTFx = audio_s / wall_s.

## Inputs (public, labeled, 16 kHz mono WAV)

| ID | Duration | Source | Character |
| --- | ---: | --- | --- |
| 01_jfk | 11.000 s | whisper.cpp `jfk.wav` | historical US speech, analog-era noise |
| 02_librispeech | 29.400 s | HF `hf-internal-testing/librispeech_asr_dummy` id `1272-128104-0004` | clean US audiobook |
| 03_minds14_en_au | 13.141 s | HF `PolyAI/minds14` `en-AU~BUSINESS_LOAN/response_20.wav` | Australian English, telephone |

A 57.4 s MINDS-14 clip (`ADDRESS/response_13.wav`) was discarded because the published transcription is truncated versus the audio. Both engines transcribed additional spoken content after the official label.

## Per-sample results

| Sample | Audio s | mlx WER | mlx cold s (RTF) | mlx warm s (RTF) | Parakeet WER | Parakeet cold s (RTF) | Parakeet warm-process s (RTF) | Parakeet infer s (RTF) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 01_jfk | 11.000 | 0.000 | 6.930 (0.630) | 3.577 (0.325) | 0.000 | 0.962 (0.087) | 0.391 (0.036) | 0.179 (0.016) |
| 02_librispeech | 29.400 | 0.088 | 6.328 (0.215) | 5.678 (0.193) | 0.059 | 0.953 (0.032) | 0.814 (0.028) | 0.585 (0.020) |
| 03_minds14_en_au | 13.141 | 0.000 | 6.646 (0.506) | 4.107 (0.313) | 0.000 | 0.758 (0.058) | 0.498 (0.038) | 0.204 (0.016) |

LibriSpeech substitutions after normalization (only non-zero WER clip): both engines mapped `at em` -> `adam` and `birket` -> `burkett`. mlx also mapped `mister` -> `mr`. That is 6/68 vs 4/68 word errors.

## Aggregates (3 clips, 53.541 s audio)

| Engine / timing | Mean WER | Concat WER | Sum wall s | Overall RTF | Overall RTFx |
| --- | ---: | ---: | ---: | ---: | ---: |
| mlx-whisper cold (load+infer) | 0.0294 | 0.0484 | 19.904 | 0.372 | 2.69x |
| mlx-whisper warm (in-process) | 0.0294 | 0.0484 | 13.362 | 0.250 | 4.01x |
| Parakeet cold process | 0.0196 | 0.0323 | 2.674 | 0.050 | 20.0x |
| Parakeet warm process | 0.0196 | 0.0323 | 1.703 | 0.032 | 31.4x |
| Parakeet infer after load | 0.0196 | 0.0323 | 0.968 | 0.018 | 55.3x |

Run window: 2026-09-19T13:43:51Z to 13:44:38Z. Raw JSON: `work/benchmark/out/results.json` (gitignored).

## Limitations

- n=3 clips, 54 s total; not a LibriSpeech/TED/earnings leaderboard.
- Single run, no averaging; other Mac load not controlled.
- mlx cold reloads weights in a new Python process per file. Parakeet warm-process still pays CoreML load because the CLI is one-shot; persistent in-process Parakeet would be closer to the infer column.
- English forced; no number/ITN normalization beyond punctuation/case.
- MINDS-14 labels can be incomplete; only a duration-matched complete label was kept.
- The live Life Recorder receiver now uses FluidAudio Parakeet. Homebrew `whisper-cli` and the isolated MLX environment remain available as fallback/benchmark paths.
- FluidAudio clone, CoreML models, and WAV fixtures live under `work/` or Application Support, not git.
