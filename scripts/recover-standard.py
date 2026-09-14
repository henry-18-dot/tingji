"""Continue one explicitly authorized recording through the local Tingji API.

No credentials are read. Submission is recorded before its one network attempt.
This helper exits on processing, completion, a failure, or its bounded deadline;
the Tingji service owns subsequent queries and automatic summarization.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = 'http://127.0.0.1:8765'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, body=None, token='', action_id=''):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-App-Token'] = token
    if action_id:
        headers['X-Action-Id'] = action_id
    request = urllib.request.Request(BASE + path, headers=headers,
        data=None if body is None else json.dumps(body).encode('utf-8'))
    with OPENER.open(request, timeout=15) as response:
        return json.load(response)


def safe_note(note):
    task = note.get('asrTask') or {}
    return {'noteId': note['id'], 'title': note['title'],
        'status': note['status'], 'stage': note.get('stage', ''),
        'transcriptChars': len(note.get('transcript', '')),
        'summaryChars': len(note.get('summary', '')),
        'standardTask': {k: v for k, v in task.items() if k in
            ('state', 'requestId', 'queryId', 'code', 'logId',
             'submittedAt', 'lastCheckedAt', 'nextCheckAt', 'autoSummarize')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--note-id', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--wait-config-seconds', type=int, default=7200)
    parser.add_argument('--observe-seconds', type=int, default=14400)
    args = parser.parse_args()
    note_id = str(uuid.UUID(args.note_id))
    folder = ROOT / 'data' / 'recovery' / note_id
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / 'standard-recovery.json'
    state = json.loads(state_path.read_text('utf-8')) if state_path.exists() else {
        'noteId': note_id, 'actionId': str(uuid.uuid4()), 'submissionAttempted': False}

    def save(phase, note=None, **extra):
        state.update(phase=phase, checkedAt=datetime.now(timezone.utc).isoformat(), **extra)
        if note:
            state['note'] = safe_note(note)
        temporary = state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(state_path)

    started = time.monotonic()
    submitted = None
    failures = 0
    while True:
        try:
            bootstrap = api('/api/bootstrap')
            note = api('/api/notes/' + note_id)
            failures = 0
        except (OSError, ValueError):
            failures += 1
            save('local_connection_failed', failures=failures)
            if failures >= 3:
                return 2
            time.sleep(10)
            continue
        config = bootstrap['settings']
        task = note.get('asrTask') or {}
        if task.get('state') in ('processing', 'completed'):
            if note['status'] == 'error':
                # ASR can be completed while silence or subsequent summary
                # failure still needs attention. Never report this as success.
                save('needs_attention', note)
                return 2
            save('processing_confirmed' if task['state'] == 'processing' else 'asr_completed', note)
            return 0
        if task.get('requestId') or note['status'] in ('transcribing', 'summarizing'):
            if task.get('state') in ('rejected', 'failed', 'paused', 'not_found', 'local_error') or note['status'] == 'error':
                save('needs_attention', note)
                return 2
            submitted = submitted or time.monotonic()
            save('awaiting_processing', note)
            if time.monotonic() - submitted >= args.observe_seconds:
                save('observation_deadline', note)
                return 3
        elif state['submissionAttempted']:
            # An ambiguous local response must never create another submission.
            save('submission_needs_review', note)
            return 2
        elif not args.execute:
            save('inspection_only', note)
            return 0
        elif config.get('asrMode') != 'standard':
            save('mode_changed', note)
            return 2
        elif not (config.get('tosConfigured') and config.get('tosPrivateConfirmed')
                  and config.get('asrConfigured') and config.get('deepseekConfigured')):
            save('waiting_for_configuration', note,
                missing=[k for k in ('tosConfigured', 'tosPrivateConfirmed', 'asrConfigured', 'deepseekConfigured') if not config.get(k)])
            if time.monotonic() - started >= args.wait_config_seconds:
                save('configuration_deadline', note)
                return 3
        else:
            fast = note.get('fastTask') or {}
            if fast and fast.get('state') != 'rejected':
                save('previous_fast_result_needs_review', note)
                return 2
            save('submitting_once', note, submissionAttempted=True)
            try:
                api('/api/notes/' + note_id + '/transcribe', {
                    'asrMode': 'standard', 'cloudUploadConsent': True,
                    'autoSummarize': True, 'template': note['template'],
                    'language': note['language']}, bootstrap['token'], state['actionId'])
            except (OSError, ValueError):
                # Observe persisted task identity; never resend the paid action.
                save('local_submission_response_unconfirmed', note)
            submitted = time.monotonic()
        time.sleep(10)


if __name__ == '__main__':
    raise SystemExit(main())
