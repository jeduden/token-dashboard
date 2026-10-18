"""Process-wide scan status + single-flight lock shared by the server and CLI."""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

SCAN_LOCK = threading.Lock()   # held for the duration of any scan
_STATE_LOCK = threading.Lock()
_EMIT_INTERVAL = 0.5

_STATE = {
    "running": False, "files_done": 0, "files_total": 0,
    "bytes_done": 0, "bytes_total": 0,
    "started_at": None, "last_finished_at": None, "last_error": None,
}


def get_status() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def _update(**kw) -> None:
    with _STATE_LOCK:
        _STATE.update(kw)


def make_progress(emit: Optional[Callable[[dict], None]] = None):
    """Return a scan_dir progress callback that updates status and emits throttled events."""
    last = [0.0]

    def cb(files_done, files_total, bytes_done, bytes_total):
        _update(files_done=files_done, files_total=files_total,
                bytes_done=bytes_done, bytes_total=bytes_total)
        now = time.time()
        if emit and (now - last[0] >= _EMIT_INTERVAL or files_done == files_total):
            last[0] = now
            emit({"type": "scan_progress", **get_status()})
    return cb


def run_scan(projects_dir, db_path, scan_fn, emit=None) -> tuple:
    """Run ``scan_fn(projects_dir, db_path, progress=cb)`` single-flight.

    Returns (started, result). When another scan holds the lock, returns
    (False, None) immediately without scanning.
    """
    if not SCAN_LOCK.acquire(blocking=False):
        return False, None
    try:
        _update(running=True, started_at=time.time(), last_error=None,
                files_done=0, files_total=0, bytes_done=0, bytes_total=0)
        try:
            result = scan_fn(projects_dir, db_path, progress=make_progress(emit))
            return True, result
        except Exception as e:
            _update(last_error=str(e))
            raise
        finally:
            _update(running=False, last_finished_at=time.time())
            if emit:
                emit({"type": "scan_progress", **get_status()})
    finally:
        SCAN_LOCK.release()
