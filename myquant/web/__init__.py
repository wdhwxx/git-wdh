# 文件名：myquant/web/__init__.py
"""
web - Web看板模块
==================
基于Streamlit的量化交易系统Web看板。

v1实现:
  - dashboard.py: Web看板（侧边栏导航 + 8个页面）
  - 数据质量探查页（任务15）：DataQualityExplorer 数据逻辑类 + 页面

TODO(后续迭代):
  - K线图交互可视化
  - 回测结果完整可视化
  - 实盘实时行情刷新
  - 策略参数动态配置
  - 实盘下单面板
  - 风控实时仪表盘
"""

from myquant.web.dashboard import (
    STREAMLIT_AVAILABLE,
    Dashboard,
    DataQualityExplorer,
    run_dashboard,
)

__all__ = [
    "Dashboard",
    "DataQualityExplorer",
    "run_dashboard",
    "STREAMLIT_AVAILABLE",
]
