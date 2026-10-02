#!/usr/bin/env python3
"""SQLite 构建入口：从 YAML 全量重建 ``build/sgs.db``。

用法::

    python scripts/build_db.py
    python scripts/build_db.py --db build/sgs.db --data-dir data
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgs_helper.cli import build_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(build_main())
