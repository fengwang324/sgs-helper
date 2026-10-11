from __future__ import annotations

import json
from pathlib import Path

import pytest

from sgs_helper.validate import validate_data_dir
from scripts.build_frontend import InvalidDataError, export_catalog


def _read_js_catalog(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    prefix = "window.SGS_CATALOG = "
    assert text.startswith(prefix)
    assert text.endswith(";\n")
    return json.loads(text[len(prefix) : -2])


def test_exports_complete_current_catalog(data_dir: Path, tmp_path: Path) -> None:
    result = validate_data_dir(data_dir)
    assert result.ok

    output = tmp_path / "web"
    exported = export_catalog(data_dir, output)
    catalog = json.loads((output / "catalog.json").read_text(encoding="utf-8"))

    assert exported.counts() == result.counts()
    assert catalog["schema_version"] == 7
    assert catalog["counts"] == {
        "products": 1,
        "generals": 163,
        "general_variants": 163,
        "skills": 291,
        "skill_versions": 291,
        "cards": 114,
        "faq": 25,
        "keywords": 43,
    }
    for kind, collection in (
        ("product", "products"),
        ("general", "generals"),
        ("general_variant", "general_variants"),
        ("skill", "skills"),
        ("skill_version", "skill_versions"),
        ("card", "cards"),
        ("faq", "faq"),
        ("keyword", "keywords"),
    ):
        assert {item["id"] for item in catalog[collection]} == result.bundle.ids(kind)

    assert len(catalog["faq"]) == 25
    assert len(catalog["keywords"]) == 43
    assert {item["id"] for item in catalog["faq"]} == result.bundle.ids("faq")
    assert {item["id"] for item in catalog["keywords"]} == result.bundle.ids("keyword")

    versions = {item["id"]: item for item in catalog["skill_versions"]}
    for general_variant in catalog["general_variants"]:
        for skill_ref in general_variant["skills"]:
            version_id = skill_ref.get("version_id")
            if version_id is not None:
                assert version_id in versions
                assert versions[version_id]["skill_id"] == skill_ref["skill_id"]

    cards_by_id = {item["id"]: item for item in catalog["cards"]}
    assert len(cards_by_id) == 114
    for card_id, card in cards_by_id.items():
        assert card["image"] == result.bundle.get("card", card_id).image

    assert _read_js_catalog(output / "catalog.js") == catalog


def test_invalid_data_does_not_overwrite_existing_catalog(synth: Path, tmp_path: Path) -> None:
    output = tmp_path / "web"
    output.mkdir()
    json_path = output / "catalog.json"
    js_path = output / "catalog.js"
    json_path.write_text("previous json\n", encoding="utf-8")
    js_path.write_text("previous js\n", encoding="utf-8")

    bad_product = synth / "products" / "demo_a.yaml"
    bad_product.write_text(
        bad_product.read_text(encoding="utf-8") + "unknown_field: rejected\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidDataError):
        export_catalog(synth, output)

    assert json_path.read_text(encoding="utf-8") == "previous json\n"
    assert js_path.read_text(encoding="utf-8") == "previous js\n"
