"""Pytest configuration: make repo root importable for tests."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Skip the runtime 7zz bootstrap by default in tests so the suite
# never reaches out to www.7-zip.org. The bootstrap module honours
# this env var (see pipeline/bootstrap.py).
os.environ.setdefault("LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP", "1")
