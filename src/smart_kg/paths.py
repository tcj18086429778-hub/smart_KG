"""项目路径工具。

本文件定义了项目根目录、配置目录、数据目录和报告目录的常量，
并提供便捷的路径拼接与父目录自动创建函数，供所有模块统一使用。
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"
REPORT_DIR = PROJECT_ROOT / "reports"


def project_path(*parts: str) -> Path:
    """返回相对于项目根目录的路径。

    参数：
        parts: 从项目根目录开始的路径片段。

    返回：
        拼接后的完整路径对象。
    """
    return PROJECT_ROOT.joinpath(*parts)


def ensure_parent(path: Path) -> Path:
    """确保目标路径的父目录存在，不存在时自动创建。

    参数：
        path: 目标文件或目录路径。

    返回：
        传入的路径对象本身，便于链式调用。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
