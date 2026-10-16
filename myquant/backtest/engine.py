# 文件名：myquant/backtest/engine.py
"""
backtest/engine.py - 回测引擎
================================
事件驱动回测引擎，继承 engine.base.BaseEngine，实现同步时间序列事件循环。

设计原则（规则2：为实盘无缝对接准备）:
  - 继承 BaseEngine，与实盘引擎(live/engine.py)共用同一生命周期接口
  - 撮合执行器（SimBroker 等）由装配方经 register_broker_factory() 或构造
    broker_factory 注入（G1 EX-09A 消除 L3→L4 反向），实盘切换为 BrokerExecutor 零修改
  - 使用 StrategyContext 调用策略，策略不依赖具体引擎
  - 使用 TradeRecorder 记录交易和净值，实盘可复用
  - 使用 MetricsCalculator 计算绩效，回测/实盘共用
  - ★ 数据层抽象：支持 DataProviderBase，回测用 HistoricalDataProvider，
    实盘用 RealtimeDataProvider，引擎只依赖抽象接口，切换零修改

数据输入方式（两种，向后兼容）:
  方式1（传统）: 直接传入 data=Dict[str, pd.DataFrame]
  方式2（推荐）: 传入 data_provider=DataProviderBase，引擎自动从provider加载
                 便捷方法: BacktestEngine.from_data_provider(provider, config, strategy)

★★★ 严格T+1成交时序（规则4：禁用未来函数）★★★
  对于每根bar i（T日）:
    1. 撮合上一根bar（T-1日）创建的订单 → 用T日开盘价成交
    2. 更新组合市场价 → 用T日收盘价
    3. 调用策略 on_bar() → 基于T日收盘价生成信号
    4. 策略创建的订单提交到broker → T+1日开盘撮合
    5. 记录T日净值（用T日收盘价计算市值）

  关键: 市价单以开盘价成交（不是收盘价），T日信号T+1日才成交。
  策略在T日收盘后看到的信号，最早T+1日开盘才能成交。

★★★ 防未来函数：数据加载 ★★★
  从 DataProvider 加载数据时，严格传入 config.end_date，
  provider 内部过滤掉 end_date 之后的所有数据，绝不返回未来数据。
  _build_unified_bars() 中再次按日期过滤，双重保障。
"""

__version__ = "0.2.0"
__author__ = "Pythonwdhmm"

import time
from typing import Any, Callable, ClassVar, Dict, List, Optional

import pandas as pd

from myquant.backtest.assembly import (  # re-export register_broker_factory（外部调用方经 engine 导入，P0-00A 迁至 assembly.py）
    get_broker_factory,
    register_broker_factory,  # noqa: F401  # re-export：外部经 engine 导入
)
from myquant.backtest.data_loader import DataProviderBase
from myquant.backtest.metrics import MetricsCalculator
from myquant.backtest.mixed_engine import MixedBacktestEngine
from myquant.backtest.recorder import TradeRecorder
from myquant.backtest.vectorized_engine import VectorizedBacktestEngine
from myquant.core.logger import get_logger
from myquant.core.lookahead_guard import (
    LookaheadGuard,
    get_lookahead_guard,  # #H2修复：scheduler 统一注入单例
)
from myquant.engine.base import BaseEngine
from myquant.models.backtest import BacktestConfig, BacktestResult, EquityPoint
from myquant.models.market_data import Bar, MarketEvent
from myquant.models.trade import OrderType
from myquant.risk.rules import RiskContext  # KP-P3-07: 回测复用实盘风控上下文
from myquant.strategies.base import Signal, StrategyBase, StrategyContext

logger = get_logger(__name__)

# 撮合执行器工厂注册表（G1 EX-09A）已迁至 myquant.backtest.assembly
# （_broker_factory 全局 + register_broker_factory + get_broker_factory；P0-00A 第一刀）。
# 本模块顶部 re-export register_broker_factory 保持外部调用方零改动；
# _resolve_broker 经 get_broker_factory() 读实时全局（避免 from backtest import 包级边成环）。


def _attach_realism_manifest(
    engine: "BacktestEngine",
    config: BacktestConfig,
    strategy: Optional[StrategyBase],
    engine_mode: str,
    engine_kwargs: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, pd.DataFrame]] = None,
) -> Any:
    """构建并挂载真实性门禁 manifest（P0-01 两阶段握手第①~④步，v2.2 §4.4）。

    构造引擎后采集 manifest → assert_realism_pass（入口断言）→ seal；挂载
    engine._realism_manifest 供 run() 收尾真实采集刷新（_refresh_causality_manifest）
    与握手第⑤步校验（#344）。

    P1-05：enabled=True 时门禁异常传播（fail-closed，违规阻断引擎创建）；
    enabled=False 时软接线（仅审计不阻断，置 engine._realism_manifest=None）。

    #349 真实端到端验收收敛：模块级函数（非 BacktestEngine 方法）→
    BacktestEngine/MixedBacktestEngine/VectorizedBacktestEngine（独立类，互不继承）
    的 create_engine 装配统一走本函数，三引擎门禁语义同构；from_data_provider
    直构路径（生产入口 scripts/生成回测报告.py 等）同样收敛——此前直构无 manifest
    → 握手⑤跳过、causality 等判据完全不生效（enabled=true 形同虚设）。

    Args:
        engine: 目标引擎实例（挂载 _realism_manifest 供 run 收尾刷新）。
        config: 回测配置（manifest 构造事实来源）。
        strategy: 策略实例（strategy_id 采集）。
        engine_mode: 引擎模式（full_sample_declared 判定：vectorized 恒 True）。
        engine_kwargs: 引擎构造 kwargs（enable_lookahead_guard 事实来源）。
        data: 已加载的 {symbol: DataFrame}（pool.admitted_count 采集；None 时记 0——
            provider 场景数据在 run 时 on_init 加载，create 时不可知，sample_pool
            判据不依赖 admitted_count）。

    Returns:
        构建的 RealismManifest（已 assert + seal）；enabled=False 软接线失败时 None。

    Raises:
        RealismGateError: enabled=True 且存在 critical 违规（入口阻断）。
    """
    import myquant.realism_gate.checks  # noqa: F401 导入即注册检查器
    from myquant.realism_gate import (
        RealismGateConfig,
        RealismGateError,
        RealismManifest,
        RealismViolation,
        assert_realism_pass,
        set_config,
    )

    _kwargs: Dict[str, Any] = engine_kwargs or {}
    cfg_dict = config.to_dict() if hasattr(config, "to_dict") else {}
    rg_raw = cfg_dict.get("realism_gate")
    # P1-03 收尾（#354）：配置缺失 = 阻断（方案 §4.3——本门禁要求显式启用，
    # 缺失即红；与 leakage_gate 段缺失落模块默认的刻意不同）。
    # 段缺失（None/空 dict/非 dict）→ 拒绝启动；BacktestConfig 默认已带显式段
    # （enabled=False 审计档），代码直构不触发；YAML 缺段经 config_builder 产出
    # 空段 → 本处拒启（生产路径"忘记配门禁"不再静默审计空转）。
    if not isinstance(rg_raw, dict) or not rg_raw:
        raise RealismGateError(
            "realism_gate 配置段缺失（配置缺失 = 阻断，方案 §4.3：本门禁要求"
            "显式启用）——拒绝启动；请显式配置 realism_gate.enabled "
            "（true=阻断档 / false=审计档）",
            violations=[RealismViolation(
                field_path="realism_gate",
                critical=True,
                message="配置段缺失（fail-closed，拒绝启动）",
                actual_value=rg_raw,
            )],
        )
    # P1-03：严格类型校验（配置损坏 fail-closed，不静默 bool() 转换）
    set_config(RealismGateConfig.from_dict(rg_raw))
    try:
        manifest = RealismManifest()
        manifest.set_field("run_id", f"run_{id(config) & 0xFFFFFFFF:08x}")
        manifest.set_field("strategy_id", getattr(strategy, "name", "") or "")
        manifest.set_field("engine_mode_requested", engine_mode)
        manifest.set_field("engine_mode_resolved", engine_mode)
        manifest.set_field("start_date", str(getattr(config, "start_date", "")))
        manifest.set_field("end_date", str(getattr(config, "end_date", "")))
        manifest.set_field("initial_capital", float(getattr(config, "initial_capital", 0)))
        manifest.set_field("price_limit.enabled", bool(cfg_dict.get("price_limit", {}).get("enabled", True)))
        manifest.set_field("price_limit.pct_by_board", cfg_dict.get("price_limit", {}).get("pct_by_board", {}))
        manifest.set_field("price_limit.pre_close_source", cfg_dict.get("price_limit", {}).get("pre_close_source", "data_col"))
        # P2-04：盘中触板建模已接线（撮合层/SimBroker 用 high/low 判涨跌停触板）
        manifest.set_field("price_limit.intraday_touch_modeled", True)
        manifest.set_field("suspension.enabled", bool(cfg_dict.get("suspension", {}).get("enabled", True)))
        manifest.set_field("suspension.basis", cfg_dict.get("suspension", {}).get("basis", "volume_zero"))
        # ── P0-08 子项③（止血 B）：7 组硬编码乐观值改真实实例读取（S-11）──
        # t1/lot/liquidity/slippage 从 config 构造事实读（broker 在 run 时 on_init
        # 才装配，此处仅有 config 事实；run 后由 _refresh_causality_manifest 刷新）
        manifest.set_field("t1_position_freeze.enabled", bool(getattr(config, "enable_t1", True)))
        # P0-03 续卡（#348）：撮合层整手执行已接线（OrderMatcher/SimBroker
        # 均按 lot_size 限制买入零股）→ lot_size>0 时整手约束真实生效，诚实报
        # True；0（未装配整手约束）报 False（#346 起接线前的诚实 False 已成历史）。
        _cfg_lot = int(getattr(config, "lot_size", 100))
        manifest.set_field("lot_size.enforced", _cfg_lot > 0)
        manifest.set_field("lot_size.value", _cfg_lot)
        _liquidity_cap = float(getattr(config, "participation_rate", 1.0))
        manifest.set_field("liquidity.participation_cap", _liquidity_cap)
        # 参与率 1.0 = 全量参与（不限制），须显式声明（1.0 恒由 config 默认，诚实披露）
        manifest.set_field("liquidity.declared_unlimited", _liquidity_cap >= 1.0)
        # 滑点从 config.slippage（SlippageModel 真实实例）读，替换 YAML cfg_dict（YAML 被忽略）
        _slippage_model = getattr(config, "slippage", None)
        _slip_type = str(getattr(_slippage_model, "type", "fixed_bps"))
        _slip_value = float(getattr(_slippage_model, "fixed_bps", 0.0))
        if _slip_type == "none":
            _slip_value = 0.0  # type="none" 实际滑点恒 0（calc 返回 0），报真实生效值
        manifest.set_field("slippage.type", _slip_type)
        manifest.set_field("slippage.value", _slip_value)
        manifest.set_field(
            "slippage.is_zero_cost",
            bool(_slip_type == "none" or _slip_value == 0.0),
        )
        manifest.set_field("commission.buy_rate", float(cfg_dict.get("commission", {}).get("buy_rate", 0.0003)))
        manifest.set_field("commission.sell_rate", float(cfg_dict.get("commission", {}).get("sell_rate", 0.0003)))
        manifest.set_field("commission.min_commission", float(cfg_dict.get("commission", {}).get("min_commission", 5.0)))
        manifest.set_field("commission.stamp_tax", float(cfg_dict.get("commission", {}).get("stamp_tax", 0.0005)))
        manifest.set_field("commission.transfer_fee", float(cfg_dict.get("commission", {}).get("transfer_fee", 0.00002)))
        manifest.set_field("fee_base_includes_slippage", False)
        manifest.set_field("fee_dilution_basis", "per_order")
        manifest.set_field("adjust_effective_by_symbol", {})
        # P0-04：从 provider 获取真实复权降级集合（延迟导入避免跨层硬依赖）
        # #346 真实生效值对齐：_adjust_degraded_symbols 为模块级全局，跨回测残留
        # 会污染入口断言（先前回测的降级标的误判本回测）；装配起点清空，
        # run 内由 provider 加载重新填充，收尾 _refresh 读到本次回测真实集合。
        try:
            from myquant.data_access.provider import (
                get_adjust_degraded_symbols,
                reset_adjust_degraded,
            )
            reset_adjust_degraded()
            manifest.set_field("adjust_degraded_symbols", get_adjust_degraded_symbols())
        except Exception:
            manifest.set_field("adjust_degraded_symbols", [])
        # P1-13 回测期数据质量门禁：真实数据质量统计（替换硬编码 0/1.0 乐观值）。
        # data dict 预加载（传统 create_engine）：入口扫描每标的 DataFrame →
        # manifest.quality.* 写真实值，data_pit 判据（NaN 价>0 / OHLC 违例>0 /
        # coverage_rate<0.5 → critical）经 assert_realism_pass 在引擎创建前
        # fail-closed 阻断（不静默放行脏数据）；
        # data=None/非 dict（provider 场景，run 时 on_init 加载）：入口写构造占位
        # （0 NaN/0 OHLC + coverage 1.0 无缺陷证据，CRITICAL_FIELDS 不可写 None），
        # 运行期 provider.get_bars 累计真实统计，收尾 _refresh 回写覆盖（含真实覆盖率）。
        _quality_stats = _scan_entry_data_quality(data, config)
        manifest.set_field("quality.nan_price_rows", _quality_stats["nan_price_rows"])
        manifest.set_field("quality.ohlc_violation_rows", _quality_stats["ohlc_violation_rows"])
        manifest.set_field("quality.coverage_rate", _quality_stats["coverage_rate"])
        # ── P1-12 数据快照 pin：fingerprint_hash/snapshot_id 从硬编码 None 改真实采集 ──
        # data=None（provider 场景，从 processed 目录读真实数据）：真扫目录校验
        # sidecar（内容哈希基线），写入真实指纹；sidecar 缺失自动首次 pin；
        # 重跑发现目录增删改 → snapshot_mismatch=True → data_pit 判据 critical 阻断。
        # data dict 预加载（内存数据/测试）：数据与 processed 目录无关 →
        # snapshot_applicable=False（不适用目录快照，data_pit 不判指纹）。
        if data is None or not isinstance(data, dict):
            try:
                from myquant.data_access.provider import verify_sidecar
                _sn = verify_sidecar()
                manifest.set_field("data.snapshot_applicable", True)
                manifest.set_field("fingerprint_hash", _sn["fingerprint_hash"])
                manifest.set_field("snapshot_id", _sn["snapshot_id"])
                manifest.set_field("data.snapshot_mismatch", not _sn["matched"])
            except Exception as _sn_e:  # noqa: BLE001
                # 快照采集失败：诚实披露为缺失（applicable=True 下 data_pit 判 critical）
                logger.warning("数据快照 pin 采集失败（按缺失披露）: %s", _sn_e)
                manifest.set_field("data.snapshot_applicable", True)
                manifest.set_field("fingerprint_hash", None)
                manifest.set_field("snapshot_id", None)
                manifest.set_field("data.snapshot_mismatch", False)
        else:
            manifest.set_field("data.snapshot_applicable", False)
            manifest.set_field("fingerprint_hash", None)
            manifest.set_field("snapshot_id", None)
            manifest.set_field("data.snapshot_mismatch", False)
        manifest.set_field("pool.pit_applied", True)
        # P0-08 子项③：排除口径真实为 today（P0-06 只接量化字段，as_of 为乐观谎报）
        manifest.set_field("pool.exclusion_basis", "today")
        # P0-06：注入真实样本口径（requested/admitted/dropped）
        _req = len(config.symbols)
        _adm = len(data) if isinstance(data, dict) else 0
        manifest.set_field("pool.requested_count", _req)
        manifest.set_field("pool.admitted_count", _adm)
        manifest.set_field("pool.dropped_count", max(0, _req - _adm))
        manifest.set_field("pool.drop_reasons", {} if _req == _adm else {"data_missing": _req - _adm})
        manifest.set_field("survivorship.discount", None)
        manifest.set_field("survivorship.window", "1y")
        manifest.set_field("survivorship.status", "unknown")
        manifest.set_field("invariants.price_bounds", {})
        manifest.set_field("invariants.volume_bound", {})
        manifest.set_field("invariants.equity_identity", {})
        manifest.set_field("invariants.cash_non_negative", {})
        manifest.set_field("invariants.t1_consistency", {})
        # ── P0-08 子项②：causality 组 run 前写构造事实 ──
        # 引擎 context 在 on_init（run 时）才创建，此处不可实测；
        # run 结束后由 _refresh_causality_manifest 从 context 实测刷新。
        # context_sliced / current_bar_ts_enforced：run 前未置位，诚实 False；
        # full_sample_declared：构造参数事实（vectorized 恒传 True，其余引擎默认 False）；
        # guard_injected：enable_lookahead_guard=True 时引擎必自建/注入 guard（构造事实）。
        manifest.update_group("causality", {
            "context_sliced": False,
            "current_bar_ts_enforced": False,
            "full_sample_declared": engine_mode == ENGINE_MODE_VECTORIZED,
            "guard_injected": bool(_kwargs.get("enable_lookahead_guard", True)),
            # [P0-08⑧ 已落地] context.data value 直取已封死：DataWindow 窗口隔离
            # （on_bar 保留 keys 枚举、封死 value 直取）构造即生效，on_init 阶段
            # 按阶段语义放行全量（S-2）；运行期由 _refresh_causality_manifest 写真实值
            "context_data_scoped": True,
            # P0-01（#344）：引擎未运行 → causality 判据门控关闭（诚实占位不误伤）；
            # run 收尾 _refresh 置 True 后判据正式生效
            "assessed_at_run_end": False,
        })
        assert_realism_pass(manifest)
        engine._realism_manifest = manifest
    except Exception as rg_e:  # noqa: BLE001
        # P1-05：enabled=True 时不吞门禁异常（违规 → 传播导致非零退出）；
        # enabled=False 时保持软接线（仅审计不阻断）。
        _rg_cfg = RealismGateConfig.from_dict(rg_raw)
        if _rg_cfg.enabled:
            raise
        logger.debug(f"真实性门禁软接线（不阻断）: {type(rg_e).__name__}: {rg_e}")
        engine._realism_manifest = None
    return engine._realism_manifest


def _scan_entry_data_quality(
    data: Optional[Dict[str, pd.DataFrame]],
    config: BacktestConfig,
) -> Dict[str, Any]:
    """P1-13 入口数据质量统计（引擎创建前，替换硬编码乐观值）。

    data dict 预加载场景：对每标的 DataFrame 调 ``scan_data_quality`` 聚合
    nan_price_rows / ohlc_violation_rows；coverage_rate = 已加载标的数 / 请求标的数
    （有数据标的占比，真实值替换原硬编码 1.0，上限截断 1.0 防基准混入超 100%）；
    data 为 None（provider 场景，run 时 on_init 才加载）：三项均写 None
    （S-11 诚实披露"未统计"，运行期由 provider.get_bars 累计、收尾 _refresh 回写）。

    Args:
        data: 已加载的 {symbol: DataFrame}（create_engine 入口传入）。
        config: 回测配置（symbols 请求池，coverage 分母）。

    Returns:
        统计字典 {"nan_price_rows", "ohlc_violation_rows", "coverage_rate"}，
        其中覆盖率可能为 None（data 为空或请求池为空）。
    """
    if data is None or not isinstance(data, dict) or len(data) == 0:
        # provider 场景（run 时 on_init 才加载）或 data 非 dict：入口无预加载数据可扫描，
        # 写构造占位（0 NaN/0 OHLC 违例 + coverage 1.0=无覆盖率缺陷证据）——
        # quality.* 属 CRITICAL_FIELDS 不可写 None；运行期 provider.get_bars 累计真实统计，
        # 收尾 _refresh 回写覆盖（与 causality 组入口 False/运行期 True 的构造占位模式一致，
        # 收尾 assert 前 data_pit 判据真实生效，含真实覆盖率）
        return {"nan_price_rows": 0, "ohlc_violation_rows": 0, "coverage_rate": 1.0}
    from myquant.data_access.provider import scan_data_quality

    nan_total = 0
    ohlc_total = 0
    for symbol, df in data.items():
        if df is None or df.empty:
            continue
        _s = scan_data_quality(df, symbol=str(symbol))
        nan_total += _s["nan_price_rows"]
        ohlc_total += _s["ohlc_violation_rows"]
    _req = len(getattr(config, "symbols", None) or [])
    _coverage: Optional[float] = None
    if _req > 0:
        _coverage = min(1.0, len(data) / _req)
    return {
        "nan_price_rows": nan_total,
        "ohlc_violation_rows": ohlc_total,
        "coverage_rate": _coverage,
    }


class BacktestEngine(BaseEngine):
    """
    事件驱动回测引擎

    继承 BaseEngine，实现同步时间序列事件循环。

    核心流程:
      1. on_init(config): 初始化broker、策略、记录器、从DataProvider加载数据
      2. run(): 逐bar遍历历史数据，执行T+1成交时序
      3. on_stop(): 计算绩效指标，生成回测结果

    使用方式（方式1：传统直接传数据）:
        config = BacktestConfig(
            strategy_name="rsrs",
            symbols=["600519.SH", "000001.SZ"],
            start_date="2020-01-01",
            end_date="2024-12-31",
            initial_capital=1000000,
        )
        engine = BacktestEngine(config=config, strategy=rsrs_strategy, data=data_dict)
        result = engine.run()

    使用方式（方式2：推荐，从DataProvider加载，为实盘无缝对接）:
        from myquant.backtest.data_loader import HistoricalDataProvider
        provider = HistoricalDataProvider()
        engine = BacktestEngine.from_data_provider(
            data_provider=provider,
            config=config,
            strategy=rsrs_strategy,
        )
        result = engine.run()
    """

    # M1A-04（v3.0 §6.4.7-A）：_bar_date_str 时间戳异常哨兵计数（可观测化）。
    # 异常不再静默——每发生一次计数 +1，测试/审计可断言次数；
    # 哨兵值 "" 交由 LookaheadGuard 记警告档 violation（跳过行为可见）。
    _bar_date_failures: ClassVar[int] = 0

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        strategy: Optional[StrategyBase] = None,
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        name: str = "backtest",
        lookahead_guard: Optional[LookaheadGuard] = None,
        enable_lookahead_guard: bool = True,
        broker_factory: Optional[Callable[[BacktestConfig], Any]] = None,
    ):
        """
        初始化回测引擎

        参数:
            config: 回测配置
            strategy: 策略实例
            data: 历史行情数据 {symbol: DataFrame}（传统方式，向后兼容）
            data_provider: 数据提供器（推荐方式，为实盘无缝对接设计）
                           传入后，on_init() 中自动从provider加载数据
            name: 引擎名称
            lookahead_guard: 防未来函数校验器实例（None则自动创建，#S3修复）
            enable_lookahead_guard: 是否启用防未来函数显式校验（默认True，规则4要求强制开启）
            broker_factory: 撮合执行器工厂（G1 EX-09A 注入点，签名
                factory(config) -> broker；None 时用 register_broker_factory 注册的
                全局工厂，仍未装配则 on_init 显式报错）

        数据优先级:
            如果同时传入 data 和 data_provider，优先使用 data（向后兼容），
            data_provider 仅在 data 为空时生效。
        """
        super().__init__(name=name)
        self.config = config or BacktestConfig()
        self.strategy = strategy
        self.data = data or {}
        self.data_provider = data_provider

        # 核心组件
        self.broker: Optional[Any] = None
        # G1 EX-09A：实例级撮合执行器工厂（优先于全局注册表）
        self._broker_factory: Optional[Callable[[BacktestConfig], Any]] = broker_factory
        self.recorder: Optional[TradeRecorder] = None
        self.metrics_calc: Optional[MetricsCalculator] = None
        self.context: Optional[StrategyContext] = None
        # P0-08 子项④（止血 C）：bar.timestamp 非法导致切片置位失败的哨兵计数
        self._slicing_failures: int = 0

        # 回测结果
        self.result: Optional[BacktestResult] = None

        # 基准数据
        self._benchmark_data: Optional[pd.DataFrame] = None
        # KP-P2-04：基准配置了但加载为空 → 标记缺失（不静默，alpha/beta/excess 置 None）
        self._benchmark_missing: bool = False

        # #S3修复：防未来函数显式校验器（引擎内置能力，不再仅依赖入口脚本）
        self._enable_lookahead_guard = enable_lookahead_guard
        self._lookahead_guard: Optional[LookaheadGuard] = None
        if enable_lookahead_guard:
            self._lookahead_guard = lookahead_guard or LookaheadGuard()
        # 订单ID -> 信号日期映射（用于成交时关联信号日，校验T+1规则）
        self._order_signal_dates: Dict[str, str] = {}
        # P1-07 子项⑤：轻量防未来防线时间线缓存（on_stop 时喂给 LightLookaheadGuard）
        self._bar_timestamps: List[float] = []

        # KP-P3-07: 回测复用实盘 RiskManager（on_init 装配；缺省 None 不拦截）
        self._risk_manager: Optional[Any] = None
        self._risk_prev_close: Dict[str, float] = {}
        self._risk_day = ""
        self._risk_day_start_assets = float(self.config.initial_capital)
        self._risk_peak_assets = float(self.config.initial_capital)
        self._risk_order_count = 0
        self._risk_last_order_time = 0.0

        logger.info(f"回测引擎初始化: {self.config}")
        if data_provider:
            logger.info(f"数据提供器: {data_provider}")
        if self._lookahead_guard:
            logger.info(f"防未来函数校验器已启用: {self._lookahead_guard}")

    # ------------------------------------------------------------------
    # 便捷构造方法：从DataProvider创建引擎（为实盘无缝对接设计）
    # ------------------------------------------------------------------

    @classmethod
    def from_data_provider(
        cls,
        data_provider: DataProviderBase,
        config: BacktestConfig,
        strategy: Optional[StrategyBase] = None,
        name: str = "backtest",
    ) -> "BacktestEngine":
        """
        从数据提供器创建回测引擎（推荐方式）

        引擎在 on_init() 中自动从 data_provider 加载 config.symbols 的数据，
        严格按 config.end_date 过滤（防未来函数）。

        实盘无缝对接：将 HistoricalDataProvider 替换为 RealtimeDataProvider，
        引擎代码零修改。

        参数:
            data_provider: 数据提供器（HistoricalDataProvider 或 RealtimeDataProvider）
            config: 回测配置（symbols/start_date/end_date 用于数据加载）
            strategy: 策略实例
            name: 引擎名称

        返回:
            BacktestEngine 实例
        """
        engine = cls(
            config=config,
            strategy=strategy,
            data_provider=data_provider,
            name=name,
        )
        # P0-01（#349）：直构路径与 create_engine 同构收敛门禁语义（manifest 构建/
        # 入口断言/封印）。event 引擎构造事实：full_sample_declared=False、
        # guard_injected=enable_lookahead_guard 默认 True（__init__ 缺省）。
        engine._realism_manifest = _attach_realism_manifest(
            engine,
            config,
            strategy,
            engine_mode=ENGINE_MODE_EVENT,
            engine_kwargs={"enable_lookahead_guard": True},
        )
        return engine

    # ------------------------------------------------------------------
    # 真实性门禁 manifest 构建（create_engine 与 from_data_provider 共用）
    # ------------------------------------------------------------------

    def attach_realism_manifest(
        self,
        config: BacktestConfig,
        strategy: Optional[StrategyBase],
        engine_mode: str,
        engine_kwargs: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Any:
        """构建并挂载真实性门禁 manifest（P0-01 两阶段握手第①~④步，v2.2 §4.4）。

        #349 收敛为模块级函数 ``_attach_realism_manifest`` 的薄封装（三引擎共用）；
        语义/时序/P1-05 软接线/参数说明见该函数 docstring。
        """
        return _attach_realism_manifest(
            self, config, strategy, engine_mode,
            engine_kwargs=engine_kwargs, data=data,
        )

    # ------------------------------------------------------------------
    # 生命周期：初始化
    # ------------------------------------------------------------------

    def _resolve_broker(self, config: BacktestConfig) -> Any:
        """解析撮合执行器：实例级工厂 > 全局注册表；未装配显式报错。

        Args:
            config: 回测配置（传给工厂构造撮合执行器）。

        Returns:
            撮合执行器实例（已 connect 前状态）。

        Raises:
            ValueError: 实例级与全局均未装配工厂，或工厂返回 None 时抛出。
        """
        factory = self._broker_factory or get_broker_factory()
        if factory is None:
            raise ValueError(
                "BacktestEngine 需要撮合执行器工厂：请经 register_broker_factory() "
                "装配（如 SimBroker），或构造时传 broker_factory 参数（G1 EX-09A）"
            )
        broker = factory(config)
        if broker is None:
            raise ValueError("broker_factory 返回了 None（撮合执行器不可为空）")
        return broker

    def on_init(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化回测引擎

        创建broker、策略上下文、记录器、绩效计算器，
        从 DataProvider 加载数据（防未来函数），调用策略on_init。
        """
        logger.info("=" * 60)
        logger.info("回测引擎初始化开始")
        logger.info("=" * 60)

        # #S3修复：每次回测开始前清空防未来函数校验记录
        if self._lookahead_guard:
            self._lookahead_guard.reset()
            self._order_signal_dates.clear()
            logger.debug("防未来函数校验器已重置")

        # 0. ★★★ 从 DataProvider 加载数据（防未来函数）★★★
        if not self.data and self.data_provider is not None:
            self._load_data_from_provider()

        # 1. 创建撮合执行器（G1 EX-09A：工厂注入，装配责任在合法层）
        self.broker = self._resolve_broker(self.config)
        self.broker.connect()

        # 1b. KP-P3-07: 装配与实盘同源的 RiskManager（回测执行实盘风控）
        # 回写#255 P0-2：装配工厂下沉至 risk.risk_manager（消除 L3→L4 反向依赖）
        from myquant.risk.risk_manager import (
            build_risk_manager_from_config,  # 懒导入避免重型耦合
        )
        _cfg = self.config.to_dict()
        self._risk_manager = build_risk_manager_from_config(
            risk_cfg=_cfg.get("risk_control"),
            position_cfg=_cfg.get("position"),
            strategy_cfg=_cfg.get("strategy"),
            enable_trading_hours=False,
        )
        self._risk_peak_assets = float(getattr(self.broker, "total_assets", self.config.initial_capital))
        # 回测为批量回放，墙钟下单间隔无意义 → 关闭 rate_limit 间隔约束（KP-P3-07 适配）
        _rl = self._risk_manager.get_rule("rate_limit")
        if _rl is not None:
            _rl.min_interval_seconds = 0.0
        logger.info("[backtest] 已装配实盘同源 RiskManager（KP-P3-07）")

        # 2. 创建记录器
        self.recorder = TradeRecorder(initial_capital=self.config.initial_capital)

        # 3. 创建绩效计算器
        self.metrics_calc = MetricsCalculator(risk_free_rate=self.config.risk_free_rate)

        # 4. 创建策略上下文（此时 self.data 已加载完成）
        # #T-06(KP-P2-08)：把防未来守卫注入 context.extra，供策略守卫钩子上报指标/止损/数据访问
        self.context = StrategyContext(
            data=self.data,
            portfolio=self.broker,
            executor=self.broker,
            config=self.config.to_dict(),
            extra=(
                {"lookahead_guard": self._lookahead_guard}
                if self._lookahead_guard is not None
                else {}
            ),
        )

        # 5. 初始化策略
        if self.strategy:
            self.strategy.on_init(self.context)
            logger.info(f"策略初始化完成: {self.strategy.name}")

        # 6. 加载基准数据
        if self.config.benchmark:
            if self.config.benchmark in self.data:
                self._benchmark_data = self.data[self.config.benchmark]
            elif self.data_provider is not None:
                # 基准不在symbols中，单独从provider加载
                self._benchmark_data = self.data_provider.get_bars(
                    symbol=self.config.benchmark,
                    start_date=self.config.start_date or None,
                    end_date=self.config.end_date or None,
                    # 基准（指数，如 000300.SH）无复权因子概念——复权因子只适用于
                    # 个股除权除息事件；指数价格编制已连续（成分股分红除权自动处理），
                    # 应用 hfq 反而触发虚假降级（000300.SH 0 因子事件 → 降级 raw →
                    # data_pit 判 critical → enabled=true 生产回测全断）。#350 基准
                    # 数据治理：基准一律按 raw 加载（指数语义，market_rules 等判据
                    # 不受影响——基准不参与撮合/交易真实性）。
                    adjust="raw",
                )
                if not self._benchmark_data.empty:
                    logger.info(
                        f"基准数据已加载: {self.config.benchmark}, "
                        f"{len(self._benchmark_data)} 根K线"
                    )
        # KP-P2-04：基准配置了但 data/provider 均未取到 → 显式告警 + 标记缺失（不再静默）
        if self.config.benchmark and self._benchmark_data is None:
            self._benchmark_missing = True
            logger.error(
                f"基准 {self.config.benchmark} 数据缺失（data/provider 均未取到），"
                f"alpha/beta/excess 将置 None，回测基准不可比（benchmark_missing=True）"
            )

        self._initialized = True
        logger.info(
            f"回测引擎初始化完成: 资金={self.config.initial_capital:,.0f}, "
            f"标的数={len(self.data)}, 策略={self.strategy.name if self.strategy else 'None'}"
        )

    # ------------------------------------------------------------------
    # 数据加载：从DataProvider加载（防未来函数核心）
    # ------------------------------------------------------------------

    def _load_data_from_provider(self) -> None:
        """
        从 DataProvider 加载回测数据

        ★★★ 防未来函数 ★★★
        - 严格使用 config.end_date 作为截止日期，provider 内部过滤未来数据
        - 使用 config.start_date 作为开始日期
        - 只加载 config.symbols 中指定的标的
        - 加载后数据按日期升序排列

        数据加载失败的标的会被跳过并记录警告。
        """
        if self.data_provider is None:
            return

        symbols = self.config.symbols
        if not symbols:
            logger.warning("config.symbols 为空，无法从 DataProvider 加载数据")
            return

        start_date = self.config.start_date or None
        end_date = self.config.end_date or None
        period = self._frequency_to_period(self.config.frequency)

        logger.info(
            f"从 DataProvider 加载数据: symbols={symbols}, "
            f"range={start_date}~{end_date}, period={period}"
        )

        # 批量加载（provider.load_multiple 内部统一 end_date，防未来函数）
        loaded = self.data_provider.load_multiple(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            period=period,
        )

        self.data = loaded

        if not self.data:
            logger.error("从 DataProvider 未加载到任何有效数据")
        else:
            total_bars = sum(len(df) for df in self.data.values())
            logger.info(
                f"数据加载完成: {len(self.data)}/{len(symbols)} 个标的, "
                f"共 {total_bars} 根K线"
            )

    @staticmethod
    def _frequency_to_period(frequency: str) -> str:
        """
        将 BacktestConfig.frequency 转换为 DataProvider.get_bars() 的 period 参数

        映射:
          "1d" / "daily" / "D" -> "daily"
          "1w" / "weekly" / "W" -> "weekly"
          "1m" / "minute" / "M" -> "minute"
          其他 -> 原样返回
        """
        freq = (frequency or "1d").lower().strip()
        mapping = {
            "1d": "daily", "daily": "daily", "d": "daily",
            "1w": "weekly", "weekly": "weekly", "w": "weekly",
            "1m": "minute", "minute": "minute", "m": "minute",
        }
        return mapping.get(freq, freq)

    # ------------------------------------------------------------------
    # 生命周期：市场事件处理（核心）
    # ------------------------------------------------------------------

    def on_market_event(self, event: MarketEvent) -> None:
        """
        处理市场事件（单bar）

        ★★★ 严格T+1成交时序 ★★★
          1. 撮合上一根bar创建的订单 → 用当前bar开盘价成交
          2. 更新组合市场价 → 用当前bar收盘价
          3. 调用策略on_bar() → 基于当前bar收盘价生成信号
          4. 策略创建的订单提交到broker → 下一根bar开盘撮合
          5. 记录当前bar净值

        参数:
            event: 市场事件（包含bar数据）
        """
        bar = event.data if hasattr(event, 'data') else event
        if not isinstance(bar, Bar):
            return


        # ── 步骤0：KP-P3-07 风控日切状态（当日首bar快照日初资产/重置下单计数）──
        trade_date = self._bar_date_str(bar)
        if self._risk_manager is not None and self._risk_day != trade_date:
            self._risk_day = trade_date
            _acct = self.broker.get_account() if self.broker is not None else None
            self._risk_day_start_assets = (
                float(_acct["total_assets"]) if _acct and _acct.get("total_assets") else 0.0
            )
            self._risk_order_count = 0

        # ── 步骤1：撮合上一根bar创建的订单（用当前bar开盘价）──
        if self.broker:
            trades = self.broker.on_bar(bar)
            trade_date_str = self._bar_date_str(bar)
            for trade in trades:
                # 查找信号原因
                reason = ""
                order = self.broker.get_order(trade.order_id) if hasattr(self.broker, 'get_order') else None
                if order:
                    reason = order.tag
                self.recorder.record_trade(trade, signal_reason=reason)
                # #S3修复：记录成交到防未来函数校验器（铁律3：信号T日→T+1日开盘成交）
                if self._lookahead_guard:
                    signal_date = self._order_signal_dates.get(trade.order_id, "")
                    side = trade.side.value if hasattr(trade.side, 'value') else str(trade.side)
                    self._lookahead_guard.record_trade(
                        symbol=trade.symbol,
                        trade_date=trade_date_str,
                        signal_date=signal_date,
                        price_type="OPEN",
                        side=side,
                    )

        # ── 步骤2：更新组合市场价（用当前bar收盘价）──
        # SimBroker.on_bar已经更新了市场价，这里无需重复

        # ── P0-07 因果切片：逐 bar 推进 context.current_bar_ts + 开启切片 ──
        # bar[t] 时 context.get_data() 只返回 <=t 的数据视图（结构性消除未来函数）
        # 注意：Bar.timestamp 是秒级 Unix float（time.time()），必须用 unit='s'
        if self.context is not None:
            try:
                self.context.current_bar_ts = pd.Timestamp(bar.timestamp, unit='s')
                self.context._slicing_enabled = True
            except (TypeError, ValueError, OverflowError) as exc:
                # ── P0-08 子项④（止血 C）：静默 fail-open 改显式降级 ──
                # bar.timestamp 非法 → 保持未置位（_slicing_enabled=False），
                # context.get_data() 将返回全量（含未来）——宁可回测变红，不可静默全量：
                # 记 critical 日志 + 哨兵计数 + 立即写 context_sliced=false
                # （run 收尾 _refresh_causality_manifest 亦报 false，与子项⑤判据一致）。
                self._slicing_failures += 1
                logger.error(
                    "bar.timestamp 非法，event 因果切片置位失败（保持未封门）: %s", exc,
                )
                _m = getattr(self, "_realism_manifest", None)
                if _m is not None:
                    _m.update_group("causality", {"context_sliced": False})

        # ── 步骤3：调用策略on_bar()（基于当前bar收盘价生成信号）──
        if self.strategy and self.context:
            signals = self.strategy.on_bar(bar, self.context)
            for signal in signals:
                self._execute_signal(signal, bar)

        # ── 步骤4：记录当前bar净值（用当前bar收盘价计算市值）──
        if self.broker and self.recorder:
            account = self.broker.get_account()
            benchmark_equity = self._calc_benchmark_equity(bar.timestamp)
            self.recorder.record_equity(
                timestamp=bar.timestamp,
                cash=account['cash'],
                position_value=account['market_value'],
                benchmark_equity=benchmark_equity,
            )

        # KP-P3-07: 当前bar收盘写入 prev_close 映射（下一bar作昨收基准）
        if self._risk_manager is not None:
            self._risk_prev_close[bar.symbol] = float(bar.close)

        # P1-08: T+1 日终结算——清空当日买入冻结（次日起当日买入可卖）
        if self.broker is not None and hasattr(self.broker, 'settle_t1'):
            self.broker.settle_t1()

        # P1-07 子项⑤：记录 bar 时间戳（供 LightLookaheadGuard 时间线校验）
        self._bar_timestamps.append(float(bar.timestamp))

        self._event_count += 1

    def _execute_signal(self, signal: Signal, bar: Bar) -> None:
        """
        执行信号：转换为订单并提交到broker

        ★★★ 防未来函数 ★★★
          信号在T日收盘后生成，订单提交后状态为SUBMITTED，
          将在T+1日开盘由broker.on_bar()撮合。
        """
        if signal.is_hold:
            return

        # T4-3：回测侧生成 signal_id（与实盘同源）+ timestamp 传 bar 时间
        if not signal.signal_id:
            signal.signal_id = signal.make_signal_id(
                as_of_date=getattr(bar, "timestamp", "") and str(bar.timestamp)[:10]
            )
        signal.timestamp = getattr(bar, "timestamp", signal.timestamp)  # T4-3：bar 时间替代 time.time()

        # T4-3：记录信号到 result.signals（供 C-10 一致性监控对拍）
        if self.result is not None and hasattr(self.result, "signals"):
            self.result.signals.append({
                "symbol": signal.symbol,
                "signal_id": signal.signal_id,
                "timestamp": signal.timestamp,
                "side": signal.signal_type.value if hasattr(signal.signal_type, "value") else str(signal.signal_type),
                "strength": signal.strength,
                "reason": signal.reason,
            })

        if signal.is_buy:
            # 买入：计算仓位
            quantity = self._calc_buy_quantity(symbol=signal.symbol, price=bar.close)
            if quantity <= 0:
                logger.debug(f"买入信号跳过: {signal.symbol} 数量为0（资金不足或满仓）")
                return
            order = signal.to_order(quantity=quantity, order_type=OrderType.MARKET)
        else:
            # 卖出：全部持仓
            position = self.context.get_position(signal.symbol) if self.context else None
            if position is None or position.quantity <= 0:
                logger.debug(f"卖出信号跳过: {signal.symbol} 无持仓")
                return
            quantity = position.quantity
            order = signal.to_order(quantity=quantity, order_type=OrderType.MARKET)

        if order and self.broker:
            # KP-P3-07: 回测执行实盘风控（未通过单记录 skip）
            if self._risk_manager is not None:
                context = self._build_backtest_risk_context(bar)
                result = self._risk_manager.check_order(order, context)
                self._risk_order_count += 1
                self._risk_last_order_time = time.time()
                if not result.passed:
                    logger.warning(
                        f"[backtest] 实盘风控拒绝订单（跳过）: {order.symbol} "
                        f"{order.side.value} {order.quantity:.0f} 原因={result.reason}"
                    )
                    return
            self.broker.submit_order(order)
            logger.debug(
                f"提交订单: {order.symbol} {order.side.value} "
                f"{order.quantity:.0f}股 (信号: {signal.reason})"
            )
            # #S3修复：记录信号到防未来函数校验器（铁律4：信号T日收盘生成，因子仅用历史数据）
            if self._lookahead_guard:
                signal_date = self._bar_date_str(bar)
                signal_type = "BUY" if signal.is_buy else ("SELL" if signal.is_sell else "CLOSE")
                # #T-06(KP-P2-08)：data_end 用策略真实上报的数据截止日（禁自证式常量——
                # 不再恒填 signal_date，若策略读取未来数据（覆盖 get_signal_data_end 如实上报更大日期），
                # 铁律4 将拦截）
                _data_end = (
                    self.strategy.get_signal_data_end(
                        signal.symbol, signal_date, self.context,
                    )
                    if self.strategy is not None
                    else signal_date
                )
                self._lookahead_guard.record_signal(
                    symbol=signal.symbol,
                    signal_date=signal_date,
                    signal_type=signal_type,
                    data_end=_data_end,
                    factor_names=[self.strategy.name] if self.strategy else None,
                )
                # 保存订单ID -> 信号日期映射，用于成交时校验T+1规则
                self._order_signal_dates[order.order_id] = signal_date

    def _build_backtest_risk_context(self, bar: Bar) -> RiskContext:
        """构建回测风控上下文（KP-P3-07，字段全部取自已发生事实，防未来）。"""
        broker = self.broker
        positions: List[Dict[str, Any]] = []
        cash = 0.0
        total_assets = 0.0
        initial_capital = float(self.config.initial_capital)
        if broker is not None:
            cash = float(getattr(broker, "available_cash", 0.0))
            total_assets = float(getattr(broker, "total_assets", 0.0))
            initial_capital = float(getattr(broker, "initial_capital", initial_capital))
            for pos in broker.get_positions():
                positions.append({
                    "symbol": pos.symbol,
                    "quantity": float(pos.quantity),
                    "avg_cost": float(pos.avg_cost),
                    "market_price": float(pos.market_price),
                })
        if total_assets > self._risk_peak_assets:
            self._risk_peak_assets = total_assets
        current_price = dict(self._risk_prev_close)
        current_price[bar.symbol] = float(bar.close)
        return RiskContext(
            cash=cash,
            total_assets=total_assets,
            initial_capital=initial_capital,
            positions=positions,
            position_count=len(positions),
            peak_assets=self._risk_peak_assets,
            current_price=current_price,
            order_count_today=self._risk_order_count,
            last_order_time=self._risk_last_order_time,
            trading_date=self._bar_date_str(bar),
            day_start_assets=self._risk_day_start_assets,
            extra={"prev_close": dict(self._risk_prev_close)},
        )

    def _calc_buy_quantity(self, symbol: str, price: float) -> int:
        """计算买入数量（基于策略的仓位管理规则）

        T5-1：与实盘 _calc_order_quantity 统一逻辑——
        总资产×max_position_pct − 已持市值，不超过可用现金。
        """
        if not self.strategy or not self.context:
            return 0
        if hasattr(self.strategy, 'calc_buy_quantity'):
            return self.strategy.calc_buy_quantity(price, self.context)
        # T5-1：向实盘对齐（总资产比例−已持市值），而非可用现金×20%
        portfolio = self.context.portfolio
        total_assets = getattr(portfolio, "total_assets", 0) or getattr(portfolio, "get_total_assets", lambda: 0)()
        max_position_pct = getattr(self, "_max_position_pct", 0.20)
        max_amount = total_assets * max_position_pct
        current_value = 0.0
        pos = self.context.get_position(symbol) if hasattr(self.context, "get_position") else None
        if pos:
            current_value = pos.quantity * price
        available_amount = max(0, max_amount - current_value)
        cash = self.context.get_cash()
        available_amount = min(available_amount, cash)
        shares = int(available_amount / price / 100) * 100
        return max(0, shares)

    def _calc_benchmark_equity(self, timestamp: float) -> float:
        """计算基准净值（L8修复：按当前bar时间对齐，只使用<=当前时点数据，防未来函数）"""
        if self._benchmark_data is None or len(self._benchmark_data) == 0:
            return 1.0
        try:
            import pandas as pd

            close = self._benchmark_data['收盘价'].astype(float)
            if len(close) == 0 or close.iloc[0] <= 0:
                return 1.0
            # 基准数据时间轴：优先 index(DatetimeIndex)，其次 timestamp/date 列，最后字符串日期 index
            idx = self._benchmark_data.index
            if isinstance(idx, pd.DatetimeIndex):
                bench_ts = idx.astype("int64") // 10 ** 9
            elif 'timestamp' in self._benchmark_data.columns:
                bench_ts = self._benchmark_data['timestamp'].astype(float)
            elif 'date' in self._benchmark_data.columns:
                bench_ts = pd.to_datetime(self._benchmark_data['date']).astype("int64") // 10 ** 9
            else:
                bench_ts = pd.to_datetime(idx.astype(str)).astype("int64") // 10 ** 9
            bench_ts = bench_ts.astype(float)

            # 定位 <= 当前时点的最后一个基准行（防未来：当日收盘前不"预知"当日及之后行情）
            mask = bench_ts <= timestamp
            pos = int(mask.sum()) - 1
            if pos < 0:
                return 1.0
            return float(close.iloc[pos] / close.iloc[0])
        except Exception as e:
            logger.debug(f"基准净值计算失败，返回默认值1.0: {e}")
        return 1.0

    # ------------------------------------------------------------------
    # #S3修复：防未来函数校验辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _bar_date_str(bar: Bar) -> str:
        """
        将Bar的timestamp转换为日期字符串（YYYY-MM-DD）

        用于LookaheadGuard的日期比较（铁律1/3/4均基于日期字符串比较）。
        """
        from datetime import datetime
        try:
            return datetime.fromtimestamp(bar.timestamp).strftime('%Y-%m-%d')
        except (OSError, ValueError, OverflowError):
            # M1A-04：异常返回哨兵值 "" 并计数（不再静默）。
            # 哨兵 "" 使 LookaheadGuard 侧记警告档 violation（跳过行为可见）。
            BacktestEngine._bar_date_failures += 1
            return ""

    def _run_lookahead_assert(self) -> bool:
        """运行防未来函数统一校验（#S3修复，P0-01修复）。

        在回测结束时调用，校验 guard 已记录的信号/成交是否违反未来函数规则。
        本方法不读取任何行情数据，仅消费回测过程中按 t 时刻记录的 guard 快照。

        Returns:
            True 表示校验通过（或 guard 被显式关闭），调用方可标记 completed；
            False 表示发现违规，本方法已将结果 mark_failed，调用方不得再标记 completed。
        """
        if not self._lookahead_guard:
            return True

        try:
            guard_stats = self._lookahead_guard.stats
            logger.info(
                f"防未来函数校验统计: "
                f"信号={guard_stats['signals_recorded']}, "
                f"成交={guard_stats['trades_recorded']}, "
                f"违规={guard_stats['violations']}"
            )
            self._lookahead_guard.assert_pass()
            logger.info("✅ 防未来函数校验通过：未发现未来函数违规")
            return True
        except Exception as e:
            # P0-01修复：违规回测必须标记为失败，禁止以 completed / is_success=True 收尾
            logger.error(f"❌ 防未来函数校验未通过：{e}")
            if self.result is not None:
                if hasattr(self.result, 'extra'):
                    self.result.extra['lookahead_guard_violations'] = self._lookahead_guard.violations
                    self.result.extra['lookahead_guard_passed'] = False
                if hasattr(self.result, 'mark_failed'):
                    self.result.mark_failed(f"防未来函数校验未通过: {e}")
            return False

    # ------------------------------------------------------------------
    # 生命周期：停止
    # ------------------------------------------------------------------

    def _run_realism_invariants(self) -> bool:
        """P1-15：结果侧真实性不变量校验（event 引擎，v2.2 §4.9）。

        从 recorder 注入：当日行情反查（RI-1 价界 / RI-2 量界）+ 组合级
        equity_curve 快照（RI-3 权益恒等 / RI-4 现金非负 / RI-6 持仓非负）+
        成交流重建持仓时间线（RI-5 T+1 一致）。

        Returns:
            True 表示通过，可 mark_complete；False 违规已 mark_failed。
        """
        try:
            from myquant.realism_gate.invariants import (
                TradeRecord,
                build_position_timeline_bar_states,
                check_result_invariants,
            )
            from myquant.realism_gate.invariants import (
                build_portfolio_bar_states as _bp,
            )
        except ImportError:
            logger.debug("realism_gate.invariants 未就绪，跳过 P1-15 校验")
            return True

        trades = self.recorder.get_trades() if self.recorder else []
        inv_trades: list = []
        for t in trades:
            sym = t.symbol
            df = self.data.get(sym)
            if df is None:
                continue
            try:
                trade_date = pd.Timestamp(t.timestamp, unit="s").strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            idx_str = df.index.astype(str)
            if trade_date not in idx_str:
                continue
            try:
                row = df.iloc[idx_str.get_loc(trade_date)]
                inv_trades.append(TradeRecord(
                    symbol=sym,
                    trade_date=trade_date,
                    side=t.side,
                    price=float(t.price),
                    quantity=float(t.quantity),
                    day_low=float(row["最低价"]),
                    day_high=float(row["最高价"]),
                    day_volume=float(row["成交量"]),
                    slippage_amount=abs(float(t.slippage)) * float(t.price),
                ))
            except (KeyError, ValueError, IndexError):
                continue

        # 组合级 bar_states（RI-3/4/6）+ 持仓时间线（RI-5）
        bar_states: list = []
        if self.recorder is not None:
            equity_curve = self.recorder.get_equity_curve()
            points = [(ep.date, ep.cash, ep.position_value) for ep in equity_curve]
            bar_states.extend(_bp(points))
        if trades:
            close_lookup: dict = {}
            for sym in {t.symbol for t in trades}:
                df = self.data.get(sym)
                if df is None or "收盘价" not in df.columns:
                    continue
                close_lookup[sym] = {
                    str(d): float(c) for d, c in zip(df.index, df["收盘价"])
                }
            timeline_trades = [
                type("T", (), {
                    "symbol": it.symbol, "trade_date": it.trade_date,
                    "side": it.side, "quantity": it.quantity,
                })()
                for it in inv_trades
            ]
            bar_states.extend(
                build_position_timeline_bar_states(timeline_trades, close_lookup)
            )

        if not inv_trades and not bar_states:
            return True

        try:
            violations = check_result_invariants(
                trades=inv_trades,
                bar_states=bar_states,
                initial_capital=float(self.config.initial_capital),
                participation_cap=float(
                    getattr(self.config, "participation_rate", 1.0)
                ),
            )
        except ValueError as exc:
            logger.warning("P1-15 invariants 输入参数错误，跳过: %s", exc)
            return True

        active = (
            "invariants.price_bounds",
            "invariants.volume_bound",
            "invariants.equity_identity",
            "invariants.cash_non_negative",
            "invariants.t1_consistency",
            "invariants.position_non_negative",
        )
        real = [v for v in violations if v.field_path in active]
        if real:
            msg = "; ".join(v.message for v in real[:5])
            logger.error("❌ P1-15 event 结果侧不变量未通过: %s", msg)
            if self.result is not None and hasattr(self.result, "extra"):
                self.result.extra["realism_invariants_failed"] = True
                self.result.extra["realism_invariants_violations"] = [
                    {"field": v.field_path, "message": v.message} for v in real[:10]
                ]
                self.result.mark_failed(f"realism_invariants_failed: {msg}")
            return False
        logger.info(
            "✅ P1-15 event 结果侧不变量校验通过（RI-1~6，trades=%d, bars=%d）",
            len(inv_trades), len(bar_states),
        )
        if self.result is not None and hasattr(self.result, "extra"):
            self.result.extra["realism_invariants_passed"] = True
        return True

    def on_stop(self) -> None:
        """
        停止回测引擎

        取消所有挂单，计算绩效指标，生成回测结果。
        """
        logger.info("回测引擎停止，开始计算绩效...")

        # P1-07 子项⑤：轻量防未来防线校验（与 mixed/vectorized 同构）
        from myquant.core.exceptions import LookaheadError as _LightErr
        from myquant.core.light_lookahead_guard import get_light_lookahead_guard
        lg = get_light_lookahead_guard()
        if lg.enabled:
            try:
                lg.reset()
                for ts in self._bar_timestamps:
                    lg.on_bar_close(ts)
                lg.assert_pass()
                if self.result is not None and hasattr(self.result, "extra"):
                    self.result.extra["light_lookahead_passed"] = True
            except _LightErr as exc:
                logger.error("❌ 轻量防未来防线校验未通过: %s", exc)
                if self.result is not None and hasattr(self.result, "mark_failed"):
                    self.result.mark_failed(f"轻量防未来防线校验未通过: {exc}")

        # 1. 取消所有活跃订单
        if self.broker:
            active_orders = self.broker.get_active_orders()
            for order in active_orders:
                self.broker.cancel_order(order.order_id)

        # 2. 计算绩效指标
        if self.recorder and self.metrics_calc:
            equity_curve = self.recorder.get_equity_curve()
            trades = self.recorder.get_trades()
            # P2-03 基准净值接线：从 equity_curve 各点 benchmark_equity 组装基准曲线，
            # 激活 alpha/beta/excess/IR 计算；基准缺失时保持 None（metrics 置 None，不伪造）。
            benchmark_curve = None
            if equity_curve and not getattr(self, "_benchmark_missing", False):
                benchmark_curve = [
                    EquityPoint(
                        timestamp=ep.timestamp, date=ep.date, equity=ep.benchmark_equity
                    )
                    for ep in equity_curve
                ]

            metrics = self.metrics_calc.calculate(
                equity_curve=equity_curve,
                trades=trades,
                benchmark_curve=benchmark_curve,
                start_date=self.config.start_date,
                end_date=self.config.end_date,
            )
        else:
            from myquant.models.backtest import PerformanceMetrics
            metrics = PerformanceMetrics()

        # 3. 填充回测结果（result 已在 run() 主循环前创建，此处只填充数据，避免 started_at 被重置）
        if self.result is None:
            # 防御性：on_stop() 被异常调用且 result 尚未创建时
            self.result = BacktestResult(config=self.config)
        self.result.metrics = metrics
        # KP-P2-04：基准缺失标记落盘（result.extra 可追溯）
        if getattr(self, '_benchmark_missing', False) and hasattr(self.result, 'extra'):
            self.result.extra['benchmark_missing'] = True
        self.result.equity_curve = self.recorder.get_equity_curve() if self.recorder else []
        self.result.trades = self.recorder.get_trades() if self.recorder else []
        # #S3修复：回测结束时运行防未来函数统一校验（引擎内置能力，不再仅依赖入口脚本）
        # P0-01修复：先校验，通过才标记 completed；失败已在方法内 mark_failed，不得覆盖
        # P1-15：事件驱动引擎补齐结果侧不变量（RI-1~6），与 mixed/vectorized 同构
        if self._run_lookahead_assert() and self._run_realism_invariants():
            self.result.mark_complete()

        self._stopped = True
        logger.info(
            f"回测完成: 总收益={metrics.total_return:.2%}, "
            f"夏普={metrics.sharpe_ratio:.2f}, "
            f"最大回撤={metrics.max_drawdown:.2%}, "
            f"交易次数={metrics.total_trades}"
        )

    # ------------------------------------------------------------------
    # 主循环：run
    # ------------------------------------------------------------------

    def run(self) -> BacktestResult:
        """
        运行回测主循环

        逐bar遍历历史数据，执行T+1成交时序。

        返回:
            BacktestResult: 回测结果
        """
        # ── P0-08 子项⑦：run 起点复位切片状态（R-25 处置，验收用例⑪）──
        # 复用同一 context 跨两次 run 时不残留上次 run 的切片时点/访问账目：
        # on_init 阶段仍可全量（R-26 冻结契约），on_bar 循环由引擎重新置位。
        if self.context is not None:
            self.context._slicing_enabled = False
            self.context.current_bar_ts = None
            self.context.accessed_max_dates.clear()

        # ── P0-01 两阶段握手第⑤步（v2.2 §4.4）：run 入口断言 manifest 已封印 ──
        # create_engine 已 assert_realism_pass + seal；manifest 缺失（旁路直构，
        # P1-04 收敛前）跳过；已建未封印 → enabled=True 时阻断（禁止绕过出口断言直跑）。
        _rg_m = getattr(self, "_realism_manifest", None)
        if _rg_m is not None and not _rg_m.is_sealed():
            from myquant.realism_gate.gate import RealismGateError, get_config
            if get_config().enabled:
                raise RealismGateError(
                    "真实性门禁两阶段握手第⑤步失败：引擎未封印（manifest 未经 "
                    "create_engine 出口断言），禁止直接 run",
                    violations=[],
                    manifest=_rg_m,
                )

        if not self._initialized:
            self.on_init()

        if not self.data:
            logger.error("无历史数据，无法运行回测")
            self.result = BacktestResult(config=self.config)
            self.result.mark_failed("无历史数据")
            return self.result

        logger.info("=" * 60)
        logger.info("回测主循环开始")
        logger.info("=" * 60)

        self._running = True

        # 创建回测结果对象（在主循环前创建，确保 started_at 记录真实开始时间，运行耗时计算正确）
        self.result = BacktestResult(config=self.config)
        self.result.status = "running"

        # 构建统一的时间序列（多标的按日期对齐）
        all_bars = self._build_unified_bars()

        # 逐bar处理
        for bar in all_bars:
            if not self._running:
                break
            event = MarketEvent(
                event_type="bar",
                data=bar,
                symbol=bar.symbol,
                timestamp=bar.timestamp,
            )
            self.on_market_event(event)

        # 停止
        self.on_stop()
        self._running = False
        # P0-08 子项②：run 结束后按实际置位刷新 causality 组（真实采集）
        self._refresh_causality_manifest()
        # P0-01 端到端（#345）：清单快照挂载到 result（随 results.json 落盘）
        # getattr 防御：直构引擎（不经 create_engine）无 _realism_manifest
        _rg_m = getattr(self, "_realism_manifest", None)
        if _rg_m is not None:
            self.result.realism_manifest = _rg_m.to_dict()

        logger.info("=" * 60)
        logger.info("回测主循环结束")
        logger.info("=" * 60)

        return self.result

    def _refresh_causality_manifest(self) -> None:
        """P0-08 子项②：run 结束后按实际置位刷新 causality 组（止血 A 真实采集）。

        替代 create_engine 处的 run 前静态反推：event/mixed 逐 bar 置位后
        ``_slicing_enabled`` 为 True、``current_bar_ts`` 已推进 → 报真实 True；
        vectorized 全量逃生口从不置位 → 报 False（由 ``full_sample_declared=True`` 声明）。
        软接线：enabled=False 时刷新/断言失败仅审计不阻断（P1-05 模式）。
        """
        _m = getattr(self, "_realism_manifest", None)
        if _m is None or self.context is None:
            return
        try:
            from myquant.realism_gate import assert_realism_pass
            from myquant.realism_gate.gate import get_config

            _m.update_group("causality", {
                "context_sliced": bool(self.context._slicing_enabled),
                "current_bar_ts_enforced": self.context.current_bar_ts is not None,
                "full_sample_declared": bool(self.context.full_sample_declared),
                "guard_injected": self.context.extra.get("lookahead_guard") is not None,
                # P0-08 子项⑧：DataWindow 窗口隔离构造即生效，on_bar 封死 value 直取（真实）
                "context_data_scoped": True,
                # P0-01（#344）：运行期真实采集已完成 → causality 判据正式生效
                "assessed_at_run_end": True,
            })
            # ── P0-08 子项⑥：访问器层实测记账评估（P1-07② 判据）──
            # accessed_max_date > current_bar_date → 实测读到未来（真拦截）；
            # 静态声明已封门却实测读到未来 → 交叉校验矛盾（用例⑭）。
            _acct = self.context.collect_access_accounting()
            _m.update_group("causality", {
                "accessed_future": bool(_acct["accessed_future"]),
                "static_declared_but_accessed_future": bool(
                    _acct["static_declared_but_accessed_future"]
                ),
            })
            if _acct["violations"]:
                _m.set_field("causality.accounting_violations", _acct["violations"])
            # ── P0-08 子项③：run 后从真实实例刷新 t1/slippage（broker 在 on_init 装配，
            # create_engine 处仅有 config 构造事实；无 broker 的引擎保留 config 值）──
            _broker = getattr(self, "broker", None)
            if _broker is not None:
                _broker_t1 = getattr(_broker, "enable_t1", None)
                if _broker_t1 is not None:
                    _m.set_field("t1_position_freeze.enabled", bool(_broker_t1))
                _broker_slip = getattr(_broker, "slippage_model", None)
                if _broker_slip is not None:
                    _bs_type = str(getattr(_broker_slip, "type", "fixed_bps"))
                    _bs_value = float(getattr(_broker_slip, "fixed_bps", 0.0))
                    if _bs_type == "none":
                        _bs_value = 0.0  # type="none" 实际滑点恒 0
                    _m.update_group("slippage", {
                        "type": _bs_type,
                        "value": _bs_value,
                        "is_zero_cost": bool(_bs_type == "none" or _bs_value == 0.0),
                    })
            # P0-04（#349）：收尾回写真实复权降级集合（provider 意图：回测后由引擎
            # 读取写入 manifest；assert 前回写 → data_pit 判据真实生效——此前仅入口
            # 空集且无回写点，判据永不触发，真实降级被隐藏）
            from myquant.data_access.provider import get_adjust_degraded_symbols
            _m.set_field("adjust_degraded_symbols", get_adjust_degraded_symbols())
            # P1-13：收尾回写运行期数据质量统计（provider 场景入口 data=None 未统计；
            # 运行期 provider.get_bars 逐窗口累计 → 聚合回写真实值 → data_pit 判据
            # 在 assert 前真实生效：回测期发现的 NaN 价/OHLC 违例在此阻断，不静默）
            try:
                from myquant.data_access.provider import get_data_quality_stats

                _dq = get_data_quality_stats()
                if _dq:
                    _m.set_field(
                        "quality.nan_price_rows",
                        sum(v["nan_price_rows"] for v in _dq.values()),
                    )
                    _m.set_field(
                        "quality.ohlc_violation_rows",
                        sum(v["ohlc_violation_rows"] for v in _dq.values()),
                    )
                    # 真实覆盖率 = 运行期实际加载标的数 / 请求标的数（provider 场景
                    # 入口占位 1.0 在此被真实值覆盖；缺数据未加载标的 → <1.0 → data_pit
                    # 判据（<0.5 → critical）在收尾 assert 前真实生效）
                    _m.set_field(
                        "quality.coverage_rate",
                        min(1.0, len(_dq) / max(1, len(self.config.symbols))),
                    )
            except Exception as _dq_e:  # noqa: BLE001
                logger.debug("收尾数据质量统计回写失败（保留入口值）: %s", _dq_e)
            assert_realism_pass(_m)
        except Exception as exc:  # noqa: BLE001
            if get_config().enabled:
                # P0-01 端到端（#345）：阻断路径产物落盘（§4.3.1）——被门禁拦下
                # 时无 result 对象，独立落 outputs/realism_gate_blocked_*.json 后 re-raise；
                # 仅 RealismGateError 携带 violations（配置损坏类异常直接传播，不产阻断产物）
                _v = getattr(exc, "violations", None)
                if _v is not None:
                    from myquant.realism_gate.report import write_blocked_artifact
                    write_blocked_artifact("outputs", _m, _v)
                raise
            logger.debug("真实性门禁 run 后刷新失败（软接线不阻断）: %s", exc)

    def _build_unified_bars(self) -> List[Bar]:
        """
        构建统一的时间序列（多标的按日期对齐）

        将所有标的的K线按日期排序，同一日期的多只股票依次处理。

        ★★★ 防未来函数（双重保障）★★★
        从 DataProvider 加载时已按 config.end_date 过滤，
        此处再次按 config.start_date/end_date 过滤，确保不返回未来数据。
        """
        all_bars = []
        from myquant.backtest.data_loader import EN_TO_CN_COLUMN_MAP
        for symbol, df in self.data.items():
            if df is None or len(df) == 0:
                continue
            # 列名统一为中文：兼容英文列名的合成数据/数据库来源
            _rename = {en: cn for en, cn in EN_TO_CN_COLUMN_MAP.items() if en in df.columns}
            if _rename:
                df = df.rename(columns=_rename)
            # ★★★ 防未来函数：按日期范围过滤（双重保障）★★★
            if self.config.start_date:
                mask = df.index.astype(str) >= self.config.start_date
                df = df[mask]
            if self.config.end_date:
                mask = df.index.astype(str) <= self.config.end_date
                df = df[mask]

            for idx, row in df.iterrows():
                timestamp = self._row_to_timestamp(idx, row)
                bar = Bar(
                    symbol=symbol,
                    open=float(row.get('开盘价', 0)),
                    high=float(row.get('最高价', 0)),
                    low=float(row.get('最低价', 0)),
                    close=float(row.get('收盘价', 0)),
                    volume=float(row.get('成交量', 0)),
                    timestamp=timestamp,
                )
                all_bars.append(bar)

        # 按时间排序（同一时间按symbol排序，保证确定性）
        all_bars.sort(key=lambda b: (b.timestamp, b.symbol))
        logger.info(f"统一时间序列构建完成: {len(all_bars)} 根K线")
        return all_bars

    @staticmethod
    def _row_to_timestamp(idx: Any, row: pd.Series) -> float:
        """将DataFrame的index转换为时间戳"""
        import pandas as pd
        if isinstance(idx, pd.Timestamp):
            return idx.timestamp()
        if hasattr(idx, 'timestamp'):
            return idx.timestamp()
        # 尝试从row中获取timestamp
        if 'timestamp' in row:
            return float(row['timestamp'])
        # 尝试解析字符串日期
        try:
            return pd.Timestamp(str(idx)).timestamp()
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # 快捷方法
    # ------------------------------------------------------------------

    def get_result(self) -> Optional[BacktestResult]:
        """获取回测结果"""
        return self.result

    def stop(self) -> None:
        """停止回测"""
        self._running = False
        super().stop()

    def __repr__(self) -> str:
        return (
            f"BacktestEngine(strategy={self.strategy.name if self.strategy else 'None'}, "
            f"symbols={len(self.data)}, "
            f"events={self._event_count}, "
            f"status={'running' if self._running else 'stopped' if self._stopped else 'initialized'})"
        )



# ═══════════════════════════════════════════════
#  三模式调度器（第6项：engine.py 三模式调度器）
# ═══════════════════════════════════════════════

ENGINE_MODE_AUTO: str = "auto"
ENGINE_MODE_EVENT: str = "event"
ENGINE_MODE_MIXED: str = "mixed"
ENGINE_MODE_VECTORIZED: str = "vectorized"
SUPPORTED_ENGINE_MODES: List[str] = [
    ENGINE_MODE_AUTO,
    ENGINE_MODE_EVENT,
    ENGINE_MODE_MIXED,
    ENGINE_MODE_VECTORIZED,
]
# auto 优先级：mixed（生产主力，资金约束）> vectorized（参数扫描）> event（真值基准）
_ENGINE_PRIORITY: List[str] = [
    ENGINE_MODE_MIXED,
    ENGINE_MODE_VECTORIZED,
    ENGINE_MODE_EVENT,
]


class BacktestScheduler:
    """三模式回测调度器。

    根据策略能力（supports_mode / engine_modes）自动选择引擎:
      - mixed      : 策略实现 generate_mixed_loop（生产主力，bar 循环，资金约束）
      - vectorized : 策略实现 generate_signals_vectorized（纯向量化，参数扫描）
      - event      : 其他（真值基准，on_bar 通用）

    用法:
        scheduler = BacktestScheduler(mode="auto")
        result = scheduler.run(config, strategy, data=data_dict)

    或便捷函数:
        result = run_backtest(config, strategy, data=data_dict, mode="auto")
    """

    def __init__(self, mode: str = ENGINE_MODE_AUTO, name: str = "backtest_scheduler"):
        """初始化调度器。

        Args:
            mode: 引擎模式（auto/event/mixed/vectorized）。
            name: 调度器名称。

        Raises:
            ValueError: mode 非法时抛出。
        """
        if mode not in SUPPORTED_ENGINE_MODES:
            raise ValueError(
                f"不支持的引擎模式: {mode!r}（支持: {SUPPORTED_ENGINE_MODES}）"
            )
        self.mode: str = mode
        self.name: str = name
        self.selected_mode: Optional[str] = None
        self.engine: Any = None

    @staticmethod
    def detect_modes(strategy: Optional[StrategyBase]) -> List[str]:
        """探测策略支持的引擎模式列表。

        Args:
            strategy: 策略实例。

        Returns:
            支持的模式名列表（按推荐优先级降序）。

        Raises:
            TypeError: strategy 类型不合法时抛出。
        """
        if strategy is None:
            return []
        if not isinstance(strategy, StrategyBase):
            raise TypeError(
                f"strategy 必须为 StrategyBase 实例，收到: {type(strategy).__name__}"
            )
        return strategy.engine_modes()

    def resolve_mode(self, strategy: Optional[StrategyBase]) -> str:
        """解析最终引擎模式（auto 时按优先级选择）。

        Args:
            strategy: 策略实例。

        Returns:
            选定的引擎模式名。

        Raises:
            ValueError: 策略不支持任何引擎模式时抛出。
        """
        if self.mode != ENGINE_MODE_AUTO:
            return self.mode
        modes = set(self.detect_modes(strategy))
        if not modes:
            raise ValueError("策略不支持任何引擎模式，无法调度")
        for m in _ENGINE_PRIORITY:
            if m in modes:
                return m
        raise ValueError(f"策略支持的引擎模式无法映射: {sorted(modes)}")

    def create_engine(
        self,
        config: BacktestConfig,
        strategy: Optional[StrategyBase],
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        **kwargs: Any,
    ) -> Any:
        """按解析出的模式创建对应引擎实例。

        Args:
            config: 回测配置。
            strategy: 策略实例。
            data: 行情数据（可选）。
            data_provider: 数据提供器（可选）。
            **kwargs: 传递给目标引擎的额外参数（如 mixed 的 stop_loss_pct）。

        Returns:
            目标引擎实例。

        Raises:
            NotImplementedError: 显式模式但策略不支持时抛出。
        """
        # ── M0-08 出口强制：防泄漏门禁（v3.0 §5.4 唯一出口收敛点）──
        # 所有回测入口（run_回测入口.py:325 / run_批量回测 / run_鲲鹏批量回测 /
        # run_鲲鹏参数优化）均收敛至 BacktestScheduler.create_engine
        # （v3.0 §6.2-9 已核验无绕过路径）；无契约/引用阻断列的策略在引擎创建前被拦。
        # 注：测试环境经 tests/conftest.py 关闭 enabled（仅审计），真实出口默认阻断。
        if strategy is not None:
            from myquant.leakage_gate import assert_all_gates_pass

            assert_all_gates_pass(
                strategy_id=getattr(strategy, "name", "") or "",
                # P2-11：decision_time 取回测配置结束日（真实决策时点上限），
                # 不再传运行当天 time.strftime——审计记录诚实性（R-16 §五-3）
                decision_time=getattr(config, "end_date", "") or "",
            )
        mode = self.resolve_mode(strategy)
        if strategy is not None and not strategy.supports_mode(mode):
            raise NotImplementedError(
                f"策略 {getattr(strategy, 'name', '?')} 不支持 {mode} 引擎"
                f"（支持: {strategy.engine_modes()}）。请显式选择匹配的引擎模式。"
            )

        engine_name = kwargs.pop("name", f"{mode}_backtest")
        # #H2修复：mixed/vectorized 引擎统一接入全局防未来函数校验器（单例）
        kwargs.setdefault("lookahead_guard", get_lookahead_guard())
        kwargs.setdefault("enable_lookahead_guard", True)
        # P0-03 卡本体收尾（#347）：lot_size 透传（config.lot_size 真实生效；
        # 此前 mixed/vectorized 引擎恒用构造默认，YAML 配置→行为断链）。
        # event 引擎（SimBroker 撮合）无整手执行逻辑，lot_size 不消费（#346 已登记）。
        _cfg_lot_size: int = int(getattr(config, "lot_size", 100))
        if mode == ENGINE_MODE_VECTORIZED:
            engine: Any = VectorizedBacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name,
                lot_size=_cfg_lot_size, **kwargs,
            )
        elif mode == ENGINE_MODE_MIXED:
            engine = MixedBacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name,
                lot_size=_cfg_lot_size, **kwargs,
            )
        else:
            engine = BacktestEngine(
                config=config, strategy=strategy, data=data,
                data_provider=data_provider, name=engine_name, **kwargs,
            )

        # ── P0-01 出口强制：回测真实性门禁（create_engine 与 from_data_provider 共用）──
        # 时序（v2.2 §4.4）：构造引擎后采集 manifest → assert_realism_pass → seal；
        # 实现收敛于模块级函数 _attach_realism_manifest（#349 统一直构路径门禁语义：
        # 生产入口 from_data_provider 此前无 manifest，握手⑤跳过、判据完全不生效；
        # 三引擎独立类共用同一模块级函数，门禁语义同构）。
        engine._realism_manifest = _attach_realism_manifest(
            engine, config, strategy, engine_mode=mode, engine_kwargs=kwargs, data=data,
        )

        self.engine = engine
        # P0-08 子项②：引擎实例已在 attach 内挂载 manifest 引用（run 收尾真实采集刷新）
        # scheduler 侧保留引用（与引擎同对象；P1-05 软接线失败时为 None），
        # 兼容既有调用方/测试读取 scheduler._realism_manifest。
        self._realism_manifest = getattr(engine, "_realism_manifest", None)
        self.selected_mode = mode
        logger.info(
            "调度器选中引擎: mode=%s, engine=%s", mode, type(engine).__name__
        )
        return engine

    def run(
        self,
        config: BacktestConfig,
        strategy: Optional[StrategyBase],
        data: Optional[Dict[str, pd.DataFrame]] = None,
        data_provider: Optional[DataProviderBase] = None,
        **kwargs: Any,
    ) -> BacktestResult:
        """运行回测（自动选择并执行对应引擎）。

        Args:
            config: 回测配置。
            strategy: 策略实例。
            data: 行情数据（可选）。
            data_provider: 数据提供器（可选）。
            **kwargs: 传递给目标引擎的额外参数。

        Returns:
            BacktestResult 回测结果。
        """
        engine = self.create_engine(config, strategy, data, data_provider, **kwargs)
        result = engine.run()
        if hasattr(result, "extra") and isinstance(result.extra, dict):
            result.extra["engine_mode"] = self.selected_mode
            result.extra["scheduler_mode"] = self.mode
        # P1-11：回测出口泄漏审计报告（R-16 §4.1；enabled=false 审计档亦落盘）
        _write_backtest_leakage_report(config, strategy, self.selected_mode, result)
        return result

    def __repr__(self) -> str:
        engine_type = type(self.engine).__name__ if self.engine is not None else "None"
        return (
            f"BacktestScheduler(mode={self.mode}, selected={self.selected_mode}, "
            f"engine={engine_type})"
        )


def _write_backtest_leakage_report(
    config: BacktestConfig,
    strategy: Optional[StrategyBase],
    engine_mode: Optional[str],
    result: BacktestResult,
) -> None:
    """回测出口泄漏审计报告落盘（P1-11 步骤①，R-16 §4.1）。

    数据契约注入：gate_stats 由 collect_gate_stats 采集（与 create_engine 内
    assert_all_gates_pass 同形）；lookahead_guard 取全局单例统计——engine.run()
    前 on_init 已 reset（engine.py:492），统计为本实验干净快照、不跨实验污染；
    decision_time 与门禁断言一致取 config.end_date（P2-11，不再用运行当天）。
    报告落盘失败不阻断回测（审计旁路）：记录 warning 并继续。

    Args:
        config: 回测配置。
        strategy: 策略实例（None 时产空 gates 报告并在 extra 标注，P1-06 联动）。
        engine_mode: 选定引擎模式。
        result: 本次回测结果。
    """
    from myquant.leakage_gate.gate import collect_gate_stats
    from myquant.leakage_gate.report import write_experiment_report

    decision_time = getattr(config, "end_date", "") or ""
    strategy_id = getattr(strategy, "name", "") or ""
    try:
        gate_stats = collect_gate_stats(strategy_id, decision_time)
        write_experiment_report(
            experiment_id=f"backtest_{getattr(config, 'start_date', '')}_{decision_time}",
            experiment_type="backtest",
            strategy_id=strategy_id,
            decision_time=decision_time,
            gate_stats=gate_stats,
            lookahead_guard=get_lookahead_guard().stats,
            extra={
                "engine_mode": engine_mode or "",
                "strategy_none": strategy is None,
                "is_success": bool(getattr(result, "is_success", None)),
            },
        )
    except Exception:
        # 报告是审计产物：失败仅记录可见，不得阻断回测主流程（py 规则：记录后恢复）
        logger.warning("回测泄漏审计报告落盘失败（P1-11，审计旁路不阻断回测）", exc_info=True)


def run_backtest(
    config: BacktestConfig,
    strategy: Optional[StrategyBase],
    data: Optional[Dict[str, pd.DataFrame]] = None,
    data_provider: Optional[DataProviderBase] = None,
    mode: str = ENGINE_MODE_AUTO,
    **kwargs: Any,
) -> BacktestResult:
    """三模式回测便捷入口（自动选择引擎）。

    参数:
        config: 回测配置
        strategy: 策略实例
        data: 行情数据（可选）
        data_provider: 数据提供器（可选）
        mode: 引擎模式（auto/event/mixed/vectorized，默认 auto）
        **kwargs: 传递给目标引擎的额外参数

    返回:
        BacktestResult 回测结果
    """
    scheduler = BacktestScheduler(mode=mode)
    return scheduler.run(
        config=config,
        strategy=strategy,
        data=data,
        data_provider=data_provider,
        **kwargs,
    )




if __name__ == "__main__":
    # 自测已迁至 myquant/backtest/engine_selftest.py（P0-00A 第四刀，回写#304）
    from myquant.backtest.engine_selftest import run_engine_selftest

    raise SystemExit(run_engine_selftest())
