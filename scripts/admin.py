#!/usr/bin/env python3
"""启动本地资料管理网页（FastAPI + Jinja2）。

直接运行：

    python scripts/admin.py                       # http://127.0.0.1:8000
    python scripts/admin.py --port 8010 --reload
    python scripts/admin.py --data-dir ./data --db ./build/sgs.db

等价于：

    uvicorn sgs_helper.admin.app:app --host 127.0.0.1 --port 8000

只用于**本地**维护资料：网页只写 data/ 下的 YAML，SQLite 由 YAML 重建。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sgs_helper.cli import admin_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(admin_main())
