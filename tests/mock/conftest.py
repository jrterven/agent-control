from __future__ import annotations

import sys
from pathlib import Path


MOCK_SRC = Path(__file__).resolve().parents[2] / "apps" / "mock-hermes" / "src"
CLIENT_SRC = Path(__file__).resolve().parents[2] / "packages" / "hermes-client"
sys.path.insert(0, str(MOCK_SRC))
sys.path.insert(0, str(CLIENT_SRC))
