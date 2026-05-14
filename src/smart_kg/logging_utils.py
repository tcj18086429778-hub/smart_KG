"""日志基础设施。

本文件为 smart_kg 项目提供统一的日志配置、参数摘要和函数调用跟踪能力，
用于支撑命令行执行、图谱读写、GPKG 成本化、栅格构建和接口服务等链路。

主要能力包括：
1. `configure_logging`：初始化项目统一日志格式与日志级别。
2. `summarize_for_log`：将复杂对象压缩成适合日志输出的摘要字符串。
3. `trace_call`：为函数或方法补充统一的入口/出口调试日志。
4. `instrument_module_functions` / `instrument_class_methods`：批量为模块函数或类方法挂载跟踪日志。
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_LOG_LEVEL = "INFO"
_CONFIGURED = False

F = TypeVar("F", bound=Callable[..., Any])


def configure_logging(level: str | None = None) -> None:
    """初始化项目日志配置。

    参数：
        level: 显式传入的日志级别，优先级高于环境变量；为空时读取
            `SMART_KG_LOG_LEVEL`，再为空时回落到 `INFO`。

    返回：
        无。

    副作用：
        修改 Python 根日志器的格式和级别；重复调用时只会更新级别，不会重复添加 handler。

    适用场景：
        命令行入口、API 启动入口、集成脚本首次进入业务逻辑之前。
    """

    global _CONFIGURED
    resolved_level = (level or os.getenv("SMART_KG_LOG_LEVEL", DEFAULT_LOG_LEVEL)).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)
    if not _CONFIGURED:
        logging.basicConfig(level=numeric_level, format=LOG_FORMAT)
        _CONFIGURED = True
        return
    logging.getLogger().setLevel(numeric_level)


def summarize_for_log(value: Any, *, max_items: int = 5, max_length: int = 160) -> str:
    """将复杂对象转换成适合写入日志的摘要字符串。

    参数：
        value: 任意待记录对象。
        max_items: 当对象是序列或映射时，最多保留的元素个数。
        max_length: 最终日志文本的最大长度，超出部分会截断。

    返回：
        适合日志输出的短字符串。

    副作用：
        无。

    适用场景：
        记录函数入参、返回值摘要、图层列表、规则列表和对象标识等信息。
    """

    if value is None:
        return "None"
    if isinstance(value, str):
        text = value
    elif isinstance(value, Mapping):
        items = list(value.items())[:max_items]
        preview = ", ".join(f"{key}={summarize_for_log(val, max_items=max_items, max_length=max_length // 2)}" for key, val in items)
        suffix = ", ..." if len(value) > max_items else ""
        text = f"{{{preview}{suffix}}}"
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value[:max_items]) if hasattr(value, "__getitem__") else list(value)[:max_items]
        preview = ", ".join(summarize_for_log(item, max_items=max_items, max_length=max_length // 2) for item in items)
        suffix = ", ..." if len(value) > max_items else ""
        left, right = ("[", "]") if not isinstance(value, tuple) else ("(", ")")
        text = f"{left}{preview}{suffix}{right}"
    elif hasattr(value, "model_dump"):
        try:
            text = summarize_for_log(value.model_dump(), max_items=max_items, max_length=max_length)
        except Exception:
            text = repr(value)
    elif hasattr(value, "__dict__") and value.__class__.__module__ != "builtins":
        payload = {
            key: inner
            for key, inner in vars(value).items()
            if not key.startswith("_")
        }
        text = f"{value.__class__.__name__}({summarize_for_log(payload, max_items=max_items, max_length=max_length)})"
    else:
        text = repr(value)
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


def trace_call(logger: logging.Logger, *, include_result: bool = False) -> Callable[[F], F]:
    """为函数或方法添加统一的入口与出口调试日志。

    参数：
        logger: 当前模块对应的日志器。
        include_result: 是否在退出日志中附带返回值摘要。

    返回：
        可直接作用于函数的装饰器。

    副作用：
        生成包装函数，并在 `DEBUG` 级别记录调用开始、结束和异常信息。

    适用场景：
        需要为大量函数补齐统一跟踪日志，但不希望逐个手写重复样板时。
    """

    def decorator(func: F) -> F:
        """实际作用于目标函数的装饰器闭包。

        若函数已标记为已跟踪则直接返回，避免重复包装。
        """
        if getattr(func, "__smart_kg_traced__", False):
            return func

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            """包装函数：在入口记录入参，在出口记录返回值或异常。"""
            debug_enabled = logger.isEnabledFor(logging.DEBUG)
            if debug_enabled:
                logger.debug(
                    "进入函数 %s.%s(args=%s, kwargs=%s)",
                    func.__module__,
                    func.__qualname__,
                    summarize_for_log(args),
                    summarize_for_log(kwargs),
                )
            try:
                result = func(*args, **kwargs)
            except Exception:
                logger.exception("函数执行失败 %s.%s", func.__module__, func.__qualname__)
                raise
            if debug_enabled:
                if include_result:
                    logger.debug(
                        "离开函数 %s.%s(result=%s)",
                        func.__module__,
                        func.__qualname__,
                        summarize_for_log(result),
                    )
                else:
                    logger.debug("离开函数 %s.%s", func.__module__, func.__qualname__)
            return result

        setattr(wrapper, "__smart_kg_traced__", True)
        return wrapper  # type: ignore[return-value]

    return decorator


def instrument_module_functions(
    namespace: dict[str, Any],
    logger: logging.Logger,
    *,
    exclude: set[str] | None = None,
    include_private: bool = True,
) -> None:
    """批量为模块级函数挂载统一调试日志。

    参数：
        namespace: 通常传入 `globals()`。
        logger: 模块日志器。
        exclude: 明确不需要自动包装的函数名集合。
        include_private: 是否包装以下划线开头的内部函数。

    返回：
        无。

    副作用：
        直接修改传入命名空间中的函数对象引用。

    适用场景：
        模块函数较多，希望统一补齐入口/出口调试日志，同时保留手写的细粒度业务日志。
    """

    excluded = exclude or set()
    for name, value in list(namespace.items()):
        if name in excluded:
            continue
        if not include_private and name.startswith("_"):
            continue
        if not inspect.isfunction(value):
            continue
        if getattr(value, "__module__", None) != namespace.get("__name__"):
            continue
        namespace[name] = trace_call(logger)(value)


def instrument_class_methods(
    cls: type[Any],
    logger: logging.Logger,
    *,
    exclude: set[str] | None = None,
) -> None:
    """批量为类方法和静态方法挂载统一调试日志。

    参数：
        cls: 待处理的类对象。
        logger: 模块日志器。
        exclude: 不需要自动包装的方法名集合。

    返回：
        无。

    副作用：
        原地替换类上的函数、静态方法和类方法实现。

    适用场景：
        像 Neo4j 写入器、规则引擎这类方法数量较多的类，需要统一补齐跟踪日志时。
    """

    excluded = exclude or set()
    for name, value in vars(cls).items():
        if name in excluded:
            continue
        if isinstance(value, staticmethod):
            wrapped = staticmethod(trace_call(logger)(value.__func__))
            setattr(cls, name, wrapped)
        elif isinstance(value, classmethod):
            wrapped = classmethod(trace_call(logger)(value.__func__))
            setattr(cls, name, wrapped)
        elif callable(value):
            setattr(cls, name, trace_call(logger)(value))
