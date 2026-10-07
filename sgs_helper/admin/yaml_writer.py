"""把实体写回 YAML 源文件（本地管理网页的唯一写盘入口）。

设计目标：**写出来的 YAML 仍然是人可以读、可以手工继续维护的**，而不是
机器序列化格式。因此：

- 字段顺序固定为 Pydantic 模型的字段顺序（``model_fields``），不按字母排序；
- 含换行的字符串、以及规则文本类字段用块状标量（``|`` / ``|-``），
  绝不把长中文折行（折行会被 YAML 解析成空格，等于篡改文本）；
- 保留文件顶部人工维护的注释块（``# ...``）；
- 保留原文件的组织方式：单条映射 / 列表 / ``items:`` 包装都按原样写回；
- 以 ``os.replace`` 原子落盘，避免写一半留下半截 YAML。

本模块只负责“单个文件怎么读写”，不负责校验；调用方必须先校验再调用。
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..constants import KIND_BY_NAME
from ..schema import MODEL_BY_KIND

#: 用块状标量写出的字段（规则文本，长且需要保证不被折行）
BLOCK_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        "text",
        "question",
        "answer",
        "definition",
        "description",
    }
)

#: source 内嵌结构的固定键序（Source 只保留 type + reference）
SOURCE_KEY_ORDER: tuple[str, ...] = ("type", "reference")

#: 一个文件最多放多少条实体（防止误把大列表塞进单文件；超过给出警告由调用方处理）
YAML_SUFFIX = ".yaml"


class BlockStr(str):
    """标记：该字符串用块状标量（``|`` / ``|-``）输出。"""


class _ReadableDumper(yaml.SafeDumper):
    """不做 key 排序、不折行的 Dumper。"""

    def increase_indent(self, flow=False, indentless=False):  # noqa: D102
        # 让列表项相对键缩进（YAML 官方风格），例如：
        #     - 集智
        return super().increase_indent(flow, False)


def _represent_str(dumper: yaml.Dumper, data: str):
    """字符串输出策略。

    块状标量只给 ``|`` 这一个样式：PyYAML 会根据字符串尾部的换行数量
    自己加上 ``-``/``+`` chomping 指示符，所以 ``x\\ny`` -> ``|-``、
    ``x\\ny\\n`` -> ``|``、``x\\ny\\n\\n`` -> ``|+``，不会丢尾换行。
    """
    forced = isinstance(data, BlockStr)
    text = str(data)
    if forced or "\n" in text:
        # 很短的单行文本不必强上块状标量，保持一行可读
        if forced and "\n" not in text and len(text) <= 30:
            return dumper.represent_scalar("tag:yaml.org,2002:str", text)
        return dumper.represent_scalar("tag:yaml.org,2002:str", text, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", text)


def _represent_date(dumper: yaml.Dumper, data: dt.date):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data.isoformat())


_ReadableDumper.add_representer(str, _represent_str)
_ReadableDumper.add_representer(BlockStr, _represent_str)
_ReadableDumper.add_representer(dt.date, _represent_date)
_ReadableDumper.add_representer(dt.datetime, _represent_date)


def dump_yaml(payload: Any) -> str:
    """把 Python 结构 dump 成可读 YAML 文本（结尾保证只有 1 个换行）。"""
    text = yaml.dump(
        payload,
        Dumper=_ReadableDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=10 ** 6,  # 关掉折行：YAML 折行会改变字符串内容（中文尤其危险）
    )
    return text if text.endswith("\n") else text + "\n"


# ---------------------------------------------------------------- 读取结构


@dataclass
class FileDoc:
    """一个 YAML 文件的组织方式。"""

    nodes: list[dict] = field(default_factory=list)
    #: single（单个映射）| list（顶层列表）| items（{items: [...]} 包装）
    wrapper: str = "single"
    #: 文件顶部的人工注释块（含空行），原样保留
    header: str = ""
    #: items 包装时的额外顶层键
    extra: dict = field(default_factory=dict)

    def index_of(self, entity_id: str) -> int | None:
        for index, node in enumerate(self.nodes):
            if isinstance(node, dict) and node.get("id") == entity_id:
                return index
        return None

    def payload(self) -> Any:
        if self.wrapper == "list":
            return self.nodes
        if self.wrapper == "items":
            payload = dict(self.extra)
            payload["items"] = self.nodes
            return payload
        return self.nodes[0] if self.nodes else {}


def leading_comment_block(text: str) -> str:
    """取出文件顶部连续的空行/注释行（人工维护的说明，必须保留）。"""
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        break
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)


def read_doc(path: Path) -> FileDoc:
    """读一个 YAML 文件并识别其组织方式。文件不存在时返回空 doc。"""
    path = Path(path)
    if not path.exists():
        return FileDoc()
    text = path.read_text(encoding="utf-8")
    doc = FileDoc(header=leading_comment_block(text))
    data = yaml.safe_load(text)
    if data is None:
        return doc
    if isinstance(data, list):
        doc.wrapper = "list"
        doc.nodes = [node for node in data if isinstance(node, dict)]
        return doc
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        doc.wrapper = "items"
        doc.nodes = [node for node in data["items"] if isinstance(node, dict)]
        doc.extra = {k: v for k, v in data.items() if k != "items"}
        return doc
    if isinstance(data, dict):
        doc.wrapper = "single"
        doc.nodes = [data]
        return doc
    raise ValueError(f"{path} 的顶层结构无法识别（{type(data).__name__}）")


def render_doc(doc: FileDoc) -> str:
    """把 FileDoc 渲染成带注释头的可读 YAML 文本。"""
    body = dump_yaml(doc.payload())
    if not doc.header:
        return body
    if doc.wrapper == "single":
        return f"{doc.header}\n{body}"
    return f"{doc.header}\n\n{body}"


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + fsync + os.replace：要么是旧的，要么是完整的新的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-admin")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- 组装实体


def entity_to_yaml_dict(kind: str, entity: Any) -> dict:
    """把 Pydantic 实体变成“按模型字段顺序排列、适合写 YAML”的 dict。"""
    data = entity.model_dump(mode="json") if hasattr(entity, "model_dump") else dict(entity)
    order = list(MODEL_BY_KIND[kind].model_fields)
    ordered: dict[str, Any] = {name: data.get(name) for name in order if name in data}
    for name in order:  # 模型里存在但 dump 没给的字段，补 null 保持完整
        ordered.setdefault(name, None)

    for name in BLOCK_TEXT_FIELDS:
        value = ordered.get(name)
        if isinstance(value, str) and value.strip():
            ordered[name] = BlockStr(value)

    # Card.effect 是嵌套映射：text 按规则文本用块状标量，skills 固定 skill_id/version_id 键序
    effect = ordered.get("effect")
    if isinstance(effect, dict):
        effect = dict(effect)
        text = effect.get("text")
        if isinstance(text, str) and text.strip():
            effect["text"] = BlockStr(text)
        skills = effect.get("skills")
        if isinstance(skills, list):
            effect["skills"] = [
                {"skill_id": ref.get("skill_id"), "version_id": ref.get("version_id")}
                for ref in skills
                if isinstance(ref, dict)
            ]
        ordered["effect"] = effect

    source = ordered.get("source")
    if isinstance(source, dict):
        ordered["source"] = {key: source.get(key, "") for key in SOURCE_KEY_ORDER}
    return ordered


# ---------------------------------------------------------------- 写文件


def entity_file_path(data_dir: Path, kind: str, entity_id: str) -> Path:
    """新增实体时的默认落盘位置：``data/<目录>/<id>.yaml``（一条记录一个文件）。"""
    return Path(data_dir) / KIND_BY_NAME[kind].dirname / f"{entity_id}{YAML_SUFFIX}"


def upsert_entity(
    path: Path,
    kind: str,
    entity: Any,
    *,
    old_id: str | None = None,
) -> Path:
    """把实体写入 ``path``：已存在则原位替换，不存在则追加。

    ``old_id`` 用于“修改了 id”的场景：先把旧 id 从文件里移除。
    """
    path = Path(path)
    doc = read_doc(path)
    if doc.wrapper == "single" and doc.nodes:
        doc.wrapper = "single"

    entity_id = entity.id if hasattr(entity, "id") else entity["id"]
    payload = entity_to_yaml_dict(kind, entity)

    if old_id and old_id != entity_id:
        index = doc.index_of(old_id)
        if index is not None:
            doc.nodes.pop(index)

    index = doc.index_of(entity_id)
    if index is None:
        doc.nodes.append(payload)
    else:
        doc.nodes[index] = payload

    _atomic_write(path, render_doc(doc))
    return path


def remove_entity(path: Path, entity_id: str) -> bool:
    """从 ``path`` 删除实体。返回 ``True`` 表示文件已无内容、需要删除该文件。"""
    path = Path(path)
    doc = read_doc(path)
    index = doc.index_of(entity_id)
    if index is not None:
        doc.nodes.pop(index)

    if not doc.nodes and not doc.extra:
        if path.exists():
            path.unlink()
        return True

    if doc.wrapper == "single":
        # 单条文件里删掉唯一一条：文件没有意义了
        if path.exists():
            path.unlink()
        return True

    _atomic_write(path, render_doc(doc))
    return False
