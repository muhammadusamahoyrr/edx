"""The wire contract version.

Both sides assert this at startup. A single deployment runs one version of each
package (design §3.5 — one instance, one tenant), so a hard lock is correct and
cheaper than negotiation. Bump on any breaking change to a model in this package.
"""

CONTRACT_VERSION = 1


class ContractMismatch(RuntimeError):
    """Raised at startup when platform and service disagree on the wire format."""


def assert_compatible(peer_version: int, peer_name: str) -> None:
    """Fail loudly at startup rather than as a 422 in week three."""
    if peer_version != CONTRACT_VERSION:
        raise ContractMismatch(
            f"{peer_name} speaks contract v{peer_version}, we speak v{CONTRACT_VERSION}. "
            "Deploy both packages together."
        )
