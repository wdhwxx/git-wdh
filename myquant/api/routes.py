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
from typing import Any, Dict, List, Set

from myquant.api.models.schemas import (
    AlertAckData,
    AlertListData,
    AlertResolveData,
    ApiResponse,
    BacktestMetrics,
    BacktestRequest,
    BacktestResponse,
    CleanRunInfo,
    CleanRunsData,
    DataQualityResponse,
    DirtyData,
    ErrorResponse,
    HealthResponse,
    LiveStatusResponse,
    OrderRequest,
    QualityMetricsInfo,
    QualityOverviewData,
    RiskStatusResponse,
    StrategyInfo,
    StrategyListResponse,
)
from myquant.core.logger import get_logger

logger = get_logger("api.routes")

# 检查fastapi是否安装
try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
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
#  A-11 告警详情脱敏（P0-7 §3；与 web/dashboard.py 规则同源，避免 API→Web 反向依赖）
# ═══════════════════════════════════════════════

_MASK_ACCOUNT_PATTERNS: tuple = ("account", "broker_account", "fund_account")
_MASK_CREDENTIAL_PATTERNS: tuple = (
    "api_key", "apikey", "secret", "token", "password", "passwd", "credential",
)
_MASK_BALANCE_PATTERNS: tuple = (
    "balance", "equity", "capital", "position_value", "available_cash",
    "total_assets", "frozen_cash", "pnl",
)


def _mask_alert_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    告警 details 脱敏（P0-7 §3：账号留后4位 / 凭证全掩码 / 资金明细聚合掩码）。

    【防未来】只脱敏当前已落库的告警详情，不预测。不修改入参。
    """
    out: Dict[str, Any] = {}
    if not isinstance(details, dict):
        return out
    for key, value in details.items():
        k = (str(key) or "").strip().lower()
        if any(p in k for p in _MASK_CREDENTIAL_PATTERNS):
            out[key] = "****"
        elif any(p in k for p in _MASK_ACCOUNT_PATTERNS):
            s = str(value) if value is not None else ""
            out[key] = ("****" + s[-4:]) if len(s) >= 4 else "****"
        elif any(p in k for p in _MASK_BALANCE_PATTERNS):
            out[key] = "****"
        elif isinstance(value, dict):
            out[key] = _mask_alert_details(value)
        else:
            out[key] = value
    return out


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
    enable_docs: bool = False,
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
    enable_docs : bool
        KP-P4-04: 是否开放 Swagger/ReDoc/OpenAPI 文档（默认 False 关闭，
        生产不暴露接口文档；文档端点也不在认证/限流豁免内）
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
        # KP-P1-07: 不静默降级——无 fastapi 时启动必须显式失败，避免『看似可用』的假成功
        raise RuntimeError("[api] fastapi未安装，API服务不可用。请运行: pip install fastapi uvicorn")

    # KP-P4-04: 生产默认关闭文档/OpenAPI 暴露（enable_docs=False 时 docs_url 等为 None）
    app = FastAPI(
        title=title,
        version=version,
        description="量化交易系统REST API，支持策略管理、回测、实盘、风控、数据、数据质量、监控等功能。",
        docs_url="/docs" if enable_docs else None,
        redoc_url="/redoc" if enable_docs else None,
        openapi_url="/openapi.json" if enable_docs else None,
    )

    # ── M4修复：服务获取统一走依赖注入层（api/dependencies/get_services.py）──
    # 外部注入的服务先注册到 ServiceContainer（保持注入优先能力），
    # 未注入时由依赖注入函数懒加载全局单例并回填容器。
    # 端点内的调用点（_get_explorer/_get_collector/_get_alert_manager）签名不变，
    # 6个数据质量端点+1个指标端点+3个告警端点自动切换为依赖注入驱动。
    from myquant.api.dependencies.get_services import (
        get_alert_manager as _di_get_alert_manager,
    )
    from myquant.api.dependencies.get_services import (
        get_data_quality_explorer as _di_get_explorer,
    )
    from myquant.api.dependencies.get_services import (
        get_metrics_collector as _di_get_collector,
    )
    from myquant.api.dependencies.get_services import (
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

    # ── A-16 RBAC：审计库解析（注入优先；未注入则守卫仅记 logger，不阻断）──
    def _get_rbac_guard() -> Any:
        """构建 RBAC 守卫：审计库取服务容器 "audit_log_store"（测试注入临时库）。"""
        from myquant.api.middleware.auth import RbacGuard
        store = _container.get("audit_log_store")
        return RbacGuard(audit_store=store)

    # ── B-2：时序库 / 对账器解析（注入优先；未注入则惰性默认实例，只读端点）──
    def _get_history_store() -> Any:
        """获取指标时序库（服务容器 "metrics_history_store" 注入优先，否则默认库）。"""
        store = _container.get("metrics_history_store")
        if store is None:
            from myquant.monitoring.metrics_history import MetricsHistoryStore
            store = MetricsHistoryStore()
            _container.register("metrics_history_store", store)
        return store

    def _get_reconciler() -> Any:
        """获取账户对账器（容器 "account_reconciler" 注入优先，否则全局惰性单例）。"""
        recon = _container.get("account_reconciler")
        if recon is None:
            from myquant.monitoring.account_reconciler import get_account_reconciler
            recon = get_account_reconciler()
            _container.register("account_reconciler", recon)
        return recon

    # CORS配置（C1修复：通配符 * 与 allow_credentials=True 是无效组合，
    # 改为环境变量白名单；KP-P1-05：缺省不开放——未设置/为空则不挂 CORS 中间件）
    _cors_raw = os.environ.get("QUANT_CORS_ORIGINS", "")
    cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()]
    if cors_origins:
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
    from myquant.api.middleware.rate_limit import create_rate_limiter

    _auth = APIKeyAuth(enabled=enable_auth)
    _limiter = create_rate_limiter(
        max_requests=int(os.environ.get("QUANT_RATE_LIMIT_MAX", "100")),
        window_seconds=int(os.environ.get("QUANT_RATE_LIMIT_WINDOW", "60")),
    )
    # 豁免路径：仅健康检查（监控探活）
    # KP-P4-04: 移除 /docs /redoc /openapi.json——API 文档不再匿名豁免
    # L7修复: 移除 /health——M10 后真实路由为 /api/health，/health 已 404（死配置）
    _exempt_paths = {"/api/health"}

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
        # 未注入的组件模块标记 unknown（保持清单完整，不谎报健康）
        # KP-P5-02: unknown 计入 degraded——探针未注册即视为降级，不再恒 healthy
        for name in ("data", "strategy", "backtest", "live", "risk", "allocation"):
            if name not in modules:
                modules[name] = "unknown"
                degraded = True
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
        """运行回测（KP-P1-02：接线真实回测引擎，不再硬编码绩效常量）"""
        global _backtest_counter
        _backtest_counter += 1
        backtest_id = f"bt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_backtest_counter}"

        try:
            from myquant.backtest.batch_backtest import STRATEGY_REGISTRY as _STRAT_REG
            from myquant.backtest.engine import run_backtest as _run_backtest_engine
            from myquant.data_access.provider import HistoricalDataProvider
            from myquant.models.backtest import BacktestConfig

            strategy_cls = _STRAT_REG.get(request.strategy_name)
            if strategy_cls is None:
                return JSONResponse(
                    status_code=501,
                    content={"success": False, "code": 501,
                             "message": f"策略未注册: {request.strategy_name}", "data": {}},
                )
            if not request.symbol or not request.start_date or not request.end_date:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "code": 422,
                             "message": "缺少 symbol/start_date/end_date", "data": {}},
                )

            strategy = strategy_cls(**request.params)
            provider = HistoricalDataProvider()
            config = BacktestConfig(
                strategy_name=request.strategy_name,
                symbols=[request.symbol],
                start_date=request.start_date,
                end_date=request.end_date,
                initial_capital=request.initial_capital,
            )
            bt_result = _run_backtest_engine(config, strategy, data_provider=provider, mode="auto")
            if bt_result is None or not bt_result.is_success:
                return JSONResponse(
                    status_code=501,
                    content={"success": False, "code": 501,
                             "message": f"回测未成功（数据不可用或执行失败）: {getattr(bt_result, 'status', 'unknown')}", "data": {}},
                )

            m = bt_result.metrics
            metrics = BacktestMetrics(
                total_return=float(getattr(m, "total_return", 0.0)),
                annual_return=float(getattr(m, "annualized_return", 0.0)),
                sharpe_ratio=float(getattr(m, "sharpe_ratio", 0.0)),
                max_drawdown=float(getattr(m, "max_drawdown", 0.0)),
                win_rate=float(getattr(m, "win_rate", 0.0)),
                total_trades=int(getattr(m, "total_trades", 0)),
                calmar_ratio=float(getattr(m, "calmar_ratio", 0.0)),
                sortino_ratio=float(getattr(m, "sortino_ratio", 0.0)),
            )
            result = BacktestResponse(
                backtest_id=backtest_id,
                status="completed",
                metrics=metrics,
                equity_curve=getattr(bt_result, "equity_curve", None) or [],
                trades=getattr(bt_result, "trades", None) or [],
                message=f"回测完成（策略={request.strategy_name}, 标的={request.symbol}）",
            )
            _backtest_results[backtest_id] = result
            return ApiResponse.success(data=result.model_dump(), message="回测完成")
        except Exception as e:
            logger.error(f"[api] 回测执行失败: {e}")
            return JSONResponse(
                status_code=501,
                content={"success": False, "code": 501, "message": f"回测执行失败: {e}", "data": {}},
            )

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
        """启动实盘（KP-P1-01：未接真实引擎 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "实盘启动未接线：需接入真实引擎后可用", "data": {}},
        )

    @app.post("/api/live/stop", response_model=ApiResponse, tags=["实盘"])
    async def stop_live():
        """停止实盘（KP-P1-01：未接真实引擎 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "实盘停止未接线：需接入真实引擎后可用", "data": {}},
        )

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
        """暂停交易（KP-P1-01：未接真实风控 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "交易暂停未接线：需接入风控管理器后可用", "data": {}},
        )

    @app.post("/api/risk/resume", response_model=ApiResponse, tags=["风控"])
    async def resume_trading():
        """恢复交易（KP-P1-01：未接真实风控 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "交易恢复未接线：需接入风控管理器后可用", "data": {}},
        )

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
        """下单（KP-P1-01：未接真实券商 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "下单未接线：需接入券商后可用", "data": {}},
        )

    @app.post("/api/order/{order_id}/cancel", response_model=ApiResponse, tags=["订单"])
    async def cancel_order(order_id: str):
        """撤单（KP-P1-01：未接真实券商 → 501，不伪造成功）"""
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501, "message": "撤单未接线：需接入券商后可用", "data": {}},
        )

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
            alert_rows = []
            for a in alerts:
                d = a.to_dict()
                d["details"] = _mask_alert_details(d.get("details"))  # A-11/P0-7 脱敏
                alert_rows.append(d)
            return ApiResponse.success(data={
                "scope": scope,
                "count": len(alert_rows),
                "alerts": alert_rows,
                "summary": summary,
            }, message="告警列表")
        except Exception as e:
            logger.error(f"[api] 告警查询失败: {e}")
            return ApiResponse.error(code=500, message="告警查询失败")

    @app.post("/api/alerts/{alert_id}/ack", response_model=ApiResponse[AlertAckData],
              response_model_exclude_none=True, tags=["监控"])
    async def ack_alert(
        alert_id: str,
        x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
        x_user: Optional[str] = Header(None, alias="X-User"),
    ):
        """
        确认告警（A-16 RBAC：需 ack_alerts 权限；越权 403 且留审计 deny 痕迹）。

        不传 X-User-Role → 默认 admin（单人系统降级，保持既有调用方可用）。
        不存在返回404。
        """
        try:
            guard = _get_rbac_guard()
            allowed, reason = guard.check(
                role=x_user_role, permission="ack_alerts",
                actor=x_user or "anonymous", resource=f"alert:{alert_id}",
            )
            if not allowed:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponse(
                        error="Forbidden", message=f"无权确认告警：{reason}",
                    ).model_dump(),
                )
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
    async def resolve_alert(
        alert_id: str,
        x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
        x_user: Optional[str] = Header(None, alias="X-User"),
    ):
        """
        解决告警（A-16 RBAC：需 resolve_alerts 权限；越权 403 且留审计 deny 痕迹）。

        不传 X-User-Role → 默认 admin（单人系统降级，保持既有调用方可用）。
        不存在返回404。
        """
        try:
            guard = _get_rbac_guard()
            allowed, reason = guard.check(
                role=x_user_role, permission="resolve_alerts",
                actor=x_user or "anonymous", resource=f"alert:{alert_id}",
            )
            if not allowed:
                return JSONResponse(
                    status_code=403,
                    content=ErrorResponse(
                        error="Forbidden", message=f"无权解决告警：{reason}",
                    ).model_dump(),
                )
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

    # ═══════════════════════════════════════════
    #  A-11 监控中心总览（只读：健康两级 + 告警 + 指标卡片 + 交易连续状态）
    # ═══════════════════════════════════════════

    @app.get("/api/monitor/overview", response_model=ApiResponse, tags=["监控"])
    async def monitor_overview():
        """
        监控中心总览（A-11：看状态→查告警 只读视图）。

        返回：系统+组件两级健康（复用 /api/health 注册检查器）、告警统计与最近
        告警（details 脱敏）、实盘指标卡片、交易连续状态（行情活性/引擎心跳/
        券商连接；指标缺失则 unknown，不伪造）。
        【防未来】只聚合当前已发生状态，不预测。
        """
        try:
            # ── 健康总览（系统+组件两级）──
            checkers: Dict[str, Any] = getattr(app.state, "health_checkers", {}) or {}
            components: List[Dict[str, Any]] = []
            degraded = False
            for name, checker in checkers.items():
                try:
                    result = checker() if callable(checker) else checker
                    if result is True or result == "healthy":
                        st = "healthy"
                    elif result is False or result == "down":
                        st = "down"
                        degraded = True
                    else:
                        st = "degraded"
                        degraded = True
                except Exception as e:
                    st = "down"
                    degraded = True
                    logger.error(f"[api] 监控健康检查器 {name} 异常: {e}")
                components.append({"name": name, "status": st})
            system_status = "degraded" if degraded else ("healthy" if components else "unknown")

            # ── 告警统计 + 最近告警（脱敏）──
            am = _get_alert_manager()
            recent = am.get_unacknowledged(limit=10)
            recent_rows = []
            for a in recent:
                d = a.to_dict()
                d["details"] = _mask_alert_details(d.get("details"))
                recent_rows.append(d)
            try:
                alert_summary = am.stats
            except Exception:
                alert_summary = {}

            # ── 实盘指标卡片 ──
            collector = _get_collector()
            cards: List[Dict[str, Any]] = []
            try:
                for m in collector.get_all():
                    td = getattr(m, "to_dict", None)
                    d = td() if callable(td) else (m if isinstance(m, dict) else {})
                    cards.append({
                        "name": d.get("name", ""),
                        "value": d.get("value", 0.0),
                    })
            except Exception as e:
                logger.error(f"[api] 监控指标卡片采集失败: {e}")
            cards = cards[:12]

            # ── 交易连续状态（指标缺失→unknown，不伪造）──
            def _continuity(hints: tuple) -> Dict[str, Any]:
                try:
                    for m in collector.get_all():
                        td = getattr(m, "to_dict", None)
                        d = td() if callable(td) else (m if isinstance(m, dict) else {})
                        name = str(d.get("name", "")).lower()
                        if any(h in name for h in hints):
                            return {"state": "ok", "metric": d.get("name"),
                                    "value": float(d.get("value", 0.0))}
                except Exception as e:
                    logger.error(f"[api] 交易连续状态采集失败: {e}")
                return {"state": "unknown", "metric": None, "value": 0.0}

            continuity = {
                "market_activity": _continuity(("market_activity", "quote_recv", "tick_recv")),
                "engine_heartbeat": _continuity(("engine_heartbeat", "scheduler_heartbeat")),
                "broker_connection": _continuity(("broker_connection", "broker_connected")),
            }

            return ApiResponse.success(data={
                "health": {
                    "system": {"status": system_status, "checks_count": len(components)},
                    "components": components,
                },
                "alerts": {"count": len(recent_rows), "alerts": recent_rows,
                           "summary": alert_summary},
                "metric_cards": cards,
                "trading_continuity": continuity,
            }, message="监控中心总览")
        except Exception as e:
            logger.error(f"[api] 监控中心总览失败: {e}")
            return ApiResponse.error(code=500, message="监控中心总览失败")

    # ═══════════════════════════════════════════
    #  A-16 受控写操作占位（改阈值 / 复位熔断）：先 RBAC 鉴权，未接线 501
    # ═══════════════════════════════════════════

    @app.post("/api/monitor/config/threshold", response_model=ApiResponse, tags=["监控"])
    async def update_monitor_threshold(
        x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
        x_user: Optional[str] = Header(None, alias="X-User"),
    ):
        """
        修改监控阈值（A-16 RBAC：需 modify_config 权限，仅 admin）。

        阈值唯一真源为 config/settings.yaml monitoring 块（R-04）；本端点尚未
        接线，鉴权通过后返回 501（不伪造成功）。越权 → 403 + 审计 deny。
        """
        guard = _get_rbac_guard()
        allowed, reason = guard.check(
            role=x_user_role, permission="modify_config",
            actor=x_user or "anonymous", resource="monitor:config:threshold",
        )
        if not allowed:
            return JSONResponse(
                status_code=403,
                content=ErrorResponse(
                    error="Forbidden", message=f"无权修改阈值：{reason}",
                ).model_dump(),
            )
        return JSONResponse(
            status_code=501,
            content={"success": False, "code": 501,
                     "message": "阈值修改未接线：需对接 settings.yaml monitoring 块后可用",
                     "data": {}},
        )

    @app.post("/api/monitor/circuit/reset", response_model=ApiResponse, tags=["监控"])
    async def reset_circuit_breaker(
        x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
        x_user: Optional[str] = Header(None, alias="X-User"),
    ):
        """
        复位熔断（A-16 RBAC / B-6 接线：需 reset_circuit_breaker 权限，仅 admin）。

        端点先鉴权（越权 403 + 审计 deny）；鉴权通过后调用
        CircuitBreaker.reset(actor, reason) 恢复开仓能力并返回真实结果。
        熔断实例优先取服务容器 "circuit_breaker"（实盘进程注入带真实
        broker/risk_manager 的实例），缺失则懒加载全局单例（复位仍可用）。
        """
        guard = _get_rbac_guard()
        allowed, reason = guard.check(
            role=x_user_role, permission="reset_circuit_breaker",
            actor=x_user or "anonymous", resource="monitor:circuit:reset",
        )
        if not allowed:
            return JSONResponse(
                status_code=403,
                content=ErrorResponse(
                    error="Forbidden", message=f"无权复位熔断：{reason}",
                ).model_dump(),
            )
        try:
            from myquant.monitoring.circuit_breaker import (
                get_circuit_breaker,
            )
            cb = _container.get("circuit_breaker")
            if cb is None:
                cb = get_circuit_breaker()
                _container.register("circuit_breaker", cb)
            # 审计库取服务容器注入值（与 RBAC guard 同源），保证复位留痕落同一库
            cb_audit = getattr(cb, "_audit", None)
            if cb_audit is None:
                store = _container.get("audit_log_store")
                if store is not None:
                    cb.attach_audit_store(store)
            reset_result = cb.reset(
                actor=x_user or "anonymous",
                reason="人工复位（经 API RBAC 鉴权）",
            )
            return ApiResponse.success(
                data=reset_result,
                message=("熔断已复位，开仓恢复" if reset_result.get("was_tripped")
                         else "熔断未处于触发状态，已确认复位"),
            )
        except Exception as e:
            logger.error(f"[api] 熔断复位失败: {e}")
            return ApiResponse.error(code=500, message="熔断复位失败")

    # ═══════════════════════════════════════════
    #  B-2：PnL 快照 + 当日权益曲线（只读；防脏门：冻结期间 PnL 不更新）
    # ═══════════════════════════════════════════

    @app.get("/api/monitor/pnl", response_model=ApiResponse, tags=["监控"])
    async def monitor_pnl():
        """
        最新 PnL 快照 + 对账状态（B-2，只读视图）。

        返回时序库中最新一点的总资产/可用/持仓市值/已实现/未实现 PnL，以及
        AccountReconciler.to_dict（consistent/frozen/last_diff）——冻结状态下
        PnL 不更新（防脏门），看板据此提示"对账冻结中"。无数据时各 PnL 项为
        None（不伪造）。【防未来】只回放已落库历史点。
        """
        try:
            from myquant.monitoring.metrics_collector import get_latest_pnl_snapshot
            store = _get_history_store()
            recon = _get_reconciler()
            data = get_latest_pnl_snapshot(store=store, reconciler=recon)
            return ApiResponse.success(data=data, message="最新 PnL 快照")
        except Exception as e:
            logger.error(f"[api] PnL 快照查询失败: {e}")
            return ApiResponse.error(code=500, message="PnL 快照查询失败")

    @app.get("/api/monitor/equity_curve", response_model=ApiResponse, tags=["监控"])
    async def monitor_equity_curve():
        """
        当日权益曲线回放（B-2，只读视图）。

        基于 metrics_history 当日序列（local 当日零点 → 当前时刻，升序）回放
        live_total_assets 曲线；敏感值经 query() 解密回读。空序列返回空列表（不
        伪造点位）。【防未来】右闭取当前时刻，只回放当日已落库点。
        """
        try:
            from myquant.monitoring.metrics_collector import replay_intraday_equity_curve
            store = _get_history_store()
            points = replay_intraday_equity_curve(store=store)
            return ApiResponse.success(
                data={"points": points, "count": len(points)},
                message="当日权益曲线",
            )
        except Exception as e:
            logger.error(f"[api] 当日权益曲线回放失败: {e}")
            return ApiResponse.error(code=500, message="当日权益曲线回放失败")

    @app.get("/api/monitor/cockpit", response_model=ApiResponse, tags=["监控"])
    async def monitor_cockpit():
        """
        业务驾驶舱四视图（B-8，只读）。

        四视图：①对账状态（AccountReconciler.to_dict）②资金 PnL（B-2 快照）
        ③限额使用（LimitUsageMonitor 配置 + collector 使用率 GAUGE）④执行质量
        （ExecutionStatsMonitor.summary：滑点/拒单/延迟）。数据全部经
        MonitorCenterDataProvider.build_cockpit_payload 聚合，与监控中心看板同源，
        保证"数据与 API 一致"。无数据视图 graceful 返回空（不伪造）。
        """
        try:
            from myquant.web.dashboard import MonitorCenterDataProvider
            provider = MonitorCenterDataProvider(
                metrics_collector=_get_collector(),
                metrics_history_store=_get_history_store(),
                account_reconciler=_get_reconciler(),
                limit_usage_monitor=_container.get("limit_usage_monitor"),
                execution_stats_monitor=_container.get("execution_stats_monitor"),
            )
            data = provider.build_cockpit_payload()
            return ApiResponse.success(data=data, message="业务驾驶舱四视图")
        except Exception as e:
            logger.error(f"[api] 业务驾驶舱聚合失败: {e}")
            return ApiResponse.error(code=500, message="业务驾驶舱聚合失败")

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
