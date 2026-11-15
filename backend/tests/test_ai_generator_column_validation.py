"""AI 策略的列引用静态校验 (2026-11-13 新增)。

背景: AI 生成的策略引用了指标流水线里不存在的列 (实测 vol_ma20 ——
流水线只有 vol_ma5 / vol_ma10)。保存时无人拦截, 跑到批量选股才炸;
且一个坏策略会让整个 run_all 抛 500, 其余 28 个正常策略的结果全部拿不到。

本文件锁住三件事:
- 幻觉列 (引用了不存在的列) 被 validate_code 拦下, 并给出可读错误;
- 合法用法 (真实列 / 虚拟因子 / 策略自算的内部列) 不被误杀;
- 校验不引入误报: 逗号多列写法、下划线中间量、alias 出来的列都放行。
"""
from __future__ import annotations

import pytest

from app.strategy.ai_generator import AIStrategyGenerator


@pytest.fixture
def gen() -> AIStrategyGenerator:
    return AIStrategyGenerator()


def _code(body: str, sid: str = "ai_test") -> str:
    return (
        "import polars as pl\n"
        f'META = {{"id": "{sid}", "name": "{sid}"}}\n'
        "def filter_history(df, params):\n"
        f"{body}\n"
    )


# ── 幻觉列必须被拦下 ────────────────────────────────────────

def test_hallucinated_column_is_rejected(gen: AIStrategyGenerator):
    """vol_ma20 不在 ENRICHED_COLUMNS 里 → 拦截。"""
    result = gen.validate_code(_code(
        '    return df.with_columns([pl.col("vol_ma20").alias("_x")])'
    ))
    assert result["valid"] is False
    assert "vol_ma20" in result["error"]
    assert "不存在的列" in result["error"]


def test_hallucinated_column_error_mentions_line_number(gen: AIStrategyGenerator):
    """错误信息带行号, 便于 AI 自我修复或用户定位。"""
    result = gen.validate_code(_code(
        '    return df.with_columns([pl.col("nonexistent_col").alias("_x")])'
    ))
    assert result["valid"] is False
    assert "行" in result["error"]


def test_unknown_column_in_filter_is_rejected(gen: AIStrategyGenerator):
    """filter 里引用不存在列同样要拦。"""
    result = gen.validate_code(_code(
        '    return df.filter(pl.col("made_up_ma") > 1)'
    ))
    assert result["valid"] is False
    assert "made_up_ma" in result["error"]


# ── 合法用法不得误杀 ────────────────────────────────────────

def test_real_indicator_column_passes(gen: AIStrategyGenerator):
    """流水线里真实存在的列 (vol_ma10) → 放行。"""
    result = gen.validate_code(_code(
        '    return df.filter(pl.col("vol_ma10") > 100)'
    ))
    assert result["valid"] is True, result.get("error")


def test_virtual_factor_column_passes(gen: AIStrategyGenerator):
    """受控虚拟因子 (ma20_bias) → 放行。"""
    result = gen.validate_code(_code(
        '    return df.filter(pl.col("ma20_bias") > 0)'
    ))
    assert result["valid"] is True, result.get("error")


def test_strategy_defined_internal_column_passes(gen: AIStrategyGenerator):
    """策略自己 with_columns 算出来的内部列 (_vol_ma20) → 放行。

    这正是 ai_mty5pjcw 的写法: 自己算 20 日均量而不是引用流水线列。
    """
    result = gen.validate_code(_code(
        '    return df.with_columns([\n'
        '        pl.col("volume").rolling_mean(20).over("symbol").alias("_vol_ma20"),\n'
        '    ])'
    ))
    assert result["valid"] is True, result.get("error")


def test_comma_separated_column_reference_passes(gen: AIStrategyGenerator):
    """pl.col("a, b") 多列写法 → 两个列都要校验, 真实列放行。"""
    result = gen.validate_code(_code(
        '    return df.with_columns([pl.col("open, close").alias("_oc")])'
    ))
    assert result["valid"] is True, result.get("error")


def test_comma_separated_with_bad_column_is_rejected(gen: AIStrategyGenerator):
    """多列写法里混入幻觉列 → 也要拦。"""
    result = gen.validate_code(_code(
        '    return df.with_columns([pl.col("open, vol_ma20").alias("_x")])'
    ))
    assert result["valid"] is False
    assert "vol_ma20" in result["error"]


def test_multi_argument_col_call_is_checked(gen: AIStrategyGenerator):
    """pl.col("a", "b") 多参数写法 → 每个参数都要校验。"""
    result = gen.validate_code(_code(
        '    return df.filter(pl.col("close", "vol_ma20") > 1)'
    ))
    assert result["valid"] is False
    assert "vol_ma20" in result["error"]


def test_multi_argument_col_call_with_real_columns_passes(gen: AIStrategyGenerator):
    """pl.col("a", "b") 两个都是真实列 → 放行。"""
    result = gen.validate_code(_code(
        '    return df.filter(pl.col("close", "volume") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


def test_bare_col_import_is_checked(gen: AIStrategyGenerator):
    """from polars import col 后的裸 col("xxx") → 同样要校验。"""
    code = (
        "from polars import col\n"
        'META = {"id": "ai_test", "name": "ai_test"}\n'
        "def filter_history(df, params):\n"
        '    return df.filter(col("vol_ma20") > 1)\n'
    )
    result = gen.validate_code(code)
    assert result["valid"] is False
    assert "vol_ma20" in result["error"]


def test_bare_col_import_with_real_column_passes(gen: AIStrategyGenerator):
    """裸 col("close") 引用真实列 → 放行。"""
    code = (
        "from polars import col\n"
        'META = {"id": "ai_test", "name": "ai_test"}\n'
        "def filter_history(df, params):\n"
        '    return df.filter(col("close") > 1)\n'
    )
    result = gen.validate_code(code)
    assert result["valid"] is True, result.get("error")


def test_alias_target_is_treated_as_defined(gen: AIStrategyGenerator):
    """alias("xxx") 创建的列名 → 视为已定义, 后续引用不报错。"""
    result = gen.validate_code(_code(
        '    df = df.with_columns([pl.col("volume").alias("my_custom_col")])\n'
        '    return df.filter(pl.col("my_custom_col") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


def test_underscore_prefixed_column_is_allowed(gen: AIStrategyGenerator):
    """下划线开头的中间量名 → 一律放行 (内部实现细节)。"""
    result = gen.validate_code(_code(
        '    return df.with_columns([pl.col("_some_helper").alias("_x")])'
    ))
    assert result["valid"] is True, result.get("error")


# ── 不影响其他校验路径 ──────────────────────────────────────

def test_syntax_error_still_reported(gen: AIStrategyGenerator):
    """语法错误仍走原有路径, 不被新校验掩盖。"""
    result = gen.validate_code("def broken(:\n    pass\n")
    assert result["valid"] is False
    assert "语法错误" in result["error"]


def test_valid_strategy_without_column_refs_passes(gen: AIStrategyGenerator):
    """不引用任何列的简单策略 → 放行。"""
    result = gen.validate_code(
        "import polars as pl\n"
        'META = {"id": "ai_test", "name": "ai_test"}\n'
        "def filter_history(df, params):\n"
        "    return df.head(10)\n"
    )
    assert result["valid"] is True, result.get("error")


# ── 截断文件检测 (2026-11-13 实测: AI 生成的文件末尾缺 return) ──

def test_truncated_strategy_without_return_is_rejected(gen: AIStrategyGenerator):
    """入口函数末尾不是 return → 判定为生成被截断, 拦截。

    实测: ai_mtyc4sun.py 末尾停在 "This is getting messy. Let me simplify.",
    filter_history 没有最终 return, 运行时报
    'NoneType' object has no attribute 'columns' —— 完全指不到真因。
    """
    result = gen.validate_code(_code(
        '    df = df.sort(["symbol", "date"])\n'
        '    if df.is_empty():\n'
        '        return df\n'
        '    # This is getting messy. Let me simplify.'
    ))
    assert result["valid"] is False
    assert "缺少 return" in result["error"]


def test_complete_strategy_with_return_passes(gen: AIStrategyGenerator):
    """末尾有 return 的正常策略不受影响。"""
    result = gen.validate_code(_code(
        '    df = df.sort(["symbol", "date"])\n'
        '    if df.is_empty():\n'
        '        return df\n'
        '    return df.filter(pl.col("close") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


def test_if_else_with_both_branches_returning_passes(gen: AIStrategyGenerator):
    """if/else 两个分支都 return → 视为安全, 不误杀。"""
    result = gen.validate_code(_code(
        '    if df.is_empty():\n'
        '        return df\n'
        '    else:\n'
        '        return df.filter(pl.col("close") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


# ── elif 链回归 (2026-11-13 复核发现的误杀) ─────────────────
# Python AST 里 elif 是 orelse=[If(...)] 的嵌套, 不是平铺分支。
# 直接取 orelse[-1] 拿到的是 If 节点而非 Return → 「三分支都 return」的
# 合法写法被误判成「缺少 return」。以下用例锁住该行为。

def test_if_elif_else_all_branches_returning_passes(gen: AIStrategyGenerator):
    """if/elif/else 三分支都 return → 放行 (原实现会误杀)。"""
    result = gen.validate_code(_code(
        '    if df.is_empty():\n'
        '        return df\n'
        '    elif len(df) > 100:\n'
        '        return df.head(100)\n'
        '    else:\n'
        '        return df.filter(pl.col("close") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


def test_if_elif_without_else_is_rejected(gen: AIStrategyGenerator):
    """if/elif 没有 else → 条件全不成立时落空 → 拦截。"""
    result = gen.validate_code(_code(
        '    if df.is_empty():\n'
        '        return df\n'
        '    elif len(df) > 100:\n'
        '        return df.head(100)'
    ))
    assert result["valid"] is False
    assert "缺少 return" in result["error"]


def test_nested_if_inside_branch_passes(gen: AIStrategyGenerator):
    """分支内部再嵌 if/else (各分支都有出口) → 放行。"""
    result = gen.validate_code(_code(
        '    if df.is_empty():\n'
        '        return df\n'
        '    else:\n'
        '        if len(df) > 100:\n'
        '            return df.head(100)\n'
        '        else:\n'
        '            return df.filter(pl.col("close") > 1)'
    ))
    assert result["valid"] is True, result.get("error")


def test_try_block_is_not_flagged(gen: AIStrategyGenerator):
    """try 结构判不了出口 → 放过, 不误杀 (宁可漏报)。"""
    result = gen.validate_code(_code(
        '    try:\n'
        '        return df.filter(pl.col("close") > 1)\n'
        '    except Exception:\n'
        '        return df'
    ))
    assert result["valid"] is True, result.get("error")


def test_for_loop_is_not_flagged(gen: AIStrategyGenerator):
    """循环结构判不了出口 → 放过, 不误杀。"""
    result = gen.validate_code(_code(
        '    out = df\n'
        '    for sym in df["symbol"].unique():\n'
        '        out = out.filter(pl.col("symbol") == sym)\n'
        '    return out'
    ))
    assert result["valid"] is True, result.get("error")


def test_no_return_at_all_is_rejected(gen: AIStrategyGenerator):
    """函数体里一句 return 都没有 → 更可疑, 同样拦截。"""
    result = gen.validate_code(_code(
        '    df = df.sort(["symbol", "date"])'
    ))
    assert result["valid"] is False
    assert "缺少 return" in result["error"]


# ── 合法写法不得误杀 (引擎注入列 / 关键字定义列 / 选择器) ────────────

def test_custom_signal_columns_pass(gen: AIStrategyGenerator):
    """csg_ / csgi_ 列由引擎注入, strategy-guide 允许在 filter 中引用。"""
    result = gen.validate_code(_code(
        '    return df.filter((pl.col("csg_my_signal") > 0) | pl.col("csgi_break"))'
    ))
    assert result["valid"] is True, result.get("error")


def test_with_columns_keyword_definition_is_treated_as_defined(gen: AIStrategyGenerator):
    """with_columns(foo=expr) 关键字形式定义的列, 之后 pl.col("foo") 合法。"""
    result = gen.validate_code(_code(
        '    df = df.with_columns(gap=pl.col("close") - pl.col("open"))\n'
        '    return df.filter(pl.col("gap") > 0)'
    ))
    assert result["valid"] is True, result.get("error")


def test_regex_and_wildcard_selectors_pass(gen: AIStrategyGenerator):
    result = gen.validate_code(_code(
        '    df = df.select(pl.col("*"))\n'
        '    return df.with_columns(pl.col("^ma\\d+$").fill_null(0))'
    ))
    assert result["valid"] is True, result.get("error")


def test_keyword_definition_does_not_whitelist_other_columns(gen: AIStrategyGenerator):
    """关键字定义只放行该名本身, 幻觉列照样拦。"""
    result = gen.validate_code(_code(
        '    df = df.with_columns(gap=pl.col("close") - pl.col("open"))\n'
        '    return df.filter(pl.col("vol_ma20") > 0)'
    ))
    assert result["valid"] is False
    assert "vol_ma20" in result["error"]
