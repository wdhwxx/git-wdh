# 文件名：tests/conftest.py
"""
conftest.py - pytest 全局配置
==============================
通过 pytest_plugins 加载 fixtures 目录下的所有 fixture 模块。
"""

import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 加载 fixtures
pytest_plugins = [
    "tests.fixtures.market_data",
    "tests.fixtures.strategies",
    "tests.fixtures.live_env",
    "tests.fixtures.database",
]
