"""Inspect private original/enhanced pairs; never changes playback defaults."""
import argparse
import json
import subprocess
from pathlib import Path


def duration(path, ffprobe):
    result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'json', str(path)], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('audio_probe_failed')
    return float(json.loads(result.stdout)['format']['duration'])


def evaluate(pairs, ffprobe):
    valid = approved = 0
    failures = []
    seen = set()
    for index, pair in enumerate(pairs):
        try:
            identity = (str(Path(pair['original']).resolve()), str(Path(pair['enhanced']).resolve()))
        except (KeyError, TypeError, ValueError):
            failures.append({'pair': index, 'reason': 'pair_validation_failed'})
            continue
        if identity in seen or identity[0] == identity[1]:
            failures.append({'pair': index, 'reason': 'duplicate_or_identical_pair'})
            continue
        seen.add(identity)
        if pair.get('consented') is not True:
            failures.append({'pair': index, 'reason': 'consent_missing'})
            continue
        try:
            original = duration(Path(pair['original']), ffprobe)
            enhanced = duration(Path(pair['enhanced']), ffprobe)
            if original <= 0 or abs(original - enhanced) > 0.1:
                raise ValueError('duration_mismatch')
            valid += 1
            if all(pair.get(key) is True for key in ('level_matched', 'alignment_checked', 'no_clipping',
                                                     'speech_boundaries_preserved', 'user_approved')):
                approved += 1
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            failures.append({'pair': index, 'reason': 'pair_validation_failed'})
    return {'pairs': len(pairs), 'duration_valid': valid, 'listening_approved': approved,
            'failures': failures, 'default_change_ready': len(pairs) >= 12 and approved == len(pairs) and not failures,
            'playback_default_changed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--ffprobe', default='/opt/homebrew/bin/ffprobe')
    args = parser.parse_args()
    print(json.dumps(evaluate(json.loads(args.manifest.read_text()), args.ffprobe), sort_keys=True))
