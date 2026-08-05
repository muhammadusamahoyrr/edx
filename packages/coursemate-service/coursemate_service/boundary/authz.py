"""Authorization re-derivation — design §10.1, §6.5.

    Open edX owns identity, roles and enrollment; we never define a parallel
    permission model. Every request carries the platform's authenticated
    identity, and the boundary re-checks enrollment and role **on every tool
    call**, not once per session.

Until now the boundary trusted the token's own `offering_id` claim. That was
weaker than the design requires, and the weakness is specific: **a token outlives
the permission it was minted under.** A student who unenrolls, is removed from a
cohort, or has access revoked keeps a valid signed token until it expires. The
signature proves the token was issued; it does not prove the enrollment still
holds.

This module closes that. Two properties make it safe to put in the request path:

* **Cached with a short TTL, not per-request round-trips.** §6.4 makes the
  metadata cache deliberately the shortest-lived tier precisely because *a
  revoked enrollment must stop working quickly*. 60s bounds the exposure while
  keeping the common case free.
* **Fails closed.** If the platform cannot be reached, access is denied rather
  than assumed. An availability problem must not become an authorization
  bypass — the tutor going down is recoverable, serving another cohort's content
  is not.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..config import settings
from .. import shared_state

log = logging.getLogger(__name__)


class NotEnrolled(PermissionError):
    """The platform says this user is not entitled to this offering."""


class PlatformUnreachable(RuntimeError):
    """Enrollment could not be confirmed. Deny — never assume."""


@dataclass(frozen=True)
class Entitlement:
    enrolled: bool
    is_staff: bool
    checked_at: float

    @property
    def age(self) -> float:
        return time.time() - self.checked_at


class EnrollmentVerifier:
    """Asks Open edX whether a user may access an offering, and caches briefly."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], Entitlement] = {}
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _key(self, user_id: str, offering_id: str) -> tuple[str, str]:
        return (user_id, offering_id)

    def invalidate(self, user_id: str | None = None, offering_id: str | None = None) -> int:
        """Drop cached entitlements.

        Called by the LMS-side unenrollment receiver (§3.4 rule 4) so revocation
        takes effect immediately rather than waiting out the TTL.
        """
        # Local first: always, even when Redis is in use, because this process
        # may have served from its own dict during a Redis outage.
        if user_id is None and offering_id is None:
            dropped = len(self._cache)
            self._cache.clear()
        else:
            doomed = [
                k for k in self._cache
                if (user_id is None or k[0] == user_id)
                and (offering_id is None or k[1] == offering_id)
            ]
            for k in doomed:
                self._cache.pop(k, None)
            dropped = len(doomed)

        client = shared_state.get_redis()
        if client is None:
            return dropped

        # SCAN, not KEYS: KEYS blocks the whole Redis instance, and this one is
        # also Celery's broker for the entire Open edX deployment.
        pattern = f"cm:authz:{user_id or '*'}:{offering_id or '*'}"
        try:
            batch: list[str] = []
            for key in client.scan_iter(match=pattern, count=500):
                batch.append(key)
                if len(batch) >= 500:
                    dropped += client.delete(*batch)
                    batch = []
            if batch:
                dropped += client.delete(*batch)
        except Exception as exc:  # noqa: BLE001
            # Loud, because this one matters: a failed invalidation means a
            # revoked student keeps working until the TTL expires. Bounded at
            # authz_cache_ttl_seconds rather than unbounded, but not immediate,
            # which is the whole point of the notice.
            log.error(
                "coursemate: authz invalidation could not reach redis (%s); "
                "revocation for user=%s offering=%s now waits out the %ss TTL",
                type(exc).__name__, user_id, offering_id,
                settings.authz_cache_ttl_seconds,
            )
        return dropped

    # --- platform credential ------------------------------------------------

    def _access_token(self) -> str:
        """OAuth2 client-credentials token for the CourseMate service account.

        The legacy `X-Edx-Api-Key` header is gone in current Open edX — it
        returns 401 — so this uses the supported grant. Cached until shortly
        before expiry, because minting one per authorization check would put two
        LMS round-trips in the retrieval path instead of zero.
        """
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        try:
            response = httpx.post(
                f"{settings.lms_url.rstrip('/')}/oauth2/access_token/",
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.lms_client_id,
                    "client_secret": settings.lms_client_secret,
                    "token_type": "jwt",
                },
                timeout=settings.authz_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise PlatformUnreachable(f"token exchange failed: {exc}") from exc

        if response.status_code != 200:
            raise PlatformUnreachable(f"token exchange returned {response.status_code}")

        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _ask_platform(self, user_id: str, offering_id: str) -> Entitlement:
        """Query the LMS enrollment API as the service account.

        Uses the *service* credential rather than the student's token: the
        student token is a claim of identity, and asking the platform "is this
        claim true" using the claim itself would be circular.

        `user_id` here is the **username** — the enrollment API keys on username,
        not the numeric id, which is why the claim carries both.
        """
        url = f"{settings.lms_url.rstrip('/')}/api/enrollment/v1/enrollment/{user_id},{offering_id}"
        try:
            response = httpx.get(
                url,
                timeout=settings.authz_timeout_seconds,
                headers={"Authorization": f"JWT {self._access_token()}"},
            )
        except PlatformUnreachable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformUnreachable(f"enrollment check failed: {exc}") from exc

        if response.status_code == 404:
            # Explicit, authoritative "no such enrollment".
            return Entitlement(enrolled=False, is_staff=False, checked_at=time.time())
        if response.status_code >= 500:
            raise PlatformUnreachable(f"LMS returned {response.status_code}")
        if response.status_code != 200:
            # 401/403 mean OUR credential is wrong, not that the student is
            # unenrolled. Denying is right; reporting it as "not enrolled" would
            # send an operator hunting the wrong problem.
            raise PlatformUnreachable(f"LMS returned {response.status_code}")

        try:
            body = response.json() or {}
        except Exception:  # noqa: BLE001
            # A 200 with an empty or non-JSON body is what this API returns for
            # "no such enrollment". Treated as NOT enrolled, which denies — the
            # safe reading of an ambiguous answer. Raising here instead would
            # turn a routine unenrolled user into an operator page.
            log.info("enrollment response for %s was not JSON; treating as not enrolled", user_id)
            return Entitlement(enrolled=False, is_staff=False, checked_at=time.time())

        return Entitlement(
            enrolled=bool(body.get("is_active", False)),
            is_staff=bool(body.get("is_staff", False)),
            checked_at=time.time(),
        )

    # --- shared cache -------------------------------------------------------
    #
    # Redis when configured, per-process otherwise. The per-process version was
    # not merely a scaling limit: `invalidate()` is a SECURITY control — the LMS
    # unenrollment receiver calls it so a revoked student stops working
    # immediately rather than waiting out the TTL — and with several replicas it
    # cleared one dictionary while the others kept serving the old answer. The
    # notice returned 200 and the student kept their access.

    def _redis_key(self, user_id: str, offering_id: str) -> str:
        return f"cm:authz:{user_id}:{offering_id}"

    def _cache_get(self, user_id: str, offering_id: str) -> Entitlement | None:
        client = shared_state.get_redis()
        if client is None:
            cached = self._cache.get(self._key(user_id, offering_id))
            if cached is not None and cached.age < settings.authz_cache_ttl_seconds:
                return cached
            return None
        try:
            raw = client.get(self._redis_key(user_id, offering_id))
        except Exception as exc:  # noqa: BLE001
            # A cache miss, not a denial. Redis is an optimisation here; the
            # platform remains the source of truth and is about to be asked.
            # Failing closed on a cache outage would deny entitled students
            # while proving nothing about anyone's enrollment.
            shared_state.redis_failed("authz cache read", exc)
            return None
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return Entitlement(
                enrolled=bool(d["enrolled"]),
                is_staff=bool(d["is_staff"]),
                checked_at=float(d["checked_at"]),
            )
        except Exception:  # noqa: BLE001 - a corrupt entry is a miss, never a grant
            return None

    def _cache_put(self, user_id: str, offering_id: str, ent: Entitlement) -> None:
        client = shared_state.get_redis()
        if client is None:
            self._cache[self._key(user_id, offering_id)] = ent
            return
        try:
            client.setex(
                self._redis_key(user_id, offering_id),
                settings.authz_cache_ttl_seconds,
                json.dumps({
                    "enrolled": ent.enrolled,
                    "is_staff": ent.is_staff,
                    "checked_at": ent.checked_at,
                }),
            )
        except Exception as exc:  # noqa: BLE001
            shared_state.redis_failed("authz cache write", exc)

    def verify(self, user_id: str, offering_id: str) -> Entitlement:
        cached = self._cache_get(user_id, offering_id)
        if cached is not None:
            return cached

        entitlement = self._ask_platform(user_id, offering_id)
        self._cache_put(user_id, offering_id, entitlement)
        return entitlement

    def require_enrolled(self, user_id: str, offering_id: str) -> Entitlement:
        entitlement = self.verify(user_id, offering_id)
        if not entitlement.enrolled:
            raise NotEnrolled(f"user {user_id} is not enrolled in {offering_id}")
        return entitlement


verifier = EnrollmentVerifier()
