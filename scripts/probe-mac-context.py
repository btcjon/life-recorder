#!/usr/bin/env python3
"""Compile and run one public CoreAudio output-IO sample.

Prints counts and capability flags only. It does not print bundle ids, device
ids, titles, or URLs, and it does not grant a browser permission.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "receiver"))

import context_observations
import mac_context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, help="Private cache. Default is a temporary directory.")
    args = parser.parse_args()
    owned = args.cache_dir
    temporary = None
    if owned is None:
        temporary = tempfile.TemporaryDirectory()
        owned = Path(temporary.name)
    try:
        binary = mac_context.helper_binary(owned, mac_context.HELPER_SOURCE)
        sample = mac_context.run_helper(binary)
    except Exception:
        sample = {"ok": False, "reason": "helper_unavailable", "processes": []}
    finally:
        if temporary is not None:
            temporary.cleanup()
    summary = {
        "helper_ok": bool(sample.get("ok")),
        "reason": sample.get("reason"),
        "output_io_count": len(sample.get("processes") or []),
        "measures_audible_sound": False,
        "browser_adapter": context_observations.BROWSER_ADAPTER,
    }
    json.dump(summary, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if summary["helper_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
