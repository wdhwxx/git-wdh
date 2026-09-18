# 文件名：myquant/__init__.py
"""
Pythonwdhmm 鲲鹏量化交易系统 — 核心源码包
============================================

包结构（严格遵循项目结构图）：
    myquant/
    ├── core/          # 基础设施（配置/日志/异常/缓存/事件总线/防未来函数）
    ├── engine/        # 引擎抽象层（BaseEngine）
    ├── models/        # 数据模型（trade/market_data/backtest）
    ├── database/      # 数据库层（connection/schema/queries/migrations）
    ├── data_access/   # 数据获取（fetcher/manager/sources/akshare_source）
    ├── indicators/    # 技术指标（moving_average/momentum/volatility/rsrs）
    ├── strategies/    # 策略（registry/base/rsrs_strategy）
    ├── backtest/      # 回测引擎（engine/metrics/recorder/report）
    ├── execution/     # 执行层（base/sim_broker/state_machine）
    ├── allocation/    # 资金分配（base/equal_weight/risk_parity/target_volatility）
    ├── risk/          # 风控（risk_manager/guardian/rules）
    ├── gui/           # GUI（可选）
    ├── web/           # Web看板（可选）
    ├── live/          # 实盘（engine/data_feed/portfolio/scheduler/state_store/brokers）
    ├── monitoring/    # 监控告警（health_check/alerting/metrics_collector）
    ├── api/           # REST API（可选）
    ├── ml/            # 机器学习（可选）
    └── utils/         # 通用工具（time_utils/file_utils/math_utils）

核心规则（4条铁律）：
    1. 以项目结构总图为核心 — 所有文件严格按结构图组织
    2. 为实时行情及实盘无缝对接做好充足准备 — 回测/实盘共用代码
    3. 输出完整全部代码，采取最优方案，不要片段
    4. 严格禁用未来函数 — LookaheadGuard 自动校验
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"
__description__ = "沪深A股日线量化回测与实盘交易系统"

# 防未来函数全局开关（规则4）
# 通过环境变量 LOOKAHEAD_GUARD_ENABLED=true 强制开启
# 在 core/lookahead_guard.py 中实现具体校验逻辑
