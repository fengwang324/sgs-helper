"""命令行入口：``validate`` / ``build`` / ``query`` 三个命令。

``scripts/`` 下的三个脚本只是这里的薄包装，安装后也可以直接用
``sgs-validate`` / ``sgs-build`` / ``sgs-query``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import render
from .db import DataValidationError, build_database
from .repo import (
    DatabaseMissing,
    card_report,
    connect,
    faq_search,
    general_report,
    keyword_report,
    product_report,
    resolve,
    search,
    skill_report,
    stats,
    timing_report,
)
from .validate import validate_data_dir

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 查询命令 -> (kind, 渲染函数)
QUERY_KINDS: dict[str, str] = {
    "skill": "skill",
    "general": "general",
    "card": "card",
    "keyword": "keyword",
    "timing": "timing",
    "product": "product",
}


def default_data_dir() -> Path:
    """优先用当前工作目录下的 ``data/``，否则回退到仓库根目录。"""
    cwd = Path.cwd()
    if (cwd / "data").is_dir():
        return cwd / "data"
    if (REPO_ROOT / "data").is_dir():
        return REPO_ROOT / "data"
    return cwd / "data"


def default_db_path() -> Path:
    cwd = Path.cwd()
    if (cwd / "build").exists() or (cwd / "data").is_dir():
        return cwd / "build" / "sgs.db"
    return REPO_ROOT / "build" / "sgs.db"


# ---------------------------------------------------------------- validate


def validate_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_data.py", description="校验 data/ 下的 YAML 数据（模型 + 交叉引用）"
    )
    parser.add_argument("--data-dir", default=None, help="数据目录，默认 ./data")
    parser.add_argument("--strict", action="store_true", help="把警告也视为失败")
    parser.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    result = validate_data_dir(data_dir)
    if not args.quiet or not result.ok:
        print(result.format_report())
    if args.strict and result.warnings:
        print("\n✗ --strict：存在警告，视为失败", file=sys.stderr)
        return 1
    return 0 if result.ok else 1


# ---------------------------------------------------------------- build


def build_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_db.py", description="把 data/ 下的 YAML 全量重建为 SQLite"
    )
    parser.add_argument("--data-dir", default=None, help="数据目录，默认 ./data")
    parser.add_argument("--db", default=None, help="输出数据库路径，默认 ./build/sgs.db")
    parser.add_argument("--no-fts", action="store_true", help="不建 FTS5 全文索引（回退 LIKE）")
    parser.add_argument(
        "--allow-invalid", action="store_true", help="即使校验有错误也强行建库（不推荐）"
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    db_path = Path(args.db) if args.db else default_db_path()

    try:
        result = build_database(
            data_dir,
            db_path,
            with_fts=not args.no_fts,
            allow_invalid=args.allow_invalid,
        )
    except DataValidationError as exc:
        print("✗ 数据校验未通过，已终止建库。请先修复：\n", file=sys.stderr)
        print(exc.result.format_report(), file=sys.stderr)
        return 2

    print(result.summary())
    if result.warnings:
        print(
            f"\n（提示：本次构建有 {len(result.warnings)} 条警告，执行 "
            "python scripts/validate_data.py 可查看详情）"
        )
    return 0


# ---------------------------------------------------------------- admin


def admin_main(argv: list[str] | None = None) -> int:
    """启动本地资料管理网页（可选依赖：fastapi / uvicorn / jinja2 / python-multipart）。"""
    parser = argparse.ArgumentParser(
        prog="admin.py", description="启动本地资料管理网页（只写 YAML，SQLite 由 YAML 重建）"
    )
    parser.add_argument("--data-dir", default=None, help="数据目录，默认 ./data")
    parser.add_argument("--db", default=None, help="生成的数据库路径，默认 ./build/sgs.db")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument("--reload", action="store_true", help="代码改动自动重启（开发用）")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - 依赖缺失时的友好提示
        print(
            '✗ 未安装网页依赖。请先执行：pip install -e ".[admin]"\n'
            "  （需要 fastapi / uvicorn / jinja2 / python-multipart）",
            file=sys.stderr,
        )
        return 1

    data_dir = Path(args.data_dir).resolve() if args.data_dir else default_data_dir().resolve()
    db_path = Path(args.db).resolve() if args.db else default_db_path().resolve()
    if not data_dir.is_dir():
        print(f"✗ 数据目录不存在：{data_dir}", file=sys.stderr)
        return 1

    # 让 uvicorn 的 worker（含 --reload 子进程）拿到同一份配置
    os.environ["SGS_DATA_DIR"] = str(data_dir)
    os.environ["SGS_DB"] = str(db_path)

    print("sgs-helper 本地资料管理")
    print(f"  数据目录（权威源）：{data_dir}")
    print(f"  SQLite 生成物      ：{db_path}")
    print(f"  浏览器打开         ：http://127.0.0.1:{args.port}")
    print("  流程：浏览器编辑 → 写回 YAML → validate → build_db → SQLite 更新")
    uvicorn.run("sgs_helper.admin.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


# ---------------------------------------------------------------- query


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _add_query_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """通用选项同时注册在主子命令上，两种写法都能用：

    - ``query.py --json skill 集智``
    - ``query.py skill 集智 --json``

    子命令上用 ``SUPPRESS`` 默认值，避免覆盖主命令同名选项已经解析出来的值。
    """

    def default(value):
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument("--db", default=default(None), help="数据库路径，默认 ./build/sgs.db")
    parser.add_argument(
        "--json", action="store_true", default=default(False),
        help="输出结构化 JSON（便于接 API）",
    )
    parser.add_argument("--limit", type=int, default=default(10), help="列表类查询的最大条数")


def query_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="query.py",
        description="查询资料库（只读 SQLite 生成物）",
        epilog="示例：python scripts/query.py skill 集智",
    )
    _add_query_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    labels = {
        "skill": "查询技能：各模式、各版本文本、历史版本、来源",
        "general": "查询武将：体力、势力、所属产品、技能绑定版本与完整说明",
        "card": "查询卡牌：分类、卡图、拓展包、效果文本、关联技能与核验状态",
        "faq": "查询 FAQ / 裁定：按问题、回答或自动识别出的关联对象搜索",
        "keyword": "查询规则关键词",
        "timing": "查询时点与结算顺序",
        "product": "查询实体产品：收录武将、技能版本与游戏牌拓展包",
        "search": "跨类型模糊检索",
        "stats": "显示数据库构建元信息与实体统计",
    }
    for command, help_text in labels.items():
        sub_parser = sub.add_parser(command, help=help_text)
        _add_query_options(sub_parser, suppress_defaults=True)
        if command != "stats":
            sub_parser.add_argument("query", help="名称 / id / 关键词")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_db_path()
    try:
        conn = connect(db_path)
    except DatabaseMissing as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "stats":
            payload = stats(conn)
            if args.json:
                _print_json(payload)
            else:
                meta = payload["meta"]
                print(f"数据库：{db_path}")
                print(
                    f"schema 版本：{meta.get('schema_version')}    构建时间：{meta.get('built_at')}"
                )
                print(
                    f"Git：{meta.get('git_branch')} {str(meta.get('git_commit'))[:10]}"
                    f"（dirty={meta.get('git_dirty')}）"
                )
                print(f"YAML 清单指纹：{meta.get('yaml_manifest_sha256', '')[:16]}…")
                print(f"来源记录：{meta.get('source_count')} 条    FTS5：{meta.get('fts5')}")
                print(
                    "实体统计："
                    + " / ".join(f"{k} {v}" for k, v in payload["counts"].items())
                )
            return 0

        if args.command == "search":
            results = search(conn, args.query, limit=max(args.limit, 10) * 3)
            if args.json:
                _print_json(results)
            else:
                print(render.render_search(args.query, results))
            return 0 if results else 1

        if args.command == "faq":
            reports = faq_search(conn, args.query, limit=args.limit)
            if not reports:
                print(f'没有找到与 "{args.query}" 相关的 FAQ。')
                return 1
            if args.json:
                _print_json(reports)
            else:
                print(f'FAQ 检索 "{args.query}"，命中 {len(reports)} 条：')
                print("\n\n".join(render.render_faq(r) for r in reports))
            return 0

        kind = QUERY_KINDS[args.command]
        if args.command == "skill":
            report = skill_report(conn, args.query)
        elif args.command == "general":
            report = general_report(conn, args.query)
        elif args.command == "card":
            report = card_report(conn, args.query)
        elif args.command == "keyword":
            report = keyword_report(conn, args.query)
        elif args.command == "timing":
            report = timing_report(conn, args.query)
        else:
            report = product_report(conn, args.query)

        if report is None:
            _entity, candidates = resolve(conn, kind, args.query)
            if candidates:
                print(render.render_candidates(kind, args.query, candidates))
            else:
                label = render.KIND_BY_NAME[kind].label
                print(f'没有找到「{label}」：{args.query}')
                suggestions = search(conn, args.query, limit=8)
                if suggestions:
                    print()
                    print(render.render_search(args.query, suggestions))
            return 1

        if args.json:
            _print_json(report)
            return 0

        renderer = {
            "skill": render.render_skill,
            "general": render.render_general,
            "card": render.render_card,
            "keyword": render.render_keyword,
            "timing": render.render_timing,
            "product": render.render_product,
        }[kind]
        print(renderer(report))
        return 0
    finally:
        conn.close()
