"""YAML 装载层测试。"""

from __future__ import annotations

from pathlib import Path

from sgs_helper.loader import iter_yaml_files, load_data_dir, load_yaml_file, manifest_digest


def test_real_data_dir_loads(data_dir: Path) -> None:
    entities, issues = load_data_dir(data_dir)
    assert issues == []
    kinds = {entity.kind for entity in entities}
    assert {
        "product",
        "general",
        "general_variant",
        "skill",
        "skill_version",
        "card",
        "card_printing",
        "faq",
        "keyword",
        "timing",
    } <= kinds
    # GeneralSet 实体与目录已删除
    assert "general_set" not in kinds


def test_iter_yaml_files_skips_templates(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "products").mkdir(parents=True)
    (data / "products" / "a.yaml").write_text("id: product_a\n", encoding="utf-8")
    (data / "products" / "_template.yaml").write_text("id: x\n", encoding="utf-8")
    (data / "products" / "note.txt").write_text("x", encoding="utf-8")
    (data / "_templates").mkdir()
    (data / "_templates" / "p.yaml").write_text("id: y\n", encoding="utf-8")

    found = [rel for _path, rel, _kind in iter_yaml_files(data)]
    assert found == ["products/a.yaml"]


def test_unknown_directories_are_ignored(tmp_path: Path) -> None:
    """只认 KIND_BY_NAME 里的目录名；删掉的 general_sets 等目录即使存在也不装载。"""
    data = tmp_path / "data"
    (data / "products").mkdir(parents=True)
    (data / "products" / "a.yaml").write_text("id: product_a\n", encoding="utf-8")
    (data / "general_sets").mkdir()
    (data / "general_sets" / "gs.yaml").write_text("id: gs_x\nname: X\n", encoding="utf-8")
    (data / "nonsense").mkdir()
    (data / "nonsense" / "y.yaml").write_text("id: y\n", encoding="utf-8")

    found = [(rel, kind) for _path, rel, kind in iter_yaml_files(data)]
    assert found == [("products/a.yaml", "product")]


def test_single_mapping_file(synth: Path) -> None:
    entities, issues = load_yaml_file(
        synth / "products" / "demo_a.yaml", "products/demo_a.yaml", "product"
    )
    assert issues == []
    assert len(entities) == 1
    assert entities[0].data["id"] == "product_demo_a"
    assert entities[0].origin.line == 1
    assert len(entities[0].origin.sha256) == 64


def test_list_file(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text(
        "- id: timing_a\n  name: A\n  definition: x\n"
        "- id: timing_b\n  name: B\n  definition: y\n",
        encoding="utf-8",
    )
    entities, issues = load_yaml_file(path, "timings/list.yaml", "timing")
    assert issues == []
    assert [e.data["id"] for e in entities] == ["timing_a", "timing_b"]
    assert [e.origin.line for e in entities] == [1, 4]


def test_items_wrapper_file(tmp_path: Path) -> None:
    path = tmp_path / "items.yaml"
    path.write_text("items:\n  - id: timing_a\n  - id: timing_b\n", encoding="utf-8")
    entities, issues = load_yaml_file(path, "timings/items.yaml", "timing")
    assert issues == []
    assert [e.origin.line for e in entities] == [2, 3]


def test_yaml_syntax_error_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("id: product_a\nname: [oops\n", encoding="utf-8")
    entities, issues = load_yaml_file(path, "products/broken.yaml", "product")
    assert entities == []
    assert len(issues) == 1
    assert issues[0].is_error
    assert "YAML 语法错误" in issues[0].message
    assert issues[0].line is not None


def test_scalar_top_level_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "scalar.yaml"
    path.write_text("just a string\n", encoding="utf-8")
    entities, issues = load_yaml_file(path, "products/scalar.yaml", "product")
    assert entities == []
    assert issues and "顶层结构" in issues[0].message


def test_non_mapping_item_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "mixed.yaml"
    path.write_text("- id: product_a\n- 42\n", encoding="utf-8")
    entities, issues = load_yaml_file(path, "products/mixed.yaml", "product")
    assert len(entities) == 1
    assert issues and "items[1]" in issues[0].field


def test_empty_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("# 只有注释\n", encoding="utf-8")
    entities, issues = load_yaml_file(path, "products/empty.yaml", "product")
    assert entities == [] and issues == []


def test_manifest_digest_changes_with_content(synth: Path) -> None:
    first = manifest_digest(synth)
    target = synth / "skills" / "demo.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "review_note: 改动\n", encoding="utf-8")
    assert manifest_digest(synth) != first
