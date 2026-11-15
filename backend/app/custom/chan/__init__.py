"""缠论分析扩展 (后端侧)。

- ``core.py``: 纯算法内核, 不依赖 FastAPI, 可单测。
- ``api.py``: HTTP 层 (薄层: 取数 + 序列化)。
- 本模块: 扩展注册入口 (EXTENSION_ID / setup / startup)。

设计文档: docs/chan-theory-spec.md
"""
from __future__ import annotations

from app.extensions import BACKEND_EXTENSION_API_VERSION

EXTENSION_ID = "chan.analysis"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar) -> None:
    """注册缠论扩展资源 (HTTP 路由)。"""
    from app.custom.chan.api import router

    registrar.include_router(router)


def startup(context) -> None:
    """绑定运行时上下文 (仓库只读句柄 + 数据目录)。"""
    from app.custom.chan.api import bind_context

    bind_context(context)


__all__ = ["EXTENSION_API_VERSION", "EXTENSION_ID", "setup", "startup"]
