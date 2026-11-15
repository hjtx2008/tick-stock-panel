"""策略代码 AST 安全校验: 白名单模块 (polars / numpy) 的文件与动态库入口必须拦截。

策略在主进程内 exec。import 白名单挡住了 os/subprocess, 但 polars / numpy 自带
read_* / write_* / np.load / SQL 表函数 / 插件加载, 可读写任意文件或加载动态库。
"""
from __future__ import annotations

import pytest

from app.strategy.ai_generator import AIStrategyGenerator

_V = AIStrategyGenerator._validate_safety


@pytest.mark.parametrize(
    "body",
    [
        'pl.read_csv("C:/Users/x/secrets.json")',
        'pl.scan_parquet("/etc/passwd")',
        'df.write_parquet("../../evil.parquet")',
        'df.lazy().sink_csv("out.csv")',
        'np.load("x.npy", allow_pickle=True)',
        'np.save("x.npy", 1)',
        'np.fromfile("x.bin")',
        'pl.SQLContext().execute("SELECT * FROM read_csv(\'a.csv\')")',
        'pl.sql("SELECT 1")',
        'np.ctypeslib.load_library("evil", ".")',
        'pl.plugins.register_plugin_function(plugin_path="evil.dll", function_name="f", args=[])',
        'pl.DataFrame.deserialize("x.bin")',
    ],
)
def test_file_and_library_entrypoints_are_rejected(body: str) -> None:
    code = f"import polars as pl\nimport numpy as np\ndef filter(df, params):\n    {body}\n    return pl.lit(True)\n"
    with pytest.raises(ValueError, match=r"读写文件|动态库"):
        _V(code)


@pytest.mark.parametrize(
    "code",
    [
        "from polars import read_csv\n",
        "from numpy import load\n",
        "import numpy.ctypeslib\n",
        "from numpy.ctypeslib import load_library\n",
    ],
)
def test_io_imports_are_rejected(code: str) -> None:
    with pytest.raises(ValueError, match=r"读写文件|动态库"):
        _V(code)


def test_ordinary_strategy_still_passes() -> None:
    code = (
        "import polars as pl\n"
        "import numpy as np\n"
        "from polars import col\n"
        "from app.backtest.matrix import load_market_data_matrix_from_parquet\n"
        "def filter(df, params):\n"
        "    x = np.where(np.isnan(np.asarray([1.0])), 0, 1)\n"
        '    return (col("close") > pl.col("ma20")) & pl.col("volume").is_not_null()\n'
    )
    _V(code)
