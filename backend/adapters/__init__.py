"""Job board adapters package.

Each adapter exposes a ``fetch()`` method returning a list of
:class:`~backend.adapters.base.NormalizedJob`.
"""

from backend.adapters.base import NormalizedJob  # noqa: F401
