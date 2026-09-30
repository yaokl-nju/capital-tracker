"""Bounded local observations propagated through asyncio.to_thread without secrets."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
import time


_ACTIVE = ContextVar('capital_diagnostics', default=None)
_FIELDS = {'backend', 'outcome', 'result_count', 'cache_hit', 'duration_seconds',
           'error_type', 'status_code', 'stage', 'citation_count', 'matched_count', 'unverified_count',
           'prompt_tokens', 'completion_tokens', 'total_tokens'}


class Diagnostics:
    def __init__(self, limit=500):
        self.events = []
        self.limit = limit
        self.dropped = 0
        self.lock = RLock()

    def append(self, kind, fields):
        # This allowlist prevents accidentally persisting HTTP headers, keys or prompts.
        event = {'kind': kind, **{k: v for k, v in fields.items()
                                 if k in _FIELDS and isinstance(v, (str, bool, int, float))}}
        with self.lock:
            if len(self.events) < self.limit:
                self.events.append(event)
            else:
                self.dropped += 1

    def snapshot(self):
        with self.lock:
            return {'events': [dict(event) for event in self.events], 'dropped_events': self.dropped}


def record_event(kind, **fields):
    capture = _ACTIVE.get()
    if capture is not None:
        capture.append(kind, fields)


@contextmanager
def capture_diagnostics():
    capture = Diagnostics()
    token = _ACTIVE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE.reset(token)


@contextmanager
def observe_stage(stage):
    started = time.monotonic()
    outcome, error_type = 'completed', ''
    try:
        yield
    except BaseException as exc:
        outcome, error_type = 'error', type(exc).__name__
        raise
    finally:
        record_event('stage', stage=stage, outcome=outcome, error_type=error_type,
                     duration_seconds=round(time.monotonic() - started, 3))
