#!/usr/bin/env python3
"""Export the validated YAML catalog for the static frontend.

Usage::

    python scripts/build_frontend.py
    python scripts/build_frontend.py --data-dir data --output-dir web
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sgs_helper.db import SCHEMA_VERSION  # noqa: E402
from sgs_helper.validate import ValidationResult, validate_data_dir  # noqa: E402

EXPORT_KINDS: tuple[tuple[str, str], ...] = (
    ("product", "products"),
    ("general", "generals"),
    ("general_variant", "general_variants"),
    ("skill", "skills"),
    ("skill_version", "skill_versions"),
    ("card", "cards"),
    ("faq", "faq"),
    ("keyword", "keywords"),
)


class InvalidDataError(ValueError):
    """Raised when YAML validation fails before any output is written."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(result.format_report())


def catalog_from_result(result: ValidationResult) -> dict[str, Any]:
    """Build the public catalog from validated Pydantic entities."""
    if not result.ok:
        raise InvalidDataError(result)

    bundle = result.bundle
    counts = result.counts()
    catalog: dict[str, Any] = {
        "schema_version": int(SCHEMA_VERSION),
        "counts": {
            plural: counts[kind]
            for kind, plural in EXPORT_KINDS
        },
    }
    for kind, plural in EXPORT_KINDS:
        catalog[plural] = [
            model.model_dump(mode="json")
            for model in bundle.all(kind).values()
        ]
    return catalog


def _write_atomic(path: Path, content: str) -> None:
    """Write one UTF-8 file through a uniquely named sibling temporary file."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="tmp_",
            suffix=path.suffix + ".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_catalog(data_dir: Path, output_dir: Path) -> ValidationResult:
    """Validate YAML, then export catalog.json and catalog.js."""
    result = validate_data_dir(data_dir)
    if not result.ok:
        raise InvalidDataError(result)

    catalog = catalog_from_result(result)
    json_text = json.dumps(
        catalog,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    js_text = f"window.SGS_CATALOG = {json_text.rstrip()};\n"

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(output_dir / "catalog.json", json_text)
    _write_atomic(output_dir / "catalog.js", js_text)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="YAML data directory (default: repository data/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "web",
        help="frontend output directory (default: repository web/)",
    )
    args = parser.parse_args(argv)

    try:
        result = export_catalog(args.data_dir, args.output_dir)
    except InvalidDataError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"写入前端目录失败：{exc}", file=sys.stderr)
        return 2

    print(f"已导出 {sum(result.counts().values())} 条记录到 {args.output_dir}")
    print(result.counts_line())
    if result.warnings:
        print(f"校验警告：{len(result.warnings)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
