"""Keep bundled Linux libraries out of external system programs."""
from __future__ import annotations

import os
import sys


def system_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if getattr(sys, "frozen", False) and sys.platform == "linux":
        # PyInstaller prepends its private libraries and saves the caller's
        # original value. Restore it only for the child, never the connector.
        original = environment.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            environment.pop("LD_LIBRARY_PATH", None)
        else:
            environment["LD_LIBRARY_PATH"] = original
    return environment
