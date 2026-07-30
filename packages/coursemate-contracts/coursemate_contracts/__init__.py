"""CourseMate wire contracts.

The one package both halves import. Kept dependency-free apart from pydantic so
that importing it into an LMS process costs nothing — see the dependency ceiling
in `CourseMate_Repository_Structure.md` §2.

Nothing here reaches a network, a database, or a model. If something in this
package needs a client, it belongs in the package that owns that client.
"""

from .version import CONTRACT_VERSION, ContractMismatch, assert_compatible

__all__ = ["CONTRACT_VERSION", "ContractMismatch", "assert_compatible"]
