"""数据加载器。

本文件提供通用的 JSON/CSV 读写工具函数，供成本规则抽取器等模块使用。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from .logging_utils import instrument_module_functions

logger = logging.getLogger(__name__)


def read_json(path: Path) -> Any:
    """读取 JSON 文件并返回解析后的对象。

    参数：
        path: JSON 文件路径。

    返回：
        解析后的 Python 对象（dict、list 等）。
    """
    logger.debug("读取 JSON 文件：path=%s", path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """将对象以缩进格式写入 JSON 文件，自动创建父目录。

    参数：
        path: 输出 JSON 文件路径。
        data: 待序列化的对象。
    """
    logger.debug("写出 JSON 文件：path=%s", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_csv_dicts(path: Path) -> list[dict[str, str | None]]:
    """读取 CSV 文件并返回行字典列表。

    参数：
        path: CSV 文件路径。

    返回：
        每行数据为一个字典，键为列名，值为字符串或 None。
    """
    logger.debug("读取 CSV 文件：path=%s", path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def clean_empty(value: Any) -> Any:
    """将空字符串转为 None，非空字符串去除首尾空白。

    参数：
        value: 任意输入值。

    返回：
        清洗后的值。
    """
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    return value


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """对整行字典执行空值清洗。

    参数：
        row: 原始行字典。

    返回：
        清洗后的行字典。
    """
    return {key: clean_empty(value) for key, value in row.items()}


instrument_module_functions(globals(), logger)
