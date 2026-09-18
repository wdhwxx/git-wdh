# 文件名：myquant/backtest/__init__.py
"""
鲲鹏量化交易系统 - 回测引擎层（Backtest Layer）
==================================================
事件驱动回测引擎、绩效指标计算、交易记录、报告生成。

核心组件:
  - BacktestEngine    : 回测引擎（继承 BaseEngine，严格T+1成交时序）
  - MetricsCalculator : 绩效指标计算器（夏普/最大回撤/胜率/Calmar/Sortino）
  - TradeRecorder     : 交易记录器（成交明细/持仓变动/资金曲线）
  - ReportGenerator   : 报告生成器（HTML/文本/CSV）
  - MonteCarloAnalyzer: 蒙特卡洛模拟分析器（支持 skip 开关，快速模式零算力跳过）
  - AsyncMonteCarlo   : 异步蒙特卡洛执行器（多组并发、限流、错误隔离、进度回传）
  - ParamOptimizer    : 策略参数优化器（网格搜索 + 进程池并行，接入并行上下文自动管理缓存）
  - IncrementalBacktester : 增量回测调度器（数据指纹识别变更标的，只重算变更，复用结果缓存）
  - BatchBacktester       : 批量回测主调度器（指纹 + 三模式引擎调度 + 内存自动降级）
  - ConsistencyChecker    : 三模式净值一致性校验器（event/mixed/vectorized 逐日对拍，
                            绝对+相对双阈值可配置，解决浮点数合法偏差误报）

设计原则:
  - 继承 engine.base.BaseEngine，与实盘引擎共用生命周期接口
  - 使用 SimBroker 撮合订单，实盘切换为券商执行器零修改
  - 严格T+1成交：T日收盘生成信号，T+1日开盘撮合
  - 市价单以开盘价成交（不是收盘价），禁用未来函数
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

from myquant.backtest.async_monte_carlo import (
    AsyncMonteCarlo,
    AsyncMonteCarloResult,
    MonteCarloTask,
)
from myquant.backtest.batch_backtest import (
    BatchBacktester,
    BatchBacktestResult,
    BatchPlan,
    MemoryDegradePolicy,
)
from myquant.backtest.consistency_check import (
    ComparisonTolerance,
    ConsistencyChecker,
    ConsistencyReport,
    DemoThreeModeStrategy,
    PairComparison,
)
from myquant.backtest.engine import BacktestEngine
from myquant.backtest.engine_benchmark import (
    EngineBenchmark,
    EngineBenchmarkConfig,
    EngineBenchmarkReport,
    EngineBenchmarkResult,
)
from myquant.backtest.incremental_backtest import (
    IncrementalBacktester,
    IncrementalBacktestResult,
    IncrementalPlan,
)
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.monte_carlo import MonteCarloAnalyzer, MonteCarloResult
from myquant.backtest.param_optimizer import (
    ParamGrid,
    ParamOptimizationResult,
    ParamOptimizer,
    ParamSpec,
    ParamTrial,
    make_demo_data,
)
from myquant.backtest.recorder import TradeRecorder
from myquant.backtest.report import ReportGenerator

__all__ = [
    'AsyncMonteCarlo',
    'AsyncMonteCarloResult',
    'BacktestEngine',
    'BatchBacktestResult',
    'BatchBacktester',
    'BatchPlan',
    'ComparisonTolerance',
    'ConsistencyChecker',
    'ConsistencyReport',
    'DemoThreeModeStrategy',
    'EngineBenchmark',
    'EngineBenchmarkConfig',
    'EngineBenchmarkReport',
    'EngineBenchmarkResult',
    'IncrementalBacktestResult',
    'IncrementalBacktester',
    'IncrementalPlan',
    'MemoryDegradePolicy',
    'MetricsCalculator',
    'MonteCarloAnalyzer',
    'MonteCarloResult',
    'MonteCarloTask',
    'PairComparison',
    'ParamGrid',
    'ParamOptimizationResult',
    'ParamOptimizer',
    'ParamSpec',
    'ParamTrial',
    'ReportGenerator',
    'TradeRecorder',
    'make_demo_data',
]
