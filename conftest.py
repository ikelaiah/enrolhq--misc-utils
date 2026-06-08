"""Pytest bootstrap: put ``src/`` on the import path.

The package lives under ``src/`` but is not pip-installed in this lightweight
repo, so this root ``conftest.py`` makes ``import enrolhq_sync`` work from the
tests without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
