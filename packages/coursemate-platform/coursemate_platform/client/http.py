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
from coursemate_contracts import CONTRACT_VERSION, ContractMismatch, assert_compatible

#: Header name is defined by the SERVICE side; duplicated here rather than
#: imported because `coursemate_platform` must not import
#: `coursemate_service` (.importlinter contract 2). A test asserts the two
#: spellings agree.
CONTRACT_VERSION_HEADER = "X-CourseMate-Contract-Version"

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


#: The peer's wire-contract version, learned once from `/health`.
#:
#: `None` means "not asked yet". Cached because the answer only changes when the
#: service is redeployed, and this process is restarted by that anyway.
_peer_contract_version: int | None = None

HEALTH_PATH = "/coursemate/health"


def _assert_peer_contract() -> None:
    """Check the service speaks our wire contract, on first contact.

    **On first contact, deliberately not at startup**, and that is the whole
    design of this check. `settings/common.py` says it in its own docstring: a
    plugin that raises during settings loading takes the LMS down for every
    course, including the ones that never enabled CourseMate. A version lock that
    can do that is worse than the skew it detects — and at Django startup the
    service may legitimately not be up yet, so it would fail on ordering alone.

    Here, the cost of a mismatch is that *CourseMate* calls fail, loudly, with
    `ContractMismatch`. Ingest and invalidation run in Celery, which already
    treats a CourseMate failure as a failed task. The LMS keeps serving.

    Failure to *reach* the service is not a mismatch and is not cached: an
    outage must not be remembered as an incompatibility that survives it. The
    request that follows will fail on its own and the breaker will count it.
    """
    global _peer_contract_version
    if _peer_contract_version is not None:
        return

    from django.conf import settings

    url = settings.COURSEMATE_SERVICE_URL.rstrip("/") + HEALTH_PATH
    try:
        response = httpx.get(url, timeout=settings.COURSEMATE_HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        peer = int(response.json()["contract_version"])
    except ContractMismatch:
        raise
    except Exception:
        log.warning(
            "coursemate: could not read the service contract version from %s; "
            "continuing unchecked", HEALTH_PATH,
        )
        return

    assert_compatible(peer, "coursemate-service")
    _peer_contract_version = peer
    log.info("coursemate: service speaks contract v%s", peer)


def _headers() -> dict:
    from django.conf import settings

    return {
        "Authorization": f"Bearer {settings.COURSEMATE_SERVICE_CREDENTIAL}",
        # Lets the SERVICE refuse us, which is the other half of the lock: this
        # side learns the peer's version from /health, and that side learns ours
        # from here. A header keeps the request bodies — and their schemas —
        # exactly as they were.
        CONTRACT_VERSION_HEADER: str(CONTRACT_VERSION),
    }


def reset_peer_contract_for_tests() -> None:
    global _peer_contract_version
    _peer_contract_version = None


def get(path: str) -> dict:
    """Read from the service. Same breaker as post(): a CourseMate outage must
    not wedge a Celery worker mid-sweep."""
    from django.conf import settings

    _assert_peer_contract()
    _breaker.before()
    url = settings.COURSEMATE_SERVICE_URL.rstrip("/") + path
    try:
        response = httpx.get(
            url,
            timeout=settings.COURSEMATE_HTTP_TIMEOUT_SECONDS,
            headers=_headers(),
        )
        response.raise_for_status()
        _breaker.record(True)
        return response.json()
    except Exception:
        _breaker.record(False)
        log.exception("coursemate: GET %s failed", path)
        raise


def post(path: str, payload: dict) -> dict:
    from django.conf import settings

    _assert_peer_contract()
    _breaker.before()
    url = settings.COURSEMATE_SERVICE_URL.rstrip("/") + path
    try:
        response = httpx.post(
            url,
            json=payload,
            timeout=settings.COURSEMATE_HTTP_TIMEOUT_SECONDS,
            headers=_headers(),
        )
        response.raise_for_status()
        _breaker.record(True)
        return response.json()
    except Exception:
        _breaker.record(False)
        log.exception("coursemate: POST %s failed", path)
        raise
