#!/usr/bin/env python3
"""查询入口。

用法::

    python scripts/query.py skill 集智
    python scripts/query.py general 孔融
    python scripts/query.py card 杀
    python scripts/query.py faq 伤害
    python scripts/query.py keyword 濒死
    python scripts/query.py timing 伤害结算
    python scripts/query.py product 国战
    python scripts/query.py search 集智
    python scripts/query.py skill 集智 --json
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sgs_helper.cli import query_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(query_main())
