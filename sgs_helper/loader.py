"""YAML 装载层。

- 一个文件可以放：单个实体映射、实体列表、或 ``items: [ ... ]`` 包装。
- 实体**放在哪个目录**决定它是什么类型（``data/generals/**`` -> general）。
- 每个实体都会记录来源文件、起始行、文件 sha256，用于可追溯性。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .constants import KIND_BY_DIRNAME, KINDS

#: 文件级错误用这种 path 记录
YAML_SUFFIXES = (".yaml", ".yml")

#: 有 libyaml 时用 C 实现的解析器：同样的 YAML，快 5～7 倍
#: （数据长到几百个文件以后，这一条直接决定管理页能不能“秒开”）
try:  # pragma: no cover - 取决于环境是否装了 libyaml
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _SafeLoader


@dataclass
class Origin:
    """实体在 YAML 源中的位置。"""

    path: str   # 相对 data/ 根目录，例如 "generals/kongrong.yaml"
    line: int   # 实体起始行（1-based）
    sha256: str  # 文件内容 sha256，用于判断构建物是否过期

    @property
    def location(self) -> str:
        return f"data/{self.path}:{self.line}"


@dataclass
class Issue:
    """一条校验问题。level 为 ``error`` 或 ``warning``。"""

    level: str
    path: str
    field: str
    message: str
    line: int | None = None
    entity_id: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level == "error"

    def format(self) -> str:
        """输出格式固定为：文件路径 / 字段 / 错误原因。"""
        location = f"data/{self.path}"
        if self.line:
            location += f":{self.line}"
        owner = f"{self.entity_id}." if self.entity_id else ""
        mark = "✗" if self.is_error else "⚠"
        return f"{mark} {location}\n    {owner}{self.field}\n    {self.message}"


@dataclass
class RawEntity:
    """尚未做模型校验的一条原始 YAML 记录。"""

    kind: str
    data: dict[str, Any]
    origin: Origin


@dataclass
class DataBundle:
    """校验通过后的完整数据集。

    ``items[kind][id]`` 是对应的 Pydantic 模型对象。
    """

    data_dir: Path
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    origin: dict[str, dict[str, Origin]] = field(default_factory=dict)

    def add(self, kind: str, entity_id: str, entity: Any, origin: Origin) -> None:
        self.items.setdefault(kind, {})[entity_id] = entity
        self.origin.setdefault(kind, {})[entity_id] = origin

    def ids(self, kind: str) -> set[str]:
        return set(self.items.get(kind, {}))

    def all(self, kind: str) -> dict[str, Any]:
        return self.items.get(kind, {})

    def get(self, kind: str, entity_id: str) -> Any | None:
        return self.items.get(kind, {}).get(entity_id)

    def origin_of(self, kind: str, entity_id: str) -> Origin | None:
        return self.origin.get(kind, {}).get(entity_id)

    def count(self, kind: str) -> int:
        return len(self.items.get(kind, {}))

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.items.values())


def iter_yaml_files(data_dir: Path) -> list[tuple[Path, str, str]]:
    """按目录约定枚举 ``data/`` 下的 YAML 文件。

    返回 ``(绝对路径, 相对 data 的路径, kind)``。以 ``_`` 开头的文件（模板）跳过。

    只走进 :data:`KINDS` 里登记的目录（``data/card_images/`` 这类非数据目录直接跳过），
    并优先用 ``os.scandir``：``Path.rglob`` 在 Windows 上会为每个条目做多次
    ``is_file`` / ``relative_to``，几百个文件就会明显拖慢整页渲染
    （管理页每个请求都要按文件指纹判断缓存是否失效）。
    """
    found: list[tuple[Path, str, str]] = []
    for spec in KINDS:
        root = data_dir / spec.dirname
        if not root.is_dir():
            continue
        # (当前目录, 相对 data_dir 的前缀)
        stack: list[tuple[Path, str]] = [(root, spec.dirname)]
        while stack:
            directory, prefix = stack.pop()
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)
            except OSError:  # pragma: no cover - 权限/竞态
                continue
            for entry in children:
                name = entry.name
                if name.startswith("_"):
                    continue
                rel = f"{prefix}/{name}"
                try:
                    is_dir = entry.is_dir()
                except OSError:  # pragma: no cover
                    continue
                if is_dir:
                    stack.append((Path(entry.path), rel))
                    continue
                if not name.lower().endswith(YAML_SUFFIXES):
                    continue
                # 从 data/ 往下第一段决定 kind（支持 data/generals/guozhan/xxx.yaml）
                top = rel.split("/", 1)[0]
                kind_spec = KIND_BY_DIRNAME.get(top)
                if kind_spec is None:
                    continue
                found.append((Path(entry.path), rel, kind_spec.kind))
    return found


def _top_level_lines(text: str) -> list[int]:
    """取出每个顶层实体在文件中的起始行号（1-based）。"""
    try:
        node = yaml.compose(text, Loader=_SafeLoader)
    except yaml.YAMLError:
        return []
    if node is None:
        return []
    if isinstance(node, yaml.SequenceNode):
        return [child.start_mark.line + 1 for child in node.value]
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if getattr(key_node, "value", None) == "items" and isinstance(
                value_node, yaml.SequenceNode
            ):
                return [child.start_mark.line + 1 for child in value_node.value]
        return [node.start_mark.line + 1]
    return [1]


def _first_content_line(text: str) -> int:
    """单个映射的起始行号，不额外解析一遍 YAML。

    绝大多数数据文件是“一个文件一条记录”，这种情况下的起始行就是第一个
    既不是空行、也不是注释、也不是 ``---`` 的行。
    """
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        return number
    return 1


def load_yaml_file(path: Path, rel: str, kind: str) -> tuple[list[RawEntity], list[Issue]]:
    """读取单个 YAML 文件，返回原始实体与文件级问题。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 极少发生
        return [], [Issue("error", rel, "-", f"文件读取失败：{exc}")]

    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    try:
        data = yaml.load(text, Loader=_SafeLoader)
    except yaml.YAMLError as exc:
        line = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = mark.line + 1
        return [], [Issue("error", rel, "-", f"YAML 语法错误：{exc}", line=line)]

    if data is None:
        return [], []

    multi = isinstance(data, list) or (
        isinstance(data, dict) and isinstance(data.get("items"), list)
    )
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        return [], [
            Issue(
                "error",
                rel,
                "-",
                "顶层结构必须是映射、列表，或 {items: [...]} 包装；"
                f"当前是 {type(data).__name__}",
            )
        ]

    # 单条映射不必再解析一遍（compose）就能拿到起始行
    lines = _top_level_lines(text) if multi else [_first_content_line(text)]
    entities: list[RawEntity] = []
    issues: list[Issue] = []
    for index, item in enumerate(items):
        line = lines[index] if index < len(lines) else (lines[0] if lines else 1)
        if not isinstance(item, dict):
            issues.append(
                Issue(
                    "error",
                    rel,
                    f"items[{index}]",
                    f"每个条目必须是字段映射；当前是 {type(item).__name__}",
                    line=line,
                )
            )
            continue
        entities.append(RawEntity(kind=kind, data=item, origin=Origin(rel, line, sha256)))
    return entities, issues


def load_data_dir(data_dir: Path) -> tuple[list[RawEntity], list[Issue]]:
    """装载整个 ``data/`` 目录。"""
    if not data_dir.is_dir():
        return [], [Issue("error", str(data_dir), "-", "data 目录不存在")]
    entities: list[RawEntity] = []
    issues: list[Issue] = []
    for path, rel, kind in iter_yaml_files(data_dir):
        file_entities, file_issues = load_yaml_file(path, rel, kind)
        entities.extend(file_entities)
        issues.extend(file_issues)
    return entities, issues


def manifest_digest(data_dir: Path) -> str:
    """把全量 YAML 的相对路径 + 内容哈希汇总成一个指纹，便于判断构建物是否过期。"""
    parts: list[str] = []
    for path, rel, _kind in iter_yaml_files(data_dir):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        parts.append(f"{rel}:{digest}")
    payload = "\n".join(sorted(parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
