"""AI 策略生成器 — 读取策略开发文档 + 调用 LLM 生成策略代码。

职责: 接收用户自然语言描述 → 读取 prompts/strategy-guide.md → 调用 LLM → 返回策略代码。
不知道: 引擎内部、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import ast
import logging
import math
import re
from pathlib import Path

from app.indicators.pipeline import ENRICHED_COLUMNS
from app.strategy.scoring import VIRTUAL_SCORING_DEPENDENCIES

logger = logging.getLogger(__name__)

# 策略开发精简指南路径 (随 backend/app 打包进 Docker, 避免 .dockerignore 排除 docs/ 导致运行时缺失)
GUIDE_PATH = Path(__file__).resolve().parent / "prompts" / "strategy-guide-compact.md"

_SYSTEM_PREFIX = """你是A股量化策略设计专家。根据用户描述的需求，参考下方的《策略开发指南》生成一个完整的策略Python文件。

文件与范围铁律（不可违反）:
1. 只创建这一个策略文件：只生成一个 .py 文件，绝不创建多文件、不拆分模块、不跨文件引用
2. 绝不触碰项目源码：不要写任何会修改 backend/、docs/、frontend/ 等现有文件的代码；不要 import os/sys/pathlib 等文件系统模块
3. 不得放入内置策略目录：AI 生成的策略只属于 data/strategies/ai/，文件名/ID 用 ai_ 前缀；内置目录 backend/app/strategy/builtin/ 由项目维护，AI 不得染指
4. polars 策略只 import polars 和 datetime；matrix_native 策略只允许 import numpy 以及 from app.backtest.matrix import 所需矩阵协议和算子

要求:
1. 用户可能调整的策略阈值通过 META["params"] 暴露，每项使用 id/label/type/default/min/max/step；公式常数、固定窗口边界、布尔开关不必强行参数化
2. 遵循指南中的文件结构，但优先贴合用户规则，不要为了套模板歪曲策略含义
3. ENTRY_SIGNALS/EXIT_SIGNALS 根据策略逻辑自行选择匹配的信号列，不要照搬示例
4. scoring 权重根据策略核心逻辑定制，总和 = 1.0；键只能使用指南中的真实数值字段或受控虚拟评分字段 ma20_bias，不得创造条件名称作为评分列
5. 优先使用 Polars 表达式、窗口函数、聚合和 with_columns/filter 实现，避免逐行/逐股 Python 循环；只有表达式难以描述的复杂状态机才使用 partition_by/to_dicts
6. 直接输出Python代码，不要输出其他内容
7. 元数据必须使用模块顶层的 META = {...} 或 META: dict = {...}，不得省略或改名；并且必须定义所选执行后端要求的策略入口

--- 策略开发指南 ---

"""

_META_NAMES = ("META", "STRATEGY_META", "meta")
_FENCED_CODE_RE = re.compile(
    r"```(?P<language>[^\n`]*)\r?\n(?P<code>.*?)```",
    re.DOTALL,
)
_POLARS_ENTRYPOINT_ERROR = "找不到策略入口函数 filter() 或 filter_history()"
_MATRIX_ENTRYPOINT_ERROR = "找不到 Matrix 策略入口 MATRIX_STRATEGY"

_POLARS_SCORING_FIELDS = frozenset(
    name
    for name in ENRICHED_COLUMNS
    if name not in {"symbol", "date", "name"} and not name.startswith("signal_")
) | frozenset(VIRTUAL_SCORING_DEPENDENCIES)
_MATRIX_SCORING_FIELDS = frozenset({
    "open", "high", "low", "close", "volume", "amount", "turnover_rate",
    "total_shares", "float_shares", "consecutive_limit_ups",
    "consecutive_limit_downs", "prev_close", "change_pct", "change_amount",
    "amplitude", "ma5", "ma10", "ma20", "ma30", "ma60", "boll_upper",
    "boll_lower", "high_60d", "low_60d", "momentum_5d", "momentum_10d",
    "momentum_20d", "momentum_30d", "momentum_60d", "annual_vol_20d",
    "rsi_6", "rsi_14", "rsi_24", "vol_ratio_5d", "ma20_bias",
})


def _top_level_assignment(
    tree: ast.Module,
    name: str,
) -> tuple[ast.Name, ast.expr | None] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target = next(
                (item for item in node.targets
                 if isinstance(item, ast.Name) and item.id == name),
                None,
            )
            if target is not None:
                return target, node.value
        elif isinstance(node, ast.AnnAssign) \
                and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return node.target, node.value
    return None


def find_meta_assignment(code: str) -> tuple[ast.Name, ast.Dict] | None:
    """Find a supported module-level META assignment without executing code."""
    tree = ast.parse(code)
    for name in _META_NAMES:
        found = _top_level_assignment(tree, name)
        if found is not None:
            target, value = found
            if not isinstance(value, ast.Dict):
                raise ValueError(f"{name} 必须是字面量字典")
            return target, value
    return None


def _strategy_execution_backend(tree: ast.Module, meta: dict | None = None) -> str:
    found = _top_level_assignment(tree, "EXECUTION_BACKEND")
    if found is not None:
        try:
            value = ast.literal_eval(found[1])
        except (ValueError, SyntaxError):
            value = None
        if isinstance(value, str):
            return value
    if isinstance(meta, dict) and isinstance(meta.get("execution_backend"), str):
        return meta["execution_backend"]
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "filter_history"
        for node in tree.body
    ):
        return "python_history_legacy"
    return "polars_expr"


def _strategy_entrypoint_error(code: str, meta: dict | None = None) -> str | None:
    tree = ast.parse(code)
    if _strategy_execution_backend(tree, meta) == "matrix_native":
        return None if _top_level_assignment(tree, "MATRIX_STRATEGY") else _MATRIX_ENTRYPOINT_ERROR
    has_polars_entrypoint = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"filter", "filter_history"}
        for node in tree.body
    )
    return None if has_polars_entrypoint else _POLARS_ENTRYPOINT_ERROR


def _flatten_if_chain(node: ast.If) -> tuple[list[list[ast.stmt]], bool]:
    """把 if / elif / else 链摊平成「分支列表 + 是否有 else」。

    为什么需要 (2026-11-13 复核): Python AST 里 elif 不是平铺分支, 而是
    `orelse=[If(...)]` 的嵌套 —— 直接取 `orelse[-1]` 拿到的是 If 节点而不是
    Return, 于是「三个分支都 return」的合法写法被误判成「缺少 return」。

    返回:
        branches: 各分支的语句列表 (if 体, elif 体..., else 体)
        has_else:  链上是否存在 else 分支 (没有 else = 存在落空路径)
    """
    branches: list[list[ast.stmt]] = [node.body]
    current = node
    while True:
        orelse = current.orelse
        if not orelse:
            return branches, False
        if len(orelse) > 1:
            # orelse 是多条语句 → 普通 else 块 (非纯 elif 链)
            branches.append(orelse)
            return branches, True
        first = orelse[0]
        if isinstance(first, ast.If):
            # elif → 继续往下摊平
            branches.append(first.body)
            current = first
            continue
        branches.append(orelse)
        return branches, True


def _definitely_exits(stmts: list[ast.stmt]) -> bool | None:
    """这组语句是否保证「函数一定会 return 或 raise」。

    返回三态:
        True  —— 所有可达路径都有出口 (安全)
        False —— 确定存在不返回的路径 (可疑)
        None  —— 结构复杂判不了 (放过, 宁可漏报不误杀)

    递归覆盖 if/elif/else 嵌套与 with 体; try / 循环 / match 判不了时返回 None。
    """
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return True
    if isinstance(last, ast.If):
        branches, has_else = _flatten_if_chain(last)
        if not has_else:
            # 没有 else → 条件不成立时直接落空
            return False
        verdicts = [_definitely_exits(b) for b in branches]
        if any(v is False for v in verdicts):
            return False
        if any(v is None for v in verdicts):
            return None
        return True
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _definitely_exits(last.body)
    if isinstance(last, (ast.Try, ast.For, ast.AsyncFor, ast.While, ast.Match)):
        # 出口语义取决于循环是否执行 / 异常是否被吞 / match 是否全覆盖 → 判不了
        return None
    # 赋值、表达式调用等 → 落空
    return False


def _missing_return_entrypoint(code: str, meta: dict | None = None) -> str | None:
    """检测策略入口函数是否存在「所有路径都 return」的保证。

    为什么需要 (2026-11-13 实测): AI 生成的文件被截断 (末尾停在
    "This is getting messy. Let me simplify."), filter_history 没有最终 return,
    运行时报 'NoneType' object has no attribute 'columns' —— 错误信息完全
    指向不了「文件没写完」这个真因。

    判定: 用 AST 找入口函数, 分析函数体是否**所有路径**都有 return/raise 出口。
    判不了的结构 (try / 循环 / match) 一律放过 —— 宁可漏报, 也不误杀合法策略。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # 语法错误由别的校验负责

    entry_names = {"filter", "filter_history"}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in entry_names:
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr) or
                not isinstance(n.value, ast.Constant)]
        if not body:
            continue
        if _definitely_exits(body) is False:
            return (
                f"策略入口函数 {node.name}() 缺少 return 语句 "
                "(文件可能在生成时被截断); 请补全函数并确保每条路径都有返回值"
            )
    return None


class AIStrategyGenerator:
    """AI 策略生成器"""

    def __init__(self) -> None:
        self._guide_cache: str | None = None

    def _get_guide(self) -> str:
        if self._guide_cache is None:
            if GUIDE_PATH.exists():
                self._guide_cache = GUIDE_PATH.read_text(encoding="utf-8")
            else:
                logger.warning("strategy guide not found at %s", GUIDE_PATH)
                self._guide_cache = ""
        return self._guide_cache

    async def generate(self, user_prompt: str) -> dict:
        """根据用户描述生成策略代码

        Returns: {"code": str, "meta": dict, "valid": bool, "error": str | None}
        """
        guide = self._get_guide()

        # 调用 LLM
        code = await self._call_llm(user_prompt, guide)
        result = self.validate_code(code)
        if self.needs_structural_repair(result):
            return await self.repair_code(result["code"], result["error"])
        return result

    async def stream(self, user_prompt: str):
        """Yield generated strategy code deltas from the configured AI provider."""
        from app.services.ai_provider import stream_ai_text

        guide = self._get_guide()
        async for chunk in stream_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PREFIX + guide},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            # None = 不传上限: 策略代码较长, 推理模型思考 token 计入预算会截断代码
            max_tokens=None,
        ):
            yield chunk

    def validate_code(self, code: str) -> dict:
        code = self._extract_code_block(code)

        # 验证
        try:
            self._validate_safety(code)
        except SyntaxError as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": f"Python 语法错误: {e.msg}",
            }
        except ValueError as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": str(e),
            }

        # 试加载获取 META
        try:
            meta = self._extract_meta(code)
        except Exception as e:
            return {
                "code": code,
                "meta": {},
                "valid": False,
                "error": f"解析META失败: {e}",
            }

        entrypoint_error = _strategy_entrypoint_error(code, meta)
        if entrypoint_error:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": entrypoint_error,
            }

        missing_return = _missing_return_entrypoint(code, meta)
        if missing_return:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": missing_return,
            }

        try:
            self._validate_meta_semantics(code, meta)
        except ValueError as e:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": str(e),
            }

        try:
            self._validate_column_references(code)
        except ValueError as e:
            return {
                "code": code,
                "meta": meta,
                "valid": False,
                "error": str(e),
            }

        return {
            "code": code,
            "meta": meta,
            "valid": True,
            "error": None,
        }

    @staticmethod
    def needs_structural_repair(result: dict) -> bool:
        error = result.get("error") or ""
        return error.startswith("解析META失败:") or error in {
            _POLARS_ENTRYPOINT_ERROR,
            _MATRIX_ENTRYPOINT_ERROR,
        } or error.startswith(("META.params", "META.scoring"))

    @staticmethod
    def _validate_meta_semantics(code: str, meta: dict) -> None:
        params = meta.get("params", [])
        if isinstance(params, (list, tuple)):
            for index, item in enumerate(params):
                if isinstance(item, dict) and not str(item.get("id") or "").strip():
                    raise ValueError(f"META.params[{index}] 缺少非空 id")

        scoring = meta.get("scoring", {})
        if not isinstance(scoring, dict):
            raise ValueError("META.scoring 必须是字典")
        if not scoring:
            return

        for name, weight in scoring.items():
            if not isinstance(name, str) or not name:
                raise ValueError("META.scoring 字段名必须是非空字符串")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
                    or not math.isfinite(float(weight)) or weight < 0:
                raise ValueError(f"META.scoring[{name!r}] 权重必须是非负有限数值")
        total_weight = sum(float(weight) for weight in scoring.values())
        if not math.isclose(total_weight, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("META.scoring 权重总和必须为 1.0")

        backend = _strategy_execution_backend(ast.parse(code), meta)
        if backend == "python_history_legacy":
            return
        allowed = _MATRIX_SCORING_FIELDS if backend == "matrix_native" else _POLARS_SCORING_FIELDS
        unknown = sorted(set(scoring) - set(allowed))
        if unknown:
            raise ValueError(
                f"META.scoring 引用了不可用字段: {unknown}; "
                "请使用真实数值字段或受控虚拟字段 ma20_bias"
            )

    @staticmethod
    def _validate_column_references(code: str) -> None:
        """静态校验策略里 pl.col("xxx") 引用的列是否真实存在。

        为什么需要 (2026-11-13 实测): AI 生成的策略引用了指标流水线里
        不存在的列 (如 vol_ma20 —— 流水线只有 vol_ma5 / vol_ma10),
        保存时无人拦截, 跑到批量选股才炸, 且一个坏策略会让整个 run_all 抛 500,
        其余 28 个正常策略的结果全部拿不到。

        允许的列:
        - ENRICHED_COLUMNS 的真实列 + 受控虚拟因子 (VIRTUAL_SCORING_DEPENDENCIES)
        - 策略自己用 with_columns/alias 定义的内部列 (以下划线开头, 如 _vol_ma20)
        - 策略自己 alias 出来的列名 (alias="xxx" 的目标名)

        识别的列引用写法:
        - pl.col("xxx") / 任意别名的模块 .col("xxx")  (属性调用)
        - pl.col("a", "b") 多参数写法 (每个参数都校验)
        - pl.col("a, b") 单参数逗号多列写法
        - from polars import col 后的裸 col("xxx") 调用

        只做「引用了但从未定义」的检测 —— 宁可放过 (运行时报错更准),
        也不误杀合法策略。
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return  # 语法错误由 _validate_safety 负责报

        available = set(ENRICHED_COLUMNS) | set(VIRTUAL_SCORING_DEPENDENCIES)
        # 策略内部定义的列: alias 目标名 + 下划线开头的中间量名
        defined: set[str] = set()
        referenced: dict[str, int] = {}

        # 裸导入的 col: from polars import col (或 as c) → col("xxx") 也是列引用
        bare_col_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] != "polars":
                    continue
                for alias in node.names:
                    if alias.name == "col":
                        bare_col_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = getattr(func, "attr", None) or getattr(func, "id", None)
            # alias("xxx") → 策略自己创建的列
            if fname == "alias" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    defined.add(first.value)
            # with_columns(foo=expr) / select(foo=expr) 关键字形式 → 同样是策略自建列
            if fname in ("with_columns", "select", "agg"):
                defined.update(kw.arg for kw in node.keywords if kw.arg)
            # col(...) → 引用外部列
            # 属性调用 (pl.col / 别名.col) 与裸导入 (from polars import col)
            is_col_call = getattr(func, "attr", None) == "col" or (
                isinstance(func, ast.Name) and func.id in bare_col_names
            )
            if not is_col_call:
                continue
            # 多参数写法 pl.col("a", "b") → 每个参数都要校验;
            # 单参数 "a, b" → 逗号切分
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for part in arg.value.split(","):
                        part = part.strip()
                        if part:
                            referenced.setdefault(part, node.lineno)

        unknown = {
            name: line
            for name, line in referenced.items()
            if name not in available
            and name not in defined
            and not name.startswith("_")
            # 自定义信号列由引擎按前缀注入 (strategy-guide 允许在 filter 中引用), 不在 ENRICHED_COLUMNS
            and not name.startswith(("csg_", "csgi_"))
            # 正则 / 通配选择器 (pl.col("^ma\d+$") / pl.col("*")) 不是具体列名
            and not (name == "*" or (name.startswith("^") and name.endswith("$")))
        }
        if unknown:
            details = ", ".join(
                f"{name} (行 {line})" for name, line in sorted(unknown.items())
            )
            raise ValueError(
                f"策略引用了不存在的列: {details}; "
                f"可用列见 ENRICHED_COLUMNS (共 {len(ENRICHED_COLUMNS)} 个); "
                "若需要额外列请在策略内用 with_columns(alias=...) 计算"
            )

    async def repair_code(self, code: str, error: str) -> dict:
        """Ask the model once for a complete replacement after a structural error."""
        try:
            backend = _strategy_execution_backend(ast.parse(code))
        except SyntaxError:
            backend = "polars_expr"
        if backend == "matrix_native":
            entrypoint_requirement = (
                '保留 EXECUTION_BACKEND = "matrix_native"，定义 MATRIX_STRATEGY，'
                "不得添加 filter() 或 filter_history()"
            )
        else:
            entrypoint_requirement = (
                "保留原执行后端，并定义对应的 filter() 或 filter_history()"
            )
        prompt = f"""上一次生成的策略代码未通过结构校验。

校验错误：{error}

请输出修复后的完整策略 Python 文件。必须保留原策略意图和参数，使用模块顶层
META = {{...}}，{entrypoint_requirement}。只输出完整 Python 代码。

上一次代码：
```python
{code}
```"""
        repaired = await self._call_llm(prompt, self._get_guide())
        return self.validate_code(repaired)

    async def _call_llm(self, user_prompt: str, guide: str) -> str:
        """Call the configured AI provider and return generated strategy code."""
        from app.services.ai_provider import generate_ai_text

        content = await generate_ai_text(
            [
                {"role": "system", "content": _SYSTEM_PREFIX + guide},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            # None = 不传上限: 策略代码较长, 推理模型思考 token 计入预算会截断代码
            max_tokens=None,
        )
        return self._extract_code_block(content)

    @staticmethod
    def _extract_code_block(content: str) -> str:
        blocks = list(_FENCED_CODE_RE.finditer(content))
        for match in blocks:
            candidate = match.group("code").strip()
            try:
                found = find_meta_assignment(candidate)
                if found is not None:
                    meta = ast.literal_eval(found[1])
                    if isinstance(meta, dict) and _strategy_entrypoint_error(candidate, meta) is None:
                        return candidate
            except (SyntaxError, ValueError):
                continue
        for match in blocks:
            if match.group("language").strip().lower() in {"python", "py"}:
                return match.group("code").strip()
        if blocks:
            return blocks[0].group("code").strip()
        return content.strip()

    # import 白名单: Polars 与矩阵策略只开放执行协议所需模块。
    # 白名单而非黑名单 — 黑名单挡不住 ctypes/importlib/builtins/pickle 等未列出的危险模块。
    _ALLOWED_IMPORT_MODULES = frozenset({
        "polars",
        "numpy",
        "app.backtest.matrix",
        "app.strategy.builtin.factor_rank_research",
        "app.strategy.market_data",   # 新增: 策略可读取指数/ETF 日K
        "datetime",
        "__future__",
    })

    @classmethod
    def _validate_safety(
        cls,
        code: str,
        *,
        extra_allowed_import_modules: frozenset[str] = frozenset(),
        extra_allowed_calls: frozenset[str] = frozenset(),
    ) -> None:
        """AST 级安全检查: import 白名单 + 危险内建调用拦截 + dunder 遍历拦截。

        注意: AST 名单不是真正的沙箱, 只能拦截常见攻击模式。真正的隔离需要
        在受限子进程里执行策略 (后续 P0)。此处拦截已知的逃逸技巧:
        - __globals__ / __builtins__ / __class__ / __subclasses__ / __mro__ 等属性访问
        - ["__import__"] / ["__builtins__"] 等字符串下标访问
        """
        tree = ast.parse(code)

        allowed_import_modules = cls._ALLOWED_IMPORT_MODULES | extra_allowed_import_modules
        forbidden_calls = {
            "open", "exec", "eval", "compile", "__import__",
            "globals", "locals", "vars", "dir", "getattr",
            "setattr", "delattr", "type", "input", "breakpoint",
        } - extra_allowed_calls

        def _module_allowed(module: str) -> bool:
            return (
                module in allowed_import_modules
                or module.split(".", 1)[0] in allowed_import_modules
            )

        # dunder 属性名: 访问这些属性可逃逸出策略沙箱拿到 os/subprocess 等
        forbidden_dunder_attrs = {
            "__globals__", "__builtins__", "__class__", "__subclasses__",
            "__mro__", "__bases__", "__base__", "__dict__", "__code__",
            "__import__", "__loader__", "__spec__", "__wrapped__",
        }
        # 字符串下标访问的危险名: x["__builtins__"] / x["__import__"]
        forbidden_subscript_strs = {
            "__builtins__", "__import__", "__globals__",
        }
        # 白名单模块自带的文件 / 动态库入口: 策略只消费引擎传入的数据, 不应读写磁盘。
        # pl.read_csv / df.write_parquet / np.load / pl.SQLContext (SQL 表函数可读文件) /
        # np.ctypeslib.load_library 与 polars 插件 (加载任意动态库)。
        forbidden_io_prefixes = ("read_", "scan_", "write_", "sink_")
        forbidden_io_names = {
            "load", "save", "savez", "savez_compressed", "fromfile", "tofile",
            "loadtxt", "savetxt", "genfromtxt", "memmap", "open_memmap", "DataSource",
            "ctypeslib", "load_library", "register_plugin_function", "plugins",
            "serialize", "deserialize", "sql", "SQLContext",
        }

        def _io_forbidden(name: str) -> bool:
            return name.startswith(forbidden_io_prefixes) or name in forbidden_io_names

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _module_allowed(alias.name):
                        raise ValueError(f"禁止 import {alias.name} (不在策略安全白名单)")
                    if any(_io_forbidden(part) for part in alias.name.split(".")[1:]):
                        raise ValueError(f"禁止 import {alias.name} (策略不允许读写文件或加载动态库)")
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not _module_allowed(mod):
                    raise ValueError(f"禁止 from {node.module} import (不在策略安全白名单)")
                if mod.split(".")[0] in ("polars", "numpy"):
                    for alias in node.names:
                        if _io_forbidden(alias.name) or mod.split(".")[-1] in forbidden_io_names:
                            raise ValueError(f"禁止导入 {mod}.{alias.name} (策略不允许读写文件)")
            if isinstance(node, ast.Attribute) and _io_forbidden(node.attr):
                raise ValueError(f"禁止访问 .{node.attr} (策略不允许读写文件或加载动态库)")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                    raise ValueError(f"禁止调用 {node.func.id}()")
            # 拦截 dunder 属性访问: x.__globals__ / ().__class__ 等
            if isinstance(node, ast.Attribute) and node.attr in forbidden_dunder_attrs:
                raise ValueError(f"禁止访问属性 {node.attr} (策略不允许 dunder 遍历逃逸)")
            # 拦截字符串下标访问危险名: x["__builtins__"]
            if isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str) \
                        and sl.value in forbidden_subscript_strs:
                    raise ValueError(f"禁止下标访问 {sl.value} (策略不允许 dunder 遍历逃逸)")

    @staticmethod
    def _extract_meta(code: str) -> dict:
        """从代码字符串中提取 META 字典（不执行代码, 仅接受字面量）

        兼容两种声明: META = {...} (Assign) 和 META: dict = {...} (AnnAssign)。
        与 api.strategy._find_meta_dict 保持同一套匹配逻辑。
        """
        found = find_meta_assignment(code)
        if found is None:
            raise ValueError("找不到 META 字典")
        _, value = found
        try:
            meta = ast.literal_eval(value)
        except (ValueError, SyntaxError) as e:
            raise ValueError(f"META 必须是纯字面量字典: {e}") from e
        if not isinstance(meta, dict):
            raise ValueError("META 必须是纯字面量字典")
        return meta
