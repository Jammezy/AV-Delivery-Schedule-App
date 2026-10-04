"""Bounded cleanup on ordinary app use; no scheduler or background thread."""
import json
import threading
import time

from retention import run_retention


class RequestRetention:
    def __init__(self):
        self._lock = threading.Lock()
        self._next_attempt = 0

    def maybe_run(self, logger):
        if time.monotonic() < self._next_attempt or not self._lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() < self._next_attempt:
                return
            try:
                result = run_retention(apply=True, max_rows=1000, timeout_ms=200,
                                       statement_timeout_ms=5000)
            except Exception as error:
                # Never log database exceptions, credentials or stored payloads.
                self._next_attempt = time.monotonic() + 300
                logger.warning("Request retention failed (%s); retry on use after five minutes",
                               type(error).__name__)
            else:
                self._next_attempt = time.monotonic() + (60 if result['backlogRemaining'] else 86400)
                logger.info("Request retention %s", json.dumps(result, sort_keys=True))
        finally:
            self._lock.release()
