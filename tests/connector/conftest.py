import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
for directory in (REPO / "packages/connector", REPO / "packages/hermes-client", REPO / "apps/api"):
    sys.path.insert(0, str(directory))
