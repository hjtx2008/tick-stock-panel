"""monitor_ext_fields 默认值与 ext_presets 内置 schema 字段名一致。

回归:ext_hy_ths 的内置字段是「所属同花顺行业」, _MONITOR_EXT_FIELDS_DEFAULT["industry"]
必须与之完全一致, 否则后端 quote_service 等读 monitor_ext_fields.industry 时会拿到
找不到的字段, 监控/通知就静默丢数据 (CONTRIBUTING.md §3 数据契约红线)。
"""
from __future__ import annotations

import json
import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """把 preferences 的存储路径隔离到 tmp, 不污染真实 data/user_data/preferences.json。"""
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_industry_default_matches_preset_field(_isolated):
    """用户未设 monitor_ext_fields 时, industry 默认值必须等于 ext_hy_ths 的内置字段名。"""
    # 模拟空配置 (与本机 data/user_data/preferences.json 不含 monitor_ext_fields 的现状一致)
    _isolated.write_text(json.dumps({}), encoding="utf-8")

    fields = preferences.get_monitor_ext_fields()

    assert "concept" in fields and fields["concept"]
    assert "industry" in fields and fields["industry"]

    industry_field = fields["industry"]["field"]
    concept_field = fields["concept"]["field"]

    # 与 ext_presets.py:87 的 ExtField("所属同花顺行业", ...) 严格一致
    assert industry_field == "ext_hy_ths.所属同花顺行业", (
        f"industry 默认值含笔误(话顺), 应为 ext_hy_ths.所属同花顺行业, 实际 {industry_field!r}"
    )
    # concept 默认值长期正确, 顺手锁住避免回归
    assert concept_field == "ext_gn_ths.所属概念"


def test_industry_default_in_ext_presets(_isolated):
    """sanity: ext_presets 里 ext_hy_ths 的字段名确实是 '所属同花顺行业' (锁住上游真相)。"""
    from app.services.ext_presets import EXT_PRESETS

    preset = next((p for p in EXT_PRESETS if p["id"] == "ext_hy_ths"), None)
    assert preset is not None, "ext_hy_ths preset 应当存在"
    field_names = {f["name"] for f in preset["fields"]}
    assert "所属同花顺行业" in field_names
    # 防御性: 旧笔误字段名不该出现
    assert "所属同话顺行业" not in field_names
