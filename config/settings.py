"""CareerCopilot 全局配置。

定义数据库文件路径、数据目录等常量。
本模块仅包含配置，不包含数据库连接或业务逻辑（见任务 3 起的各模块）。
"""

from __future__ import annotations

from pathlib import Path

# -----------------------------------------------------------------------------
# 路径定义
# -----------------------------------------------------------------------------

# 项目根目录（本文件位于 config/settings.py，故上溯两级）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# 数据目录
DATA_DIR: Path = PROJECT_ROOT / "data"
# 真实源工作簿目录（被 .gitignore 完整排除，不得提交）
PRIVATE_DATA_DIR: Path = DATA_DIR / "private"
# 用户上传的原始工作簿临时目录（被 .gitignore 排除）
UPLOADS_DIR: Path = DATA_DIR / "uploads"
# 匿名样本目录（可提交，仅含虚构数据）
SAMPLE_DATA_DIR: Path = DATA_DIR / "sample"
SAMPLE_CSV: Path = SAMPLE_DATA_DIR / "sample_opportunities.csv"

# -----------------------------------------------------------------------------
# 数据库
# -----------------------------------------------------------------------------

# SQLite 数据库文件路径（单文件存储，被 .gitignore 排除）
DB_PATH: Path = DATA_DIR / "careercopilot.db"

# -----------------------------------------------------------------------------
# 应用常量
# -----------------------------------------------------------------------------

# 应用标题
APP_TITLE: str = "CareerCopilot 求职助手"
# 应用副标题
APP_SUBHEADER: str = "面向中国大学生秋招的本地求职机会管理工具"
# 目标 Python 版本（仅作记录，不做运行时强制校验）
TARGET_PYTHON_VERSION: tuple[int, int] = (3, 11)


def ensure_dirs() -> None:
    """确保运行所需的目录存在（不创建 private/，避免误导用户放置真实数据）。"""

    for path in (DATA_DIR, UPLOADS_DIR, SAMPLE_DATA_DIR):
        path.mkdir(parents=True, exist_ok=True)
