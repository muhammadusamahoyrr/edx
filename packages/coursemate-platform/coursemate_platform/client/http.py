"""Server-to-server HTTP. NOT student traffic.

Since design v8 the student path does not pass through Python at all: the XBlock
mints a token and the browser streams from the service directly. So the circuit
breaker here guards *background* work — ingest writes and invalidation — where a
CourseMate outage must not wedge a Celery worker.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 5
_COOLDOWN_SECONDS = 60


class CircuitOpen(RuntimeError):
    """The service has been failing; stop calling it for a cooling period."""


class _Breaker:
    def __init__(self) -> None:
        self._failures = 0
        self._opened_at = 0.0

    def before(self) -> None:
        if self._failures < _FAILURE_THRESHOLD:
            return
        if time.time() - self._opened_at < _COOLDOWN_SECONDS:
            raise CircuitOpen("CourseMate service is in cooldown")
        self._failures = 0  # half-open: allow one probe

    def record(self, ok: bool) -> None:
        if ok:
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= _FAILURE_THRESHOLD:
            self._opened_at = time.time()


_breaker = _Breaker()


def post(path: str, payload: dict) -> dict:
    from django.conf import settings

    _breaker.before()
    url = settings.COURSEMATE_SERVICE_URL.rstrip("/") + path
    try:
        response = httpx.post(
            url,
            json=payload,
            timeout=settings.COURSEMATE_HTTP_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {settings.COURSEMATE_SERVICE_CREDENTIAL}"},
        )
        response.raise_for_status()
        _breaker.record(True)
        return response.json()
    except Exception:
        _breaker.record(False)
        log.exception("coursemate: POST %s failed", path)
        raise
