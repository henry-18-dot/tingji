"""Read missing durations in a bounded local worker; no recognition requests."""
import math
import threading
from concurrent.futures import ThreadPoolExecutor

from . import jobs, storage

POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tingji-duration')
LOCK = threading.Lock()
PENDING = set()
FAILED = set()


def ensure_duration(note):
    value = note.get('duration')
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return
    if not note.get('audioFile') or note.get('audioDeletedAt') or not jobs.FFPROBE:
        return
    ident = note['id']
    with LOCK:
        if ident in PENDING or ident in FAILED:
            return
        source = (storage.DATA / 'audio' / note['audioFile']).resolve()
        if not source.is_relative_to((storage.DATA / 'audio').resolve()) or not source.is_file():
            return
        PENDING.add(ident)

    def work():
        try:
            seconds = jobs.duration(source)
            with storage.LOCK:
                current = storage.get_note(ident)
                if not current.get('duration') and not current.get('audioDeletedAt'):
                    storage.update_note(ident, duration=seconds)
        except Exception:
            with LOCK:
                FAILED.add(ident)
        finally:
            with LOCK:
                PENDING.discard(ident)
    POOL.submit(work)
