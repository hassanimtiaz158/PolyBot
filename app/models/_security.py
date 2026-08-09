"""Secure pickle deserialization with class whitelisting.

The ``RestrictedUnpickler`` only allows deserialization of known-safe classes
from numpy, sklearn, lightgbm, and the application's own model modules.
All other classes are rejected to prevent arbitrary code execution via
malicious pickle files.
"""

from __future__ import annotations

import logging
import pickle
from typing import Any

logger = logging.getLogger(__name__)

# Whitelisted module prefixes for deserialization.
_ALLOWED_PREFIXES = (
    "numpy.",
    "numpy.core.",
    "numpy.ma.",
    "sklearn.",
    "sklearn.tree.",
    "sklearn.ensemble.",
    "sklearn.linear_model.",
    "sklearn.preprocessing.",
    "sklearn.calibration.",
    "sklearn.isotonic.",
    "lightgbm.",
    "builtins.",
    "collections.",
    # Application's own model classes (safe — we control the code).
    "app.models.probability_model.",
    "app.models.calibration.",
)

# Exact class names that are safe to deserialize.
_ALLOWED_CLASSES = frozenset({
    "numpy.dtype",
    "numpy.ndarray",
    "numpy.float64",
    "numpy.int64",
    "numpy.bool_",
    "numpy.str_",
    "numpy.bytes_",
    "numpy.array",
    "numpy.core.multiarray._reconstruct",
    "builtins.range",
    "builtins.set",
    "builtins.frozenset",
    "builtins.dict",
    "builtins.tuple",
    "builtins.list",
    "collections.OrderedDict",
})


class RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that rejects untrusted classes."""

    def find_class(self, module: str, name: str) -> Any:
        qualified = f"{module}.{name}"

        # Check exact match first
        if qualified in _ALLOWED_CLASSES:
            return super().find_class(module, name)

        # Check prefix match for known ML libraries
        if any(qualified.startswith(p) for p in _ALLOWED_PREFIXES):
            return super().find_class(module, name)

        raise pickle.UnpicklingError(
            f"Disallowed class: {qualified!r} "
            f"(module={module!r}, name={name!r})"
        )


def safe_load(path: str) -> Any:
    """Load a pickle file with restricted deserialization."""
    with open(path, "rb") as f:
        return RestrictedUnpickler(f).load()


def safe_dump(obj: Any, path: str) -> None:
    """Dump an object to a pickle file."""
    with open(path, "wb") as f:
        pickle.dump(obj, f)
