"""
Compatibility layer for different bittensor SDK versions.

Older SDK uses short metagraph attribute names: T, C, I, E, S, D, R
Newer SDK (AsyncMetagraph) uses full names: trust, consensus, incentive, emission, ...

This module provides safe access that works with both.
"""

import logging

logger = logging.getLogger(__name__)

# Short name → list of possible attribute names (tried in order)
_METAGRAPH_ALIASES = {
    "T": ["T", "trust"],
    "C": ["C", "consensus"],
    "I": ["I", "incentive"],
    "E": ["E", "emission", "emissions"],
    "S": ["S", "stake", "alpha_stake"],
    "D": ["D", "dividends"],
    "R": ["R", "ranks", "rank"],
    "Tv": ["Tv", "validator_trust"],
}

_logged_mappings: set[str] = set()


def meta_get(meta, attr: str):
    """Get a metagraph vector attribute, handling SDK version differences.

    Usage:
        trust_vector = meta_get(meta, "T")
        trust_uid = meta_get(meta, "T")[uid]

    Returns the vector/array, or None if not found.
    """
    # Try exact name first
    val = getattr(meta, attr, None)
    if val is not None:
        return val

    # Try aliases
    for alias in _METAGRAPH_ALIASES.get(attr, []):
        val = getattr(meta, alias, None)
        if val is not None:
            key = f"{attr}->{alias}"
            if key not in _logged_mappings:
                logger.info(f"Metagraph compat: {attr} resolved to {alias}")
                _logged_mappings.add(key)
            return val

    return None


def meta_get_uid(meta, attr: str, uid: int, default: float = 0.0) -> float:
    """Get a single neuron's value from a metagraph vector.

    Usage:
        trust = meta_get_uid(meta, "T", uid)
        incentive = meta_get_uid(meta, "I", uid)

    Returns float, or default if attribute not found.
    """
    vec = meta_get(meta, attr)
    if vec is not None:
        try:
            return float(vec[uid])
        except (IndexError, TypeError):
            return default
    return default