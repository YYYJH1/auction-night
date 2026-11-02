"""Make the shared helpers of the engine tests importable from the night tests."""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
for helper_dir in (TESTS / "engine", TESTS / "night"):
    if str(helper_dir) not in sys.path:
        sys.path.insert(0, str(helper_dir))
