"""Modding-framework adapters.

Each adapter teaches ModWright how one framework scaffolds, builds, deploys,
and logs. See `base.ModFrameworkAdapter` for the contract and `registry` for
lookup.
"""

from modwright.adapters.base import ModFrameworkAdapter
from modwright.adapters.registry import ADAPTERS, detect_framework, get_adapter

__all__ = ["ModFrameworkAdapter", "ADAPTERS", "detect_framework", "get_adapter"]
