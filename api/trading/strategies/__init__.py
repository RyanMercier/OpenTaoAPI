"""Strategy plugin registry.

Built-in strategies live in this package. Operators can register their own
by either dropping a file into this directory and importing it, or by
pointing the ``OPENTAO_EXTERNAL_STRATEGIES`` env var at a directory or
single ``.py`` file. Each strategy must:

  1. Subclass ``Strategy`` from ``api.trading.strategies.base``.
  2. Decorate the class with ``@register_strategy("<key>")``.

The decorator populates ``STRATEGIES`` so the runner, the API, and the UI
can discover them by name.

Cut from the original TaoTrader release: ``timesfm``, ``combo``, and the
proprietary ``stmc`` / ``vstmc`` / ``xstmc`` momentum strategies. Operators
running the proprietary set keep it private and load it via the
external-strategy path.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Type

from .base import Strategy

logger = logging.getLogger(__name__)

STRATEGIES: dict[str, Type[Strategy]] = {}
STRATEGY_SOURCES: dict[str, str] = {}  # key -> "builtin" or "external:<path>"


def register_strategy(key: str, *, source: str = "builtin"):
    """Class decorator. ``@register_strategy("mean_reversion")`` adds the
    decorated class to the global registry. Re-registering the same key
    replaces the previous entry; sources differing across registrations
    are logged as warnings."""
    def deco(cls: Type[Strategy]) -> Type[Strategy]:
        prev = STRATEGY_SOURCES.get(key)
        if prev is not None and prev != source:
            logger.warning(
                "Strategy key %s already registered (was %s), overwriting with %s",
                key, prev, source,
            )
        STRATEGIES[key] = cls
        STRATEGY_SOURCES[key] = source
        return cls
    return deco


def list_strategies() -> list[dict]:
    """Return registry contents in JSON-friendly form for the API."""
    out = []
    for key in sorted(STRATEGIES):
        cls = STRATEGIES[key]
        first_line = ""
        if cls.__doc__:
            for ln in cls.__doc__.strip().splitlines():
                if ln.strip():
                    first_line = ln.strip()
                    break
        out.append({
            "name": key,
            "source": STRATEGY_SOURCES.get(key, "unknown"),
            "doc": first_line,
        })
    return out


def load_external_strategies(paths_env: str | None = None) -> int:
    """Import strategy modules from a colon-separated list (env var by
    default). Each entry can be a directory (every non-underscore ``*.py``
    file is imported) or a single ``.py`` file. Returns the file count."""
    raw = paths_env if paths_env is not None else os.environ.get(
        "OPENTAO_EXTERNAL_STRATEGIES", ""
    )
    if not raw:
        return 0
    loaded = 0
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        path = Path(entry).expanduser().resolve()
        if not path.exists():
            logger.warning("External strategy path does not exist: %s", path)
            continue
        files = [path] if path.is_file() else sorted(path.glob("*.py"))
        for f in files:
            if f.name.startswith("_"):
                continue
            try:
                _import_external_file(f)
                loaded += 1
            except Exception:
                logger.exception("Failed to import external strategy %s", f)
    return loaded


def _import_external_file(path: Path) -> None:
    """Import a strategy file by path. Any keys it registers get retagged
    with the file path as their source so the API can show provenance."""
    pre_keys = set(STRATEGIES)
    spec = importlib.util.spec_from_file_location(
        f"opentao_external.{path.stem}", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    new_keys = set(STRATEGIES) - pre_keys
    for key in new_keys:
        STRATEGY_SOURCES[key] = f"external:{path}"


# Import built-ins last so their ``@register_strategy`` calls work.
from . import drain_detector   # noqa: E402,F401
from . import mean_reversion   # noqa: E402,F401
from . import momentum         # noqa: E402,F401
from . import stake_velocity   # noqa: E402,F401

# Class re-exports for callers that prefer importing types directly.
from .drain_detector import DrainDetector              # noqa: E402,F401
from .mean_reversion import MeanReversionStrategy      # noqa: E402,F401
from .momentum import MomentumStrategy                  # noqa: E402,F401
from .stake_velocity import StakeVelocityStrategy      # noqa: E402,F401

__all__ = [
    "Strategy",
    "STRATEGIES",
    "STRATEGY_SOURCES",
    "register_strategy",
    "list_strategies",
    "load_external_strategies",
    "DrainDetector",
    "MeanReversionStrategy",
    "MomentumStrategy",
    "StakeVelocityStrategy",
]
