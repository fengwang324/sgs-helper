#!/usr/bin/env python3
"""数据校验入口。

用法::

    python scripts/validate_data.py
    python scripts/validate_data.py --data-dir data --strict
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgs_helper.cli import validate_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(validate_main())
