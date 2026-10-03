"""校验器测试：交叉引用、重复 id、版本链、时点关系等。"""

from __future__ import annotations

from pathlib import Path

from conftest import SOURCE_YAML, error_text, replace_in, warning_text, write_dataset

from sgs_helper.validate import validate_data_dir


def test_synth_dataset_is_clean(synth: Path) -> None:
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    assert result.warnings == [], warning_text(result)


def test_real_dataset_is_clean(data_dir: Path) -> None:
    result = validate_data_dir(data_dir)
    assert result.ok, error_text(result)
    assert result.counts()["skill_version"] >= 1
    assert result.counts_line()


# ---------------------------------------------------------------- 错误


def test_duplicate_id_is_detected(synth: Path) -> None:
    original = (synth / "products" / "demo_a.yaml").read_text(encoding="utf-8")
    (synth / "products" / "demo_a_copy.yaml").write_text(original, encoding="utf-8")
    result = validate_data_dir(synth)
    assert not result.ok
    assert "id 重复" in error_text(result)
    assert "demo_a_copy.yaml" in error_text(result)


def test_missing_skill_reference_is_detected(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    text = error_text(result)
    assert "skill_missing" in text
    assert "skills[0].skill_id" in text
    assert "data/skills/" in text


def test_missing_product_reference_is_detected(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "product_id: product_demo_a",
        "product_id: product_missing",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "product_missing" in error_text(result)
    assert "product_id" in error_text(result)


def test_wrong_kind_reference_gives_hint(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "product_id: product_demo_a",
        "product_id: card_demo",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "请检查字段是否写错" in error_text(result)


def test_empty_reference_element_is_detected(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "skill_id: skill_demo",
        'skill_id: ""',
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "skills[0].skill_id" in error_text(result)


def test_self_reference_is_detected(synth: Path) -> None:
    replace_in(
        synth / "keywords" / "demo2.yaml", "definition: 第二个演示关键词\n",
        "definition: 第二个演示关键词\nrelated_keyword_ids:\n  - keyword_demo2\n",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "自引用" in error_text(result)


def test_previous_version_must_belong_to_same_skill(synth: Path) -> None:
    replace_in(
        synth / "skill_versions" / "demo_identity_v2.yaml",
        "previous_version_id: skill_demo_identity_v1",
        "previous_version_id: skill_demo_other_v1",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "属于技能 skill_demo_other" in error_text(result)


def test_previous_version_cycle_is_detected(synth: Path) -> None:
    replace_in(
        synth / "skill_versions" / "demo_identity_v1.yaml",
        "effective_to: 2024-12-31",
        "effective_to: 2024-12-31\nprevious_version_id: skill_demo_identity_v2",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "版本链成环" in error_text(result)


def test_timing_parent_cycle_is_detected(synth: Path) -> None:
    replace_in(
        synth / "timings" / "demo_next.yaml", "id: timing_demo_next\n",
        "id: timing_demo_next\nparent_id: timing_demo\n",
    )
    replace_in(
        synth / "timings" / "demo.yaml", "id: timing_demo\n",
        "id: timing_demo\nparent_id: timing_demo_next\n",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "parent 链成环" in error_text(result)


# ---------------------------------------------------------------- 警告


def test_duplicate_reference_in_list_is_warned(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml", "skills:\n  - skill_id: skill_demo\n",
        "skills:\n  - skill_id: skill_demo\n  - skill_id: skill_demo\n",
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "列表内重复引用" in warning_text(result)


def test_overlapping_effective_range_is_warned(synth: Path) -> None:
    replace_in(
        synth / "skill_versions" / "demo_identity_v2.yaml",
        "product_id: null\neffective_from: 2025-01-01",
        "product_id: product_demo_b\neffective_from: 2024-06-01",
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "生效区间重叠" in warning_text(result)


def test_hp_step_is_warned(synth: Path) -> None:
    replace_in(synth / "general_variants" / "demo_beta_identity.yaml", "hp: 3\n", "hp: 3.3\n")
    result = validate_data_dir(synth)
    assert result.ok
    assert "不是 0.5 的整数倍" in warning_text(result)


def test_duplicate_card_combo_is_warned(synth: Path) -> None:
    original = (synth / "card_printings" / "demo_a_spade_1.yaml").read_text(encoding="utf-8")
    (synth / "card_printings" / "demo_a_spade_1_again.yaml").write_text(
        original.replace("card_demo_a_spade_1", "card_demo_a_spade_1_again"), encoding="utf-8"
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "存在多张" in warning_text(result)


def test_skill_without_version_is_warned(synth: Path) -> None:
    (synth / "skills" / "lonely.yaml").write_text(
        f"id: skill_lonely\nname: 无版本技能\n{SOURCE_YAML}", encoding="utf-8"
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "没有任何 SkillVersion" in warning_text(result)


def test_missing_source_is_warned(synth: Path) -> None:
    path = synth / "general_variants" / "demo_beta_identity.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(SOURCE_YAML, ""), encoding="utf-8")
    result = validate_data_dir(synth)
    assert result.ok
    assert "未记录来源" in warning_text(result)


def test_timing_previous_next_asymmetry_is_warned(synth: Path) -> None:
    replace_in(
        synth / "timings" / "demo_next.yaml", "previous_ids:\n  - timing_demo\n", ""
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "未反向包含" in warning_text(result)


def test_mode_mismatch_with_product_is_warned(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml", "product_id: product_demo_a",
        "product_id: product_demo_b",
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "不一致" in warning_text(result)


def test_skill_without_version_in_general_mode_is_warned(synth: Path) -> None:
    replace_in(synth / "general_variants" / "demo_beta_identity.yaml", "mode: identity", "mode: ol")
    result = validate_data_dir(synth)
    assert result.ok
    assert "没有任何 SkillVersion" in warning_text(result)


# ---------------------------------------------------------------- 汇总输出


def test_report_format_has_path_field_reason(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    report = validate_data_dir(synth).format_report()
    assert "data/general_variants/demo_alpha_guozhan.yaml" in report
    assert "skills[0].skill_id" in report
    assert "skill_missing" in report
    assert "校验失败" in report


def test_counts_line(synth: Path) -> None:
    result = validate_data_dir(synth)
    line = result.counts_line()
    assert "实体产品 2" in line
    assert "技能版本 4" in line


def test_helper_write_dataset_is_reusable(tmp_path: Path) -> None:
    from conftest import SYNTHETIC

    root = write_dataset(tmp_path / "other", {"products/p.yaml": SYNTHETIC["products/demo_a.yaml"]})
    assert (root / "products" / "p.yaml").exists()
