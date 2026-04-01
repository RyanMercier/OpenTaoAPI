"""
Compatibility layer for bittensor SDK metagraph attribute names.

Older SDK versions use short names (T, C, I, E, S, D, R) while newer
versions use full names (trust, consensus, incentive, emission, ...).
"""

import logging

logger = logging.getLogger(__name__)

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
    """Get a metagraph vector by name, resolving SDK version differences."""
    val = getattr(meta, attr, None)
    if val is not None:
        return val

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
    """Get a single neuron's scalar value from a metagraph vector."""
    vec = meta_get(meta, attr)
    if vec is not None:
        try:
            return float(vec[uid])
        except (IndexError, TypeError):
            return default
    return default
