"""Export the completed classroom note without calling either AI service."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tingji import course_naming, storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--note-id', required=True)
    parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    deadline = time.monotonic() + max(0, args.wait_seconds)
    previous = None
    while True:
        note = storage.get_note(args.note_id)
        phase = (note['status'], (note.get('asrTask') or {}).get('state'))
        if phase != previous:
            print(json.dumps({'status': phase[0], 'asrState': phase[1]}, ensure_ascii=False), flush=True)
            previous = phase
        if note['status'] == 'error':
            print(json.dumps({'error': note.get('error') or note.get('stage')}, ensure_ascii=False), flush=True)
            return 2
        if (note['status'] == 'ready' and note.get('asrComplete') is True
                and note.get('summary', '').strip() and not note.get('summaryStale')):
            folder = ROOT / 'output'
            folder.mkdir(exist_ok=True)
            outputs = {
                course_naming.download_name(note): note['summary'].strip() + '\n',
            }
            for name, content in outputs.items():
                target = folder / name
                target.write_text(content, encoding='utf-8-sig')
            print(json.dumps({'files': [str(folder / name) for name in outputs]}, ensure_ascii=False), flush=True)
            return 0
        if time.monotonic() >= deadline:
            return 3
        time.sleep(15)


if __name__ == '__main__':
    raise SystemExit(main())
