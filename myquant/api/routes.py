# 文件名：myquant/api/routes.py
"""
routes.py - API路由聚合
========================
REST API路由定义与聚合，基于FastAPI。

v1实现范围（基础骨架）:
  - create_app() 创建FastAPI应用（注册所有路由）
  - 健康检查 API
  - 策略管理 API（列表/详情）
  - 回测 API（运行/查询结果）
  - 实盘 API（状态/启动/停止）
  - 风控 API（状态/暂停/恢复）
  - 数据 API（质量报告）
  - 数据质量查询 API（任务16：总览/运行列表/运行详情/脏数据统计/实时指标）
  - Prometheus 指标导出 API（任务16：/api/metrics）
  - 告警 API（任务16：/api/alerts 列表/确认/解决）
  - 数据质量详细响应 schema（任务17：routes 端点接入 QualityMetricsInfo/CleanRunsData/QualityOverviewData/AlertListData 等 response_model）
  - 订单 API（下单/撤单，TODO占位）
  - 持仓 API（列表，TODO占位）
  - 认证中间件集成（API Key）
  - 限流中间件集成
  - CORS配置
  - 统一异常处理

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（实盘控制/订单/持仓/实时行情API全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — API只返回当前已知数据，不预测未来

防未来函数说明:
  - 所有API只返回当前已知的账户/持仓/行情/回测结果数据
  - 回测结果API严格按时间顺序返回，不泄露未来信息
  - 实盘状态API只返回当前实时数据，不预判未来走势
  - 数据质量API只返回已清洗数据的统计（清洗运行 end_time 严格过滤由任务13保证）
  - /api/metrics 只导出已采集的指标，/api/alerts 只返回已发生的告警
  - 下单API只提交当前订单，不预测成交结果

TODO(后续迭代):
  - WebSocket实时行情推送
  - 策略参数动态配置API
  - 风控规则配置API
  - 数据下载/清洗触发API
  - 用户管理API
  - 系统监控API
  - 日志查询API
  - API文档增强（Swagger/ReDoc自定义）

依赖: fastapi（可选）, typing, datetime, myquant.core.logger, myquant.api.models, myquant.api.middleware, myquant.api.dependencies
"""

from __future__ import annotations

import os

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from myquant.core.logger import get_logger
from myquant.api.models.schemas import (
    ApiResponse,
    ErrorResponse,
    HealthResponse,
    StrategyInfo,
    StrategyListResponse,
    BacktestRequest,
    BacktestResponse,
    BacktestMetrics,
    LiveStatusResponse,
    RiskStatusResponse,
    DataQualityResponse,
    OrderRequest,
    OrderResponse,
    QualityMetricsInfo,
    CleanRunInfo,
    CleanRunsData,
    DirtyData,
    QualityOverviewData,
    AlertListData,
    AlertAckData,
    AlertResolveData,
)

logger = get_logger("api.routes")

# 检查fastapi是否安装
try:
    from fastapi import FastAPI, HTTPException, Depends, Header, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, PlainTextResponse
    from starlette.middleware.base import BaseHTTPMiddleware
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    logger.warning("[api] fastapi未安装，API服务不可用。请运行: pip install fastapi uvicorn")

    # 降级：定义占位类
    class FastAPI:  # type: ignore[no-redef]
        """FastAPI未安装时的占位类"""
        def __init__(self, **kwargs: Any):
            self.title = kwargs.get("title", "API")
            self.version = kwargs.get("version", "v1.0.0")
            self.routes = []

        def get(self, path: str, **kwargs: Any):
            def decorator(func: Any):
                return func
            return decorator

        def post(self, path: str, **kwargs: Any):
            def decorator(func: Any):
                return func
            return decorator

        def include_router(self, *args: Any, **kwargs: Any):
            pass

        def add_middleware(self, *args: Any, **kwargs: Any):
            pass

        def exception_handler(self, *args: Any, **kwargs: Any):
            def decorator(func: Any):
                return func
            return decorator

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int = 500, detail: str = ""):
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    def Depends(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None

    def Header(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None

    def Query(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        return None

    class BaseHTTPMiddleware:  # type: ignore[no-redef]
        """fastapi未安装时的占位类，保证模块顶层中间件类可正常定义。"""

        def __init__(self, app: Any, **kwargs: Any):
            self.app = app


# ═══════════════════════════════════════════════
#  内存存储（v1使用，后续迭代替换为数据库）
# ═══════════════════════════════════════════════

_backtest_results: Dict[str, BacktestResponse] = {}
_backtest_counter = 0


# ═══════════════════════════════════════════════
#  认证 / 限流中间件（C1修复：接线 create_app 的 enable_auth/enable_rate_limit）
# ═══════════════════════════════════════════════

class _AuthMiddleware(BaseHTTPMiddleware):
    """API Key 认证中间件

    校验请求头 X-API-Key（H6修复：query 传 key 默认禁用，避免密钥进浏览器历史/代理日志）；
    未认证请求返回 401；认证未启用（enabled=False）时放行；
    健康检查/API文档路径豁免（监控探活与文档浏览需要免认证）。
    """

    def __init__(self, app: Any, auth: Any, exempt_paths: Set[str]) -> None:
        super().__init__(app)
        self._auth = auth
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        if not self._auth.enabled or request.url.path in self._exempt_paths:
            return await call_next(request)
        headers = {k: v for k, v in request.headers.items()}
        query = dict(request.query_params)
        if self._auth.verify_request(headers, query):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error="Unauthorized",
                message="无效的API Key：请通过请求头 X-API-Key 提供有效密钥",
            ).model_dump(),
            headers={"WWW-Authenticate": "ApiKey"},
        )


class _RateLimitMiddleware(BaseHTTPMiddleware):
    """IP 维度限流中间件

    按客户端 IP 固定窗口限流；超限请求返回 429（附 Retry-After 头）；
    健康检查/API文档路径豁免（监控探活不占配额）。
    """

    def __init__(self, app: Any, limiter: Any, exempt_paths: Set[str]) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        if request.url.path in self._exempt_paths:
            return await call_next(request)
        client_ip = request.client.host if request.client else "unknown"
        result = self._limiter.allow(client_ip)
        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(
                    error="RateLimited",
                    message=f"请求过于频繁，请在 {result.retry_after:.0f} 秒后重试",
                ).model_dump(),
                headers={"Retry-After": str(max(1, int(result.retry_after)))},
            )
        return await call_next(request)



# ═══════════════════════════════════════════════
#  创建FastAPI应用
# ═══════════════════════════════════════════════

def create_app(
    title: str = "Pythonwdhmm 量化交易系统 API",
    version: str = "v1.0.0",
    enable_auth: bool = True,
    enable_rate_limit: bool = True,
    data_quality_explorer: Any = None,
    metrics_collector: Any = None,
    alert_manager: Any = None,
) -> FastAPI:
    """
    创建FastAPI应用并注册所有路由

    Parameters
    ----------
    title : str
        API标题
    version : str
        API版本
    enable_auth : bool
        是否启用API Key认证（默认开启；认证Key从环境变量 QUANT_API_KEYS
        或 api_keys 参数读取，未配置时拒绝所有请求——安全默认）
    enable_rate_limit : bool
        是否启用IP限流（默认开启；阈值经 QUANT_RATE_LIMIT_MAX /
        QUANT_RATE_LIMIT_WINDOW 环境变量配置，默认 100 次/60 秒）
    data_quality_explorer : DataQualityExplorer | None
        数据质量探查器（任务16，None则自动构建全局单例）
    metrics_collector : MetricsCollector | None
        指标采集器（任务16，None则用全局单例）
    alert_manager : AlertManager | None
        告警管理器（任务16，None则用全局单例）

    Returns
    -------
    FastAPI : FastAPI应用实例
    """
    if not FASTAPI_AVAILABLE:
        logger.warning("[api] fastapi未安装，返回降级应用实例。请运行: pip install fastapi uvicorn")
        # 返回降级占位实例，不抛出异常，便于在无fastapi环境下导入和测试
        return FastAPI(title=title, version=version)

    app = FastAPI(
        title=title,
        version=version,
        description="量化交易系统REST API，支持策略管理、回测、实盘、风控、数据、数据质量、监控等功能。",
    )

    # ── M4修复：服务获取统一走依赖注入层（api/dependencies/get_services.py）──
    # 外部注入的服务先注册到 ServiceContainer（保持注入优先能力），
    # 未注入时由依赖注入函数懒加载全局单例并回填容器。
    # 端点内的调用点（_get_explorer/_get_collector/_get_alert_manager）签名不变，
    # 6个数据质量端点+1个指标端点+3个告警端点自动切换为依赖注入驱动。
    from myquant.api.dependencies.get_services import (
        get_alert_manager as _di_get_alert_manager,
        get_data_quality_explorer as _di_get_explorer,
        get_metrics_collector as _di_get_collector,
        get_service_container,
    )
    _container = get_service_container()
    if data_quality_explorer is not None:
        _container.register("data_quality_explorer", data_quality_explorer)
    if metrics_collector is not None:
        _container.register("metrics_collector", metrics_collector)
    if alert_manager is not None:
        _container.register("alert_manager", alert_manager)

    def _get_explorer() -> Any:
        """获取数据质量探查器（M4修复：委托依赖注入层）"""
        return _di_get_explorer()

    def _get_collector() -> Any:
        """获取指标采集器（M4修复：委托依赖注入层）"""
        return _di_get_collector()

    def _get_alert_manager() -> Any:
        """获取告警管理器（M4修复：委托依赖注入层）"""
        return _di_get_alert_manager()

    # CORS配置（C1修复：通配符 * 与 allow_credentials=True 是无效组合，
    # 改为环境变量白名单；默认全开放，生产建议设置 QUANT_CORS_ORIGINS）
    cors_origins = [
        o.strip()
        for o in os.environ.get("QUANT_CORS_ORIGINS", "*").split(",")
        if o.strip()
    ]
    cors_allow_credentials = cors_origins != ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── L3修复：健康检查注册表（组件可注入真实状态，未注入模块报 unknown）──
    app.state.health_checkers: Dict[str, Any] = {}

    def register_health_checker(name: str, checker: Any) -> None:
        """注册组件健康检查器：checker 为返回 bool/str 的可调用对象"""
        app.state.health_checkers[name] = checker
        logger.info(f"[api] 健康检查器已注册: {name}")

    app.register_health_checker = register_health_checker

    # ── C1修复：认证/限流中间件真实接线（原 enable_auth/enable_rate_limit 参数悬空）──
    from myquant.api.middleware.auth import APIKeyAuth
    from myquant.api.middleware.rate_limit import FixedWindowLimiter

    _auth = APIKeyAuth(enabled=enable_auth)
    _limiter = FixedWindowLimiter(
        max_requests=int(os.environ.get("QUANT_RATE_LIMIT_MAX", "100")),
        window_seconds=int(os.environ.get("QUANT_RATE_LIMIT_WINDOW", "60")),
    )
    # 豁免路径：健康检查（监控探活）与 API 文档（浏览器浏览）
    # L7修复: 移除 /health——M10 后真实路由为 /api/health，/health 已 404（死配置）
    _exempt_paths = {"/api/health", "/docs", "/redoc", "/openapi.json"}

    if enable_rate_limit:
        app.add_middleware(
            _RateLimitMiddleware,
            limiter=_limiter,
            exempt_paths=_exempt_paths,
        )
        logger.info(f"[api] 限流中间件已启用: {_limiter.max_requests}次/{_limiter.window_seconds}秒/IP")
    if enable_auth:
        app.add_middleware(
            _AuthMiddleware,
            auth=_auth,
            exempt_paths=_exempt_paths,
        )
        logger.info(f"[api] 认证中间件已启用: keys={_auth.key_count}, header={_auth.header_name}")

    # 全局异常处理
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=f"HTTP{exc.status_code}",
                message=str(exc.detail),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request, exc):
        logger.error(f"[api] 未处理异常: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="InternalError",
                message="服务器内部错误",
            ).model_dump(),
        )

    # ═══════════════════════════════════════════
    #  健康检查
    # ═══════════════════════════════════════════

    @app.get("/api/health", response_model=ApiResponse, tags=["系统"])
    async def health_check():
        """健康检查（L3修复: 组件状态来自注入的检查器，未注入报 unknown，不再字面量谎报）"""
        checkers: Dict[str, Any] = getattr(app.state, "health_checkers", {}) or {}
        modules: Dict[str, str] = {}
        degraded = False
        for name, checker in checkers.items():
            try:
                result = checker() if callable(checker) else checker
                if result is True or result == "healthy":
                    modules[name] = "healthy"
                elif result is False or result == "down":
                    modules[name] = "down"
                    degraded = True
                else:
                    modules[name] = "degraded"
            except Exception as e:
                modules[name] = "down"
                degraded = True
                logger.error(f"[api] 健康检查器 {name} 异常: {e}")
        # 未注入的组件模块标记 unknown（保持清单完整，但不谎报健康）
        for name in ("data", "strategy", "backtest", "live", "risk", "allocation"):
            if name not in modules:
                modules[name] = "unknown"
        health = HealthResponse(
            status="degraded" if degraded else "healthy",
            version=version,
            modules=modules,
        )
        return ApiResponse.success(data=health.model_dump())

    # ═══════════════════════════════════════════
    #  策略管理
    # ═══════════════════════════════════════════

    @app.get("/api/strategies", response_model=ApiResponse, tags=["策略"])
    async def list_strategies():
        """获取已注册策略列表"""
        # v1占位：后续迭代从策略管理器获取
        strategies = [
            StrategyInfo(
                name="ma_cross",
                class_name="MACrossStrategy",
                description="均线交叉策略",
                params={"short_period": 5, "long_period": 20},
                registered=True,
            ),
            StrategyInfo(
                name="rsi",
                class_name="RSIStrategy",
                description="RSI超买超卖策略",
                params={"period": 14, "oversold": 30, "overbought": 70},
                registered=True,
            ),
        ]
        result = StrategyListResponse(strategies=strategies, total=len(strategies))
        return ApiResponse.success(data=result.model_dump())

    @app.get("/api/strategies/{strategy_name}", response_model=ApiResponse, tags=["策略"])
    async def get_strategy(strategy_name: str):
        """获取策略详情"""
        # v1占位
        return ApiResponse.success(data={
            "name": strategy_name,
            "description": f"策略 {strategy_name} 的详细信息（v1占位）",
            "params": {},
        })

    # ═══════════════════════════════════════════
    #  回测
    # ═══════════════════════════════════════════

    @app.post("/api/backtest/run", response_model=ApiResponse, tags=["回测"])
    async def run_backtest(request: BacktestRequest):
        """运行回测"""
        global _backtest_counter
        _backtest_counter += 1
        backtest_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_backtest_counter}"

        # v1占位：后续迭代调用回测引擎
        metrics = BacktestMetrics(
            total_return=0.0523,
            annual_return=0.125,
            sharpe_ratio=1.25,
            max_drawdown=0.085,
            win_rate=0.55,
            total_trades=23,
            calmar_ratio=1.47,
            sortino_ratio=1.82,
        )
        result = BacktestResponse(
            backtest_id=backtest_id,
            status="completed",
            metrics=metrics,
            equity_curve=[],
            trades=[],
            message=f"回测完成（v1占位，策略={request.strategy_name}, 标的={request.symbol}）",
        )
        _backtest_results[backtest_id] = result
        return ApiResponse.success(data=result.model_dump(), message="回测任务已提交")

    @app.get("/api/backtest/{backtest_id}", response_model=ApiResponse, tags=["回测"])
    async def get_backtest_result(backtest_id: str):
        """获取回测结果"""
        result = _backtest_results.get(backtest_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"回测任务不存在: {backtest_id}")
        return ApiResponse.success(data=result.model_dump())

    # ═══════════════════════════════════════════
    #  实盘
    # ═══════════════════════════════════════════

    @app.get("/api/live/status", response_model=ApiResponse, tags=["实盘"])
    async def get_live_status():
        """获取实盘状态"""
        # v1占位：后续迭代从实盘引擎获取
        status = LiveStatusResponse(
            connected=False,
            running=False,
            broker="paper",
            total_assets=100000.0,
            available_cash=100000.0,
            position_value=0.0,
            today_pnl=0.0,
            positions=[],
        )
        return ApiResponse.success(data=status.model_dump())

    @app.post("/api/live/start", response_model=ApiResponse, tags=["实盘"])
    async def start_live():
        """启动实盘"""
        # v1占位：后续迭代调用实盘引擎
        return ApiResponse.success(data={"status": "starting"}, message="实盘启动指令已发送（v1占位）")

    @app.post("/api/live/stop", response_model=ApiResponse, tags=["实盘"])
    async def stop_live():
        """停止实盘"""
        # v1占位
        return ApiResponse.success(data={"status": "stopping"}, message="实盘停止指令已发送（v1占位）")

    @app.get("/api/live/positions", response_model=ApiResponse, tags=["实盘"])
    async def get_positions():
        """获取持仓列表"""
        # v1占位：后续迭代从实盘引擎获取
        return ApiResponse.success(data={"positions": [], "total": 0}, message="持仓列表（v1占位）")

    # ═══════════════════════════════════════════
    #  风控
    # ═══════════════════════════════════════════

    @app.get("/api/risk/status", response_model=ApiResponse, tags=["风控"])
    async def get_risk_status():
        """获取风控状态"""
        # v1占位：后续迭代从风控管理器获取
        status = RiskStatusResponse(
            status="NORMAL",
            health_status="HEALTHY",
            status_reason="系统正常",
            enabled_rules=["position_limit", "single_order_limit", "max_drawdown", "stop_loss",
                          "take_profit", "rate_limit", "price_limit", "trading_hours",
                          "blacklist", "cash_availability"],
            today_rejections=0,
            recent_events=[],
        )
        return ApiResponse.success(data=status.model_dump())

    @app.post("/api/risk/pause", response_model=ApiResponse, tags=["风控"])
    async def pause_trading(reason: str = "API手动暂停"):
        """暂停交易"""
        # v1占位：后续迭代调用风控管理器
        return ApiResponse.success(data={"status": "PAUSED", "reason": reason}, message="交易已暂停（v1占位）")

    @app.post("/api/risk/resume", response_model=ApiResponse, tags=["风控"])
    async def resume_trading():
        """恢复交易"""
        # v1占位
        return ApiResponse.success(data={"status": "NORMAL"}, message="交易已恢复（v1占位）")

    # ═══════════════════════════════════════════
    #  数据
    # ═══════════════════════════════════════════

    @app.get("/api/data/quality", response_model=ApiResponse[DataQualityResponse],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality(
        symbol: str = Query("", description="标的代码"),
        data_type: str = Query("bar", description="数据类型: bar/tick"),
    ):
        """获取数据质量报告（任务16：真实总览，兼容 DataQualityResponse 结构）"""
        try:
            explorer = _get_explorer()
            overview = explorer.get_overview(limit=10)
            metrics = overview.get("metrics", {})
            total = metrics.get("total_records") or 0
            bad = metrics.get("dirty_count") or 0
            dirty_summary = overview.get("dirty_summary", {})
            # status：质量等级指标在 collector 中为数值（BuiltinMetrics.QUALITY_GRADE_MAP），
            # 需映射回字符串；字符串/None 直接兜底。
            status_raw = metrics.get("quality_grade")
            if isinstance(status_raw, str) and status_raw:
                status = status_raw
            elif status_raw is not None:
                from myquant.monitoring.metrics_collector import BuiltinMetrics as _B
                _rev = {v: k for k, v in _B.QUALITY_GRADE_MAP.items()}
                status = _rev.get(int(status_raw), "good")
            else:
                status = "good"
            quality = DataQualityResponse(
                symbol=symbol or "",
                data_type=data_type,
                total_records=int(total),
                valid_records=int(total - bad),
                bad_records=int(bad),
                quality_rate=round(1.0 - (metrics.get("dirty_ratio") or 0.0), 6),
                status=status,
                bad_types=dirty_summary.get("by_type", {}),
            )
            return ApiResponse.success(data=quality.model_dump(), message="数据质量报告")
        except Exception as e:
            logger.error(f"[api] 数据质量报告失败: {e}")
            return ApiResponse.error(code=500, message="数据质量报告失败")

    @app.post("/api/data/clean", response_model=ApiResponse, tags=["数据"])
    async def trigger_data_clean(
        symbol: str = Query(..., description="标的代码"),
        start_date: str = Query(..., description="开始日期"),
        end_date: str = Query(..., description="结束日期"),
    ):
        """触发数据清洗"""
        # v1占位：后续迭代调用数据清洗流水线
        return ApiResponse.success(
            data={"symbol": symbol, "start_date": start_date, "end_date": end_date},
            message="数据清洗任务已提交（v1占位）",
        )

    # ═══════════════════════════════════════════
    #  订单（TODO占位，后续迭代增强）
    # ═══════════════════════════════════════════

    @app.post("/api/order", response_model=ApiResponse, tags=["订单"])
    async def place_order(request: OrderRequest):
        """下单（TODO占位，后续迭代对接券商）"""
        # v1占位：后续迭代调用实盘引擎/券商
        order_id = f"ord_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        result = OrderResponse(
            order_id=order_id,
            status="submitted",
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            filled_quantity=0,
            message="订单已提交（v1占位，未实际对接券商）",
        )
        return ApiResponse.success(data=result.model_dump())

    @app.post("/api/order/{order_id}/cancel", response_model=ApiResponse, tags=["订单"])
    async def cancel_order(order_id: str):
        """撤单（TODO占位）"""
        return ApiResponse.success(data={"order_id": order_id, "status": "cancelled"}, message="撤单指令已发送（v1占位）")

    # ═══════════════════════════════════════════
    #  资金分配
    # ═══════════════════════════════════════════

    @app.post("/api/allocation/calculate", response_model=ApiResponse, tags=["资金分配"])
    async def calculate_allocation(
        method: str = Query("equal_weight", description="分配方法: equal_weight/risk_parity"),
        assets: List[str] = Query(..., description="资产列表"),
        total_capital: float = Query(100000.0, description="总资金"),
    ):
        """计算资金分配"""
        # v1占位：后续迭代调用资金分配器
        n = len(assets)
        weights = {a: 1.0 / n for a in assets} if n > 0 else {}
        amounts = {a: w * total_capital for a, w in weights.items()}
        return ApiResponse.success(data={
            "method": method,
            "weights": weights,
            "amounts": amounts,
            "total_capital": total_capital,
        }, message=f"资金分配计算完成（v1占位，方法={method}）")

    # ═══════════════════════════════════════════
    #  数据质量查询接口（任务16）
    # ═══════════════════════════════════════════

    @app.get("/api/data/quality/overview", response_model=ApiResponse[QualityOverviewData],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality_overview():
        """
        数据质量总览

        返回：实时指标 + 清洗运行总数 + 最近一次运行 + 质量等级分布 + 脏数据统计。
        【防未来函数】只返回数据库中已发生的清洗运行统计。
        """
        try:
            explorer = _get_explorer()
            overview = explorer.get_overview(limit=20)
            return ApiResponse.success(data=overview, message="数据质量总览")
        except Exception as e:
            logger.error(f"[api] 数据质量总览失败: {e}")
            return ApiResponse.error(code=500, message="数据质量总览失败")

    @app.get("/api/data/quality/runs", response_model=ApiResponse[CleanRunsData],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality_runs(
        symbol: str = Query("", description="标的代码"),
        data_type: str = Query("", description="数据类型: bar/tick"),
        status: str = Query("", description="状态: completed/failed/running"),
        source: str = Query("", description="来源: offline/live"),
        limit: int = Query(20, ge=1, le=100, description="返回条数"),
    ):
        """
        清洗运行列表（按 started_at 倒序）

        【防未来函数】仅返回已发生的清洗运行（任务13 end_time 严格过滤）。
        """
        try:
            explorer = _get_explorer()
            runs = explorer.get_clean_runs(
                symbol=symbol or None,
                data_type=data_type or None,
                status=status or None,
                source=source or None,
                limit=limit,
            )
            total = explorer.count_clean_runs(
                symbol=symbol or None,
                data_type=data_type or None,
                status=status or None,
                source=source or None,
            )
            return ApiResponse.success(data={
                "total": total,
                "limit": limit,
                "runs": runs,
            }, message="清洗运行列表")
        except Exception as e:
            logger.error(f"[api] 清洗运行查询失败: {e}")
            return ApiResponse.error(code=500, message="清洗运行查询失败")

    @app.get("/api/data/quality/runs/{run_id}", response_model=ApiResponse[CleanRunInfo],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality_run_detail(run_id: str):
        """清洗运行详情（不存在返回404）"""
        try:
            explorer = _get_explorer()
            detail = explorer.get_run_detail(run_id)
            if detail is None:
                raise HTTPException(status_code=404, detail=f"清洗运行不存在: {run_id}")
            return ApiResponse.success(data=detail, message="清洗运行详情")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[api] 清洗运行详情失败: {e}")
            return ApiResponse.error(code=500, message="清洗运行详情失败")

    @app.get("/api/data/quality/dirty", response_model=ApiResponse[DirtyData],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality_dirty(
        run_id: str = Query("", description="清洗运行ID"),
        symbol: str = Query("", description="标的代码"),
        data_type: str = Query("", description="数据类型"),
        include_detail: bool = Query(True, description="是否含脏数据明细"),
    ):
        """
        脏数据统计（汇总 + 可选明细，含脏数据类型中文描述）

        【防未来函数】只统计数据库中已落盘的脏数据记录。
        """
        try:
            explorer = _get_explorer()
            summary = explorer.get_dirty_summary(
                run_id=run_id or None,
                symbol=symbol or None,
                data_type=data_type or None,
            )
            result: Dict[str, Any] = dict(summary)
            if include_detail:
                result["details"] = explorer.get_dirty_stats(
                    run_id=run_id or None,
                    symbol=symbol or None,
                    data_type=data_type or None,
                )
            return ApiResponse.success(data=result, message="脏数据统计")
        except Exception as e:
            logger.error(f"[api] 脏数据统计失败: {e}")
            return ApiResponse.error(code=500, message="脏数据统计失败")

    @app.get("/api/data/quality/metrics", response_model=ApiResponse[QualityMetricsInfo],
             response_model_exclude_none=True, tags=["数据质量"])
    async def get_data_quality_metrics():
        """
        实时质量指标（MetricsCollector 任务11）

        【防未来函数】只返回指标采集器中已采集的实时指标。
        """
        try:
            explorer = _get_explorer()
            return ApiResponse.success(data=explorer.get_metrics(), message="实时质量指标")
        except Exception as e:
            logger.error(f"[api] 质量指标获取失败: {e}")
            return ApiResponse.error(code=500, message="质量指标获取失败")

    # ═══════════════════════════════════════════
    #  监控：Prometheus 指标导出 + 告警（任务16）
    # ═══════════════════════════════════════════

    @app.get("/api/metrics", response_class=PlainTextResponse, tags=["监控"])
    async def metrics_export():
        """
        Prometheus 指标导出（供 Prometheus/Grafana 抓取）

        【防未来函数】只导出指标采集器中已采集的指标数据点。
        """
        try:
            collector = _get_collector()
            return collector.to_prometheus(include_timestamp=True)
        except Exception as e:
            logger.error(f"[api] Prometheus 指标导出失败: {e}")
            return PlainTextResponse("# ERROR metrics export failed\n", status_code=500)

    @app.get("/api/alerts", response_model=ApiResponse[AlertListData],
             response_model_exclude_none=True, tags=["监控"])
    async def get_alerts(
        scope: str = Query("all", description="all/unacknowledged/escalated"),
        level: str = Query("", description="级别: info/warning/error/critical"),
        category: str = Query("", description="告警分类"),
        limit: int = Query(50, ge=1, le=500, description="返回条数"),
    ):
        """
        告警列表（含统计）

        【防未来函数】只返回已发生的告警记录，不预测未来告警。
        """
        try:
            am = _get_alert_manager()
            if scope == "unacknowledged":
                alerts = am.get_unacknowledged(limit=limit)
            elif scope == "escalated":
                alerts = am.get_escalated(limit=limit)
            else:
                level_enum = None
                if level:
                    from myquant.core.constants import SeverityLevel
                    try:
                        level_enum = SeverityLevel(level)
                    except ValueError:
                        level_enum = None
                alerts = am.get_history(
                    level=level_enum,
                    category=category or None,
                    limit=limit,
                )
            summary = am.stats
            return ApiResponse.success(data={
                "scope": scope,
                "count": len(alerts),
                "alerts": [a.to_dict() for a in alerts],
                "summary": summary,
            }, message="告警列表")
        except Exception as e:
            logger.error(f"[api] 告警查询失败: {e}")
            return ApiResponse.error(code=500, message="告警查询失败")

    @app.post("/api/alerts/{alert_id}/ack", response_model=ApiResponse[AlertAckData],
              response_model_exclude_none=True, tags=["监控"])
    async def ack_alert(alert_id: str):
        """确认告警（不存在返回404）"""
        try:
            am = _get_alert_manager()
            ok = am.acknowledge(alert_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"告警不存在: {alert_id}")
            return ApiResponse.success(
                data={"alert_id": alert_id, "acknowledged": True}, message="告警已确认"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[api] 告警确认失败: {e}")
            return ApiResponse.error(code=500, message="告警确认失败")

    @app.post("/api/alerts/{alert_id}/resolve", response_model=ApiResponse[AlertResolveData],
              response_model_exclude_none=True, tags=["监控"])
    async def resolve_alert(alert_id: str):
        """解决告警（不存在返回404）"""
        try:
            am = _get_alert_manager()
            ok = am.resolve(alert_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"告警不存在: {alert_id}")
            return ApiResponse.success(
                data={"alert_id": alert_id, "resolved": True}, message="告警已解决"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[api] 告警解决失败: {e}")
            return ApiResponse.error(code=500, message="告警解决失败")

    logger.info(f"[api] FastAPI应用创建成功: {title} v{version}")
    return app


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def run_api(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """
    便捷函数：运行API服务

    Parameters
    ----------
    host : str
        监听地址
    port : int
        监听端口
    reload : bool
        是否启用热重载
    """
    if not FASTAPI_AVAILABLE:
        print("=" * 60)
        print("  ⚠️ FastAPI未安装")
        print("=" * 60)
        print("\n  API服务需要FastAPI依赖，请运行以下命令安装:")
        print("\n    pip install fastapi uvicorn")
        print("\n  安装后运行:")
        print("\n    python run_api.py")
        print("\n" + "=" * 60)
        return

    try:
        import uvicorn
    except ImportError:
        print("⚠️ uvicorn未安装，请运行: pip install uvicorn")
        return

    app = create_app()
    logger.info(f"[api] 启动API服务: {host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=reload)
