"""校验器测试：交叉引用、重复 id、版本链、武将牌系列归属、卡牌一致性、卡图存在性。

精简后 ``related_*`` / ``parent_id`` / ``previous_ids`` / ``next_ids`` / ``effective_*``
等字段全部删除：写了就是未知字段（error），而不是走原来的关系校验。

卡牌单层化之后，``CardPrinting`` 时代的「同花色点数重复 / 漏填花色」警告随该层一起消失，
现在只管 `card_set` 必须被某个产品声明、卡图必须存在、``effect.skills[].version_id``
必须属于同一个技能。**分类取值不再受任何预设白名单约束**：二 / 三级分类可以自由填写、
也可以留空，校验器不提示「不在登记的分类里」，也不要求“补进 constants”。
"""

from __future__ import annotations

from pathlib import Path

from conftest import error_text, replace_in, warning_text, write_dataset

from sgs_helper.validate import validate_data_dir


def test_synth_dataset_is_clean(synth: Path) -> None:
    # conftest 的合成武将版本引用了 variant_demo_alpha_guozhan.jpg；卡图存在性由
    # validate._check_images 单独校验（见 test_missing_card_image_is_warned），
    # 这里先补上文件，才能断言“其余校验零警告”。
    images = synth / "card_images"
    images.mkdir(exist_ok=True)
    (images / "variant_demo_alpha_guozhan.jpg").write_bytes(b"")
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    assert result.warnings == [], warning_text(result)


def test_real_dataset_is_clean(data_dir: Path) -> None:
    result = validate_data_dir(data_dir)
    assert result.ok, error_text(result)
    assert result.bundle.total > 1000
    assert result.counts()["skill_version"] >= 1
    assert result.counts_line()


def test_real_dataset_has_no_warnings_or_empty_skills(data_dir: Path) -> None:
    """清理无描述的空技能后，真实数据没有警告，也没有无版本技能。"""
    result = validate_data_dir(data_dir)
    assert result.ok, error_text(result)
    assert result.warnings == [], warning_text(result)

    skills_without_version = {
        skill_id
        for skill_id in result.bundle.all("skill")
        if not any(
            sv.skill_id == skill_id for sv in result.bundle.all("skill_version").values()
        )
    }
    assert skills_without_version == set()


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


def test_explicit_version_must_belong_to_same_skill(synth: Path) -> None:
    """绑定到别的技能的版本必须报错（文本由它决定，不能张冠李戴）。"""
    replace_in(
        synth / "general_variants" / "demo_alpha_identity.yaml",
        "version_id: skill_demo_identity_v1",
        "version_id: skill_demo_other_v1",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "绑定" in error_text(result)
    assert "skill_demo_other" in error_text(result)


def test_self_reference_is_detected(synth: Path) -> None:
    path = synth / "skill_versions" / "demo_identity_v1.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "previous_version_id: skill_demo_identity_v1\n",
        encoding="utf-8",
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
    path = synth / "skill_versions" / "demo_identity_v1.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "previous_version_id: skill_demo_identity_v2\n",
        encoding="utf-8",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    assert "版本链成环" in error_text(result)


def test_removed_fields_are_rejected_not_ignored(synth: Path) -> None:
    """旧字段必须报未知字段，不能静默丢弃（keyword 的 mode 只是其中一个代表）。"""
    replace_in(
        synth / "keywords" / "demo.yaml", "id: keyword_demo\n",
        "id: keyword_demo\nmode: common\n",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    text = error_text(result)
    assert "mode" in text
    assert "未知字段" in text


# ---------------------------------------------------------------- 卡牌错误 / 警告


def test_missing_card_skill_reference_is_detected(synth: Path) -> None:
    replace_in(
        synth / "cards" / "demo_trick.yaml",
        "skill_id: skill_demo",
        "skill_id: skill_missing",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    text = error_text(result)
    assert "skill_missing" in text
    assert "effect.skills[0].skill_id" in text
    assert "data/skills/" in text


def test_empty_card_skill_reference_is_detected(synth: Path) -> None:
    replace_in(synth / "cards" / "demo_trick.yaml", "skill_id: skill_demo", 'skill_id: ""')
    result = validate_data_dir(synth)
    assert not result.ok
    assert "effect.skills[0].skill_id" in error_text(result)


def test_card_effect_version_must_belong_to_same_skill(synth: Path) -> None:
    """卡牌效果绑定的版本必须属于同一个技能（与武将版本同规则）。"""
    replace_in(
        synth / "cards" / "demo_trick.yaml",
        "version_id: skill_demo_guozhan_v1",
        "version_id: skill_demo_other_v1",
    )
    result = validate_data_dir(synth)
    assert not result.ok
    text = error_text(result)
    assert "effect.skills[0].version_id" in text
    assert "skill_demo_other" in text


def test_card_categories_are_free_and_may_be_blank(synth: Path) -> None:
    """分类不受预设白名单约束：二 / 三级可以自由填写，也可以留空，校验器都不提示。

    （历史上这里有一条「基本牌三级分类应为牌族」和一条「指示物二级分类未登记」的警告，
    两条都已删除：录入页本来就支持自由输入，把预设值当成规范会自相矛盾。）
    """
    replace_in(
        synth / "cards" / "demo_card.yaml",
        "category_2: 基本牌\ncategory_3: 杀\n",
        "category_2: 自定义二级\ncategory_3: 自定义三级\n",
    )
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    text = warning_text(result)
    assert "基本牌的三级分类应为牌族" not in text
    assert "指示物的二级分类" not in text

    # 指示物 + 全新的二级分类：一样不提示
    replace_in(
        synth / "cards" / "demo_trick.yaml",
        "category_1: 游戏牌",
        "category_1: 指示物",
    )
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    assert "指示物的二级分类" not in warning_text(result)


def test_card_subcategories_may_be_blank(synth: Path) -> None:
    """二 / 三级分类允许留空：只填一级（或只填一 / 二级）也是合法数据。"""
    replace_in(
        synth / "cards" / "demo_card.yaml",
        "category_2: 基本牌\ncategory_3: 杀\n",
        "category_2: ''\ncategory_3: ''\n",
    )
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    card = result.bundle.get("card", "card_demo")
    assert (card.category_2, card.category_3) == ("", "")


def test_card_set_must_be_declared_by_a_product(synth: Path) -> None:
    replace_in(synth / "cards" / "demo_trick.yaml", "card_set: 演示牌堆", "card_set: 幻想的拓展包")
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    text = warning_text(result)
    assert "不在任何产品声明" in text
    assert "幻想的拓展包" in text


def test_indicator_is_a_category_not_a_card_set(synth: Path) -> None:
    """「指示物」是 category_1，不是游戏牌拓展包，校验器要给专门提示。"""
    replace_in(synth / "cards" / "demo_trick.yaml", "card_set: 演示牌堆", "card_set: 指示物")
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    assert "不是游戏牌拓展包" in warning_text(result)


def test_missing_card_image_is_warned(synth: Path) -> None:
    replace_in(synth / "cards" / "demo_card.yaml", 'image: ""', "image: 不存在的卡图.jpg")
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    text = warning_text(result)
    assert "卡图" in text and "不存在" in text
    assert "不存在的卡图.jpg" in text


# ---------------------------------------------------------------- 警告


def test_duplicate_reference_in_list_is_warned(synth: Path) -> None:
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml", "skills:\n  - skill_id: skill_demo\n",
        "skills:\n  - skill_id: skill_demo\n  - skill_id: skill_demo\n",
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "列表内重复引用" in warning_text(result)


def test_general_set_must_come_from_product(synth: Path) -> None:
    """GeneralSet 实体删除后新增的对账：系列名必须出现在所属产品的 general_sets 里。"""
    replace_in(
        synth / "general_variants" / "demo_alpha_guozhan.yaml",
        "general_set: 演示武将牌系列",
        "general_set: 不存在的系列",
    )
    result = validate_data_dir(synth)
    assert result.ok
    text = warning_text(result)
    assert "不在所属产品" in text
    assert "不存在的系列" in text


def test_hp_step_is_warned(synth: Path) -> None:
    replace_in(synth / "general_variants" / "demo_beta_identity.yaml", "hp: 3\n", "hp: 3.3\n")
    result = validate_data_dir(synth)
    assert result.ok
    assert "不是 0.5 的整数倍" in warning_text(result)


def test_skill_without_version_is_warned(synth: Path) -> None:
    (synth / "skills" / "lonely.yaml").write_text(
        "id: skill_lonely\nname: 无版本技能\n", encoding="utf-8"
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "没有任何 SkillVersion" in warning_text(result)


def test_general_without_variant_is_warned(synth: Path) -> None:
    (synth / "generals" / "lonely.yaml").write_text(
        "id: general_lonely\nname: 孤儿武将\n", encoding="utf-8"
    )
    result = validate_data_dir(synth)
    assert result.ok
    assert "还没有任何版本" in warning_text(result)


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


def test_common_version_counts_as_available_text(synth: Path) -> None:
    """common 版本是回退候选：只要有一条，就不再报“没有可用文本”。"""
    from conftest import SOURCE_YAML

    replace_in(synth / "general_variants" / "demo_beta_identity.yaml", "mode: identity", "mode: ol")
    (synth / "skill_versions" / "demo_common_v1.yaml").write_text(
        f"id: skill_demo_common_v1\nskill_id: skill_demo\nmode: common\n"
        f"text: 通用版演示技能文本\n{SOURCE_YAML}",
        encoding="utf-8",
    )
    result = validate_data_dir(synth)
    assert result.ok, error_text(result)
    assert "没有任何 SkillVersion" not in warning_text(result)


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
