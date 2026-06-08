"""Backwards-compatible entry point.

The implementation moved into the ``enrolhq_sync`` package. This thin shim
keeps the original script path working — prefer ``python -m enrolhq_sync``.

    python src/batch-update-student-parent-profiles.py [--live] [--limit N] ...
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from enrolhq_sync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
