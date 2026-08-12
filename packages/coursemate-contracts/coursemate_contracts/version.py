"""The wire contract version.

A single deployment runs one version of each package (design §3.5 — one
instance, one tenant), so a hard lock is correct and cheaper than negotiation.
Bump on any breaking change to a model in this package.

**Asserted on first contact and on every server-to-server call — NOT at
startup**, and the difference is deliberate. An earlier version of this docstring
said "Both sides assert this at startup", which was false in two ways: nothing
called `assert_compatible` at all, and a startup assert on the platform side
could not have been written safely. `settings/common.py` explains why in its own
docstring — a plugin that raises during settings loading takes the LMS down for
every course, including the ones that never enabled CourseMate. A version lock
able to do that is worse than the skew it detects.

Where it is actually enforced:

* **Platform → service**, `client/http.py`: reads `contract_version` from the
  service's `/health` on the first server-to-server call, caches it, and raises
  `ContractMismatch` on skew. A CourseMate call fails; the LMS keeps serving.
* **Service → platform**, `api/deps.contract_version_guard`: the client stamps
  `X-CourseMate-Contract-Version` on every ingest and invalidation request, and
  the service refuses a mismatch with `CONTRACT_MISMATCH` (HTTP 409). Governed by
  `contract_version_lock`; with the lock off, nothing is checked.

A header rather than a body field, so the request schemas — and the published
OpenAPI — are untouched by the thing that checks them.
"""

CONTRACT_VERSION = 1


class ContractMismatch(RuntimeError):
    """Raised at startup when platform and service disagree on the wire format."""


def assert_compatible(peer_version: int, peer_name: str) -> None:
    """Fail loudly on first contact rather than as a 422 in week three."""
    if peer_version != CONTRACT_VERSION:
        raise ContractMismatch(
            f"{peer_name} speaks contract v{peer_version}, we speak v{CONTRACT_VERSION}. "
            "Deploy both packages together."
        )
