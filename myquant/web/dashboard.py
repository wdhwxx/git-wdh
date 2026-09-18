# 文件名：myquant/web/dashboard.py
"""
dashboard.py - Web看板（Streamlit）
=====================================
量化交易系统Web看板，基于Streamlit。

v1实现范围（基础骨架 + 数据质量探查）:
  - 侧边栏导航（首页/策略/回测/实盘/风控/数据质量/数据/关于）
  - 首页：系统概览、模块状态卡片、快速入口
  - 策略页：策略列表占位、策略参数配置占位
  - 回测页：回测配置表单、回测结果占位
  - 实盘页：实盘状态、账户概览、持仓列表占位
  - 风控页：风控状态、规则配置、事件日志占位
  - 数据质量页：数据质量探查（任务15，实时指标+清洗运行+等级分布+脏数据统计+运行详情）
  - 数据页：数据质量报告、数据源管理占位
  - 与核心模块集成接口（注入策略/回测/实盘/风控/数据管理器/数据质量探查器/数据库连接）
  - Streamlit未安装时的友好提示

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（实时行情刷新、实盘控制面板全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — Web只展示当前已知数据，不预测未来

防未来函数说明:
  - Web看板只展示当前已知的账户/持仓/行情/回测结果数据
  - 不展示任何未来数据或预测数据
  - 回测结果展示严格按时间顺序，不泄露未来信息
  - 实盘面板只展示当前实时数据，不预判未来走势
  - 数据质量探查只展示数据库/指标采集器中已发生的数据
  - 清洗运行列表的 end_time 严格过滤由 CleanDataQueries（任务13）保证

TODO(后续迭代):
  - K线图交互（plotly/streamlit-echarts）
  - 回测结果可视化（净值曲线/回撤图/绩效指标）
  - 实盘实时行情刷新（streamlit-autorefresh）
  - 策略参数动态配置
  - 下单面板（实盘模式）
  - 风控仪表盘（实时风控状态）
  - 多页面应用（streamlit multipage）
  - 用户认证/权限管理
  - 深色主题

依赖: streamlit（可选）, typing, datetime, myquant.core.logger
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.monitoring.data_quality_explorer import DataQualityExplorer  # #S2修复：从monitoring底层导入，消除API依赖Web的架构倒置

logger = get_logger("web.dashboard")

# 检查streamlit是否安装
try:
    import streamlit as st
    STREAMLIT_AVAILABLE = True
except ImportError:
    STREAMLIT_AVAILABLE = False
    logger.warning("[web] streamlit未安装，Web看板不可用。请运行: pip install streamlit")


# ═══════════════════════════════════════════════
#  页面名称枚举
# ═══════════════════════════════════════════════

PAGES = [
    "🏠 首页",
    "📊 策略",
    "📈 回测",
    "💹 实盘",
    "🛡️ 风控",
    "🧹 数据质量",
    "💾 数据",
    "ℹ️ 关于",
]


# ═══════════════════════════════════════════════
#  DataQualityExplorer 已迁移到 myquant.monitoring.data_quality_explorer
#  （#S2修复：消除API层依赖Web层的架构倒置）
#  本文件通过顶部 from myquant.monitoring.data_quality_explorer import DataQualityExplorer 保持向后兼容
# ═══════════════════════════════════════════════


# ═══════════════════════════════════════════════
#  Web看板
# ═══════════════════════════════════════════════

class Dashboard:
    """
    量化交易系统Web看板（Streamlit）

    v1基础骨架：侧边栏导航 + 7个页面占位。

    使用方式:
        # 方式1：通过入口脚本运行
        # streamlit run run_web.py

        # 方式2：在Python中调用
        from myquant.web import Dashboard
        dashboard = Dashboard()
        dashboard.run()  # 需要在streamlit上下文中运行

    与核心模块集成（后续迭代）:
        dashboard.set_strategy_manager(strategy_mgr)
        dashboard.set_backtest_engine(backtest_engine)
        dashboard.set_live_engine(live_engine)
        dashboard.set_risk_manager(risk_mgr)
        dashboard.set_data_manager(data_mgr)
    """

    def __init__(self, title: str = "Pythonwdhmm 量化交易系统"):
        """
        初始化Web看板

        Parameters
        ----------
        title : str
            看板标题
        """
        self.title = title

        # 核心模块引用（后续迭代注入）
        self._strategy_manager: Optional[Any] = None
        self._backtest_engine: Optional[Any] = None
        self._live_engine: Optional[Any] = None
        self._risk_manager: Optional[Any] = None
        self._data_manager: Optional[Any] = None
        self._data_quality_explorer: Optional[Any] = None  # 任务15：数据质量探查器
        self._db_connection: Optional[Any] = None          # 任务15：数据库连接

        # 页面渲染函数注册表
        self._page_renderers: Dict[str, Callable] = {
            "🏠 首页": self._render_home,
            "📊 策略": self._render_strategy,
            "📈 回测": self._render_backtest,
            "💹 实盘": self._render_live,
            "🛡️ 风控": self._render_risk,
            "🧹 数据质量": self._render_data_quality,
            "💾 数据": self._render_data,
            "ℹ️ 关于": self._render_about,
        }

        logger.info(f"[web] Web看板初始化: {title}")

    # ═══════════════════════════════════════════
    #  核心模块注入（后续迭代）
    # ═══════════════════════════════════════════

    def set_strategy_manager(self, manager: Any) -> None:
        """注入策略管理器"""
        self._strategy_manager = manager
        logger.info("[web] 策略管理器已注入")

    def set_backtest_engine(self, engine: Any) -> None:
        """注入回测引擎"""
        self._backtest_engine = engine
        logger.info("[web] 回测引擎已注入")

    def set_live_engine(self, engine: Any) -> None:
        """注入实盘引擎"""
        self._live_engine = engine
        logger.info("[web] 实盘引擎已注入")

    def set_risk_manager(self, manager: Any) -> None:
        """注入风控管理器"""
        self._risk_manager = manager
        logger.info("[web] 风控管理器已注入")

    def set_data_manager(self, manager: Any) -> None:
        """注入数据管理器"""
        self._data_manager = manager
        logger.info("[web] 数据管理器已注入")

    def set_data_quality_explorer(self, explorer: Any) -> None:
        """注入数据质量探查器（任务15新增）"""
        self._data_quality_explorer = explorer
        logger.info("[web] 数据质量探查器已注入")

    def set_database_connection(self, connection: Any) -> None:
        """注入数据库连接（任务15新增，未注入时自动 fallback 到全局单例）"""
        self._db_connection = connection
        logger.info("[web] 数据库连接已注入")

    def _get_explorer(self) -> "DataQualityExplorer":
        """
        获取数据质量探查器（注入优先，否则自动构建）

        自动构建使用全局数据库连接（get_connection）与全局指标采集器（get_metrics_collector）。
        """
        if self._data_quality_explorer is None:
            self._data_quality_explorer = DataQualityExplorer(connection=self._db_connection)
            logger.info("[web] 数据质量探查器自动构建完成")
        return self._data_quality_explorer

    # ═══════════════════════════════════════════
    #  运行
    # ═══════════════════════════════════════════

    def run(self) -> None:
        """
        运行Web看板（需要在streamlit上下文中调用）

        通常通过入口脚本运行:
            streamlit run run_web.py
        """
        if not STREAMLIT_AVAILABLE:
            self._render_no_streamlit()
            return

        # 页面配置
        st.set_page_config(
            page_title=self.title,
            page_icon="📊",
            layout="wide",
            initial_sidebar_state="expanded",
        )

        # 侧边栏导航
        with st.sidebar:
            st.markdown(f"## 📊 {self.title}")
            st.markdown("---")
            selected_page = st.radio("导航", PAGES, label_visibility="collapsed")
            st.markdown("---")
            st.caption(f"v1 基础骨架 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # 渲染选中页面
        renderer = self._page_renderers.get(selected_page, self._render_home)
        renderer()

    # ═══════════════════════════════════════════
    #  页面：首页
    # ═══════════════════════════════════════════

    def _render_home(self) -> None:
        """渲染首页"""
        st.title("🏠 系统概览")
        st.markdown("---")

        # 模块状态卡片
        st.subheader("📦 模块状态")
        col1, col2, col3, col4 = st.columns(4)

        modules = [
            ("数据层", "✅ 已完成", "raw/processed/bad"),
            ("策略层", "✅ 已完成", "BaseStrategy + 注册器"),
            ("回测引擎", "✅ 已完成", "processed真实数据链路"),
            ("实盘骨架", "✅ 已完成", "data_feed/portfolio/engine"),
        ]

        for i, (name, status, desc) in enumerate(modules):
            with [col1, col2, col3, col4][i]:
                st.metric(label=name, value=status, delta=desc)

        st.markdown("---")

        # 风控/资金分配状态
        st.subheader("🛡️ 风控 & 💰 资金分配")
        col5, col6 = st.columns(2)
        with col5:
            st.info("**风控模块** ✅ 已完成\n- 10类风控规则\n- 风控管理器\n- 风控守护v1骨架")
        with col6:
            st.info("**资金分配** ✅ 已完成\n- 等权分配\n- 风险平价（逆波动率+完整迭代）\n- 目标波动率TODO占位")

        st.markdown("---")

        # 快速入口
        st.subheader("🚀 快速入口")
        col7, col8, col9, col10 = st.columns(4)
        with col7:
            if st.button("📊 策略管理", use_container_width=True):
                st.switch_page("__main__") if hasattr(st, "switch_page") else None
        with col8:
            if st.button("📈 运行回测", use_container_width=True):
                pass
        with col9:
            if st.button("💹 启动实盘", use_container_width=True):
                pass
        with col10:
            if st.button("🛡️ 风控状态", use_container_width=True):
                pass

        st.markdown("---")
        st.caption("💡 提示：v1为基础骨架，各页面功能后续迭代实现。")

    # ═══════════════════════════════════════════
    #  页面：策略
    # ═══════════════════════════════════════════

    def _render_strategy(self) -> None:
        """渲染策略页"""
        st.title("📊 策略管理")
        st.markdown("---")

        # 策略列表
        st.subheader("📋 已注册策略")
        if self._strategy_manager:
            # 后续迭代：从策略管理器获取策略列表
            st.info("策略管理器已注入，后续迭代展示策略列表")
        else:
            st.warning("⚠️ 策略管理器未注入（v1占位）")
            st.markdown("""
            **示例策略（后续迭代）:**
            - MA交叉策略
            - RSI超买超卖策略
            - MACD策略
            - 布林带策略
            - 自定义策略
            """)

        st.markdown("---")

        # 策略参数配置
        st.subheader("⚙️ 策略参数配置")
        st.info("v1占位，后续迭代实现动态参数配置表单")

        col1, col2 = st.columns(2)
        with col1:
            st.text_input("策略名称", value="MA交叉策略", disabled=True)
            st.number_input("短期均线", value=5, disabled=True)
            st.number_input("长期均线", value=20, disabled=True)
        with col2:
            st.number_input("持仓周期", value=5, disabled=True)
            st.number_input("止损比例", value=0.05, disabled=True)
            st.number_input("止盈比例", value=0.10, disabled=True)

    # ═══════════════════════════════════════════
    #  页面：回测
    # ═══════════════════════════════════════════

    def _render_backtest(self) -> None:
        """渲染回测页"""
        st.title("📈 回测")
        st.markdown("---")

        # 回测配置
        st.subheader("⚙️ 回测配置")
        col1, col2 = st.columns(2)
        with col1:
            st.selectbox("策略", ["MA交叉策略", "RSI策略", "MACD策略"], disabled=True)
            st.text_input("标的", value="000001.SZ", disabled=True)
            st.date_input("开始日期", disabled=True)
        with col2:
            st.number_input("初始资金", value=100000, disabled=True)
            st.number_input("手续费率", value=0.0003, disabled=True)
            st.date_input("结束日期", disabled=True)

        st.button("▶️ 运行回测", disabled=True, use_container_width=True)

        st.markdown("---")

        # 回测结果
        st.subheader("📊 回测结果")
        if self._backtest_engine:
            st.info("回测引擎已注入，后续迭代展示回测结果")
        else:
            st.warning("⚠️ 回测引擎未注入（v1占位）")

        # 绩效指标占位
        col3, col4, col5, col6 = st.columns(4)
        with col3:
            st.metric("总收益率", "--", delta="待运行")
        with col4:
            st.metric("夏普比率", "--", delta="待运行")
        with col5:
            st.metric("最大回撤", "--", delta="待运行")
        with col6:
            st.metric("胜率", "--", delta="待运行")

        st.info("📈 净值曲线 / 回撤图 / 交易明细（后续迭代实现可视化）")

    # ═══════════════════════════════════════════
    #  页面：实盘
    # ═══════════════════════════════════════════

    def _render_live(self) -> None:
        """渲染实盘页"""
        st.title("💹 实盘交易")
        st.markdown("---")

        # 实盘状态
        st.subheader("📡 实盘状态")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("连接状态", "未连接", delta="v1占位")
        with col2:
            st.metric("运行状态", "未启动", delta="v1占位")
        with col3:
            st.metric("今日盈亏", "--", delta="待启动")

        st.markdown("---")

        # 账户概览
        st.subheader("💰 账户概览")
        if self._live_engine:
            st.info("实盘引擎已注入，后续迭代展示账户信息")
        else:
            st.warning("⚠️ 实盘引擎未注入（v1占位）")

        col4, col5, col6, col7 = st.columns(4)
        with col4:
            st.metric("总资产", "--")
        with col5:
            st.metric("可用资金", "--")
        with col6:
            st.metric("持仓市值", "--")
        with col7:
            st.metric("冻结资金", "--")

        st.markdown("---")

        # 持仓列表
        st.subheader("📋 持仓列表")
        st.info("v1占位，后续迭代实现持仓表格（标的/数量/成本/现价/盈亏）")

        st.markdown("---")

        # 手动下单
        st.subheader("📝 手动下单")
        st.warning("⚠️ v1占位，实盘下单功能后续迭代实现（需对接券商）")

    # ═══════════════════════════════════════════
    #  页面：风控
    # ═══════════════════════════════════════════

    def _render_risk(self) -> None:
        """渲染风控页"""
        st.title("🛡️ 风控管理")
        st.markdown("---")

        # 风控状态
        st.subheader("📊 风控状态")
        if self._risk_manager:
            st.info("风控管理器已注入，后续迭代展示实时风控状态")
        else:
            st.warning("⚠️ 风控管理器未注入（v1占位）")

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("风控状态", "NORMAL", delta="正常")
        with col2:
            st.metric("健康状态", "HEALTHY", delta="健康")
        with col3:
            st.metric("今日拦截", "0", delta="无")

        st.markdown("---")

        # 风控规则
        st.subheader("📋 风控规则（10类）")
        rules = [
            ("仓位限制", "单票最大仓位/最大持仓数"),
            ("单笔限额", "单笔订单最大金额"),
            ("最大回撤", "账户回撤超阈值禁止新开仓"),
            ("个股止损", "亏损超阈值禁止加仓"),
            ("个股止盈", "盈利超阈值禁止加仓（防追高）"),
            ("流控", "每日最大下单数/最小间隔"),
            ("涨跌停", "涨停不买/跌停不卖"),
            ("交易时段", "非交易时段禁止下单"),
            ("黑名单", "禁止交易指定标的"),
            ("资金可用性", "买入金额≤可用资金"),
        ]

        for i in range(0, len(rules), 2):
            col_a, col_b = st.columns(2)
            with col_a:
                name, desc = rules[i]
                st.checkbox(f"**{name}** — {desc}", value=True, disabled=True, key=f"rule_{i}")
            if i + 1 < len(rules):
                with col_b:
                    name, desc = rules[i + 1]
                    st.checkbox(f"**{name}** — {desc}", value=True, disabled=True, key=f"rule_{i+1}")

        st.markdown("---")

        # 风控事件日志
        st.subheader("📝 风控事件日志")
        st.info("v1占位，后续迭代实现风控事件日志表格")

    # ═══════════════════════════════════════════
    #  页面：数据质量探查（任务15新增）
    # ═══════════════════════════════════════════

    @staticmethod
    def _fmt_metric(value: Any, kind: str = "int") -> str:
        """
        格式化指标值供 metric 展示

        kind : int → 整型；ratio → 百分比；else → 原样字符串
        value 为 None 时返回 "--"（无数据）
        """
        if value is None:
            return "--"
        if kind == "ratio":
            return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else str(value)
        if kind == "int":
            return str(int(value)) if isinstance(value, (int, float)) else str(value)
        return str(value)

    def _render_data_quality(self) -> None:
        """渲染数据质量探查页（任务15）"""
        st.title("🧹 数据质量探查")
        st.markdown("---")

        # 探查器初始化（注入优先，否则自动构建）
        try:
            explorer = self._get_explorer()
        except Exception as e:
            st.error(f"⚠️ 数据质量探查器初始化失败: {e}")
            st.info("请确认 database/quant.db 存在且已初始化表结构（create_all_tables）")
            return

        # ── 区块1：实时质量指标（MetricsCollector 任务11） ──
        st.subheader("📡 实时质量指标")
        metrics = explorer.get_metrics()
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1:
            st.metric("脏数据占比", self._fmt_metric(metrics.get("dirty_ratio"), "ratio"))
        with col2:
            st.metric("脏数据条数", self._fmt_metric(metrics.get("dirty_count"), "int"))
        with col3:
            st.metric("总记录数", self._fmt_metric(metrics.get("total_records"), "int"))
        with col4:
            st.metric("流水线运行", self._fmt_metric(metrics.get("pipeline_runs"), "int"))
        with col5:
            st.metric("流水线失败", self._fmt_metric(metrics.get("pipeline_failures"), "int"))
        if metrics.get("quality_grade"):
            st.caption(f"当前质量等级: **{explorer.grade_desc(metrics['quality_grade'])}**"
                       f"（{metrics['quality_grade']}）")

        st.markdown("---")

        # ── 区块2：清洗运行总览（CleanDataQueries 任务13） ──
        st.subheader("🗂️ 清洗运行总览")
        col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns(5)
        with col_f1:
            f_symbol = st.text_input("标的", value="", key="dq_symbol")
        with col_f2:
            f_data_type = st.selectbox("数据类型", ["", "bar", "tick"], key="dq_dtype")
        with col_f3:
            f_status = st.selectbox("状态", ["", "completed", "failed", "running"], key="dq_status")
        with col_f4:
            f_source = st.selectbox("来源", ["", "offline", "live"], key="dq_source")
        with col_f5:
            f_limit = st.selectbox("条数", [10, 20, 50], index=1, key="dq_limit")

        runs = explorer.get_clean_runs(
            symbol=f_symbol or None,
            data_type=f_data_type or None,
            status=f_status or None,
            source=f_source or None,
            limit=f_limit,
        )
        total_runs = explorer.count_clean_runs(
            symbol=f_symbol or None,
            data_type=f_data_type or None,
            status=f_status or None,
            source=f_source or None,
        )
        st.caption(f"清洗运行总数: **{total_runs}** 次（当前筛选展示 {len(runs)} 条，按时间倒序）")

        if runs:
            rows = []
            for r in runs:
                vr = r.get("valid_rate")
                rows.append({
                    "时间": str(r.get("started_at", ""))[:19],
                    "标的": r.get("symbol", ""),
                    "类型": r.get("data_type", ""),
                    "状态": r.get("status", ""),
                    "质量": explorer.grade_desc(r.get("quality_grade")),
                    "严重": r.get("severity", ""),
                    "总数/有效/脏": f"{r.get('total', 0)}/{r.get('valid', 0)}/{r.get('bad', 0)}",
                    "合格率": f"{vr * 100:.1f}%" if vr is not None else "-",
                    "来源": r.get("source", ""),
                    "run_id": r.get("run_id", ""),
                })
            st.dataframe(rows, use_container_width=True)
        else:
            st.info("暂无清洗运行记录（可先在数据管理页运行清洗）")

        st.markdown("---")

        # ── 区块3：质量等级分布 ──
        st.subheader("🏅 质量等级分布（最近 %d 次运行）" % f_limit)
        dist = explorer.get_grade_distribution(limit=f_limit)
        if dist:
            st.dataframe([{
                "质量等级": d["grade_desc"],
                "等级编码": d["grade"],
                "运行次数": d["count"],
                "占比": f"{d['ratio'] * 100:.1f}%",
            } for d in dist], use_container_width=True)
        else:
            st.info("暂无质量等级数据")

        st.markdown("---")

        # ── 区块4：脏数据统计 ──
        st.subheader("🗑️ 脏数据统计")
        dirty_summary = explorer.get_dirty_summary()
        if dirty_summary.get("total_bad", 0) > 0:
            col_d1, col_d2, col_d3 = st.columns(3)
            with col_d1:
                st.metric("脏数据总条数", dirty_summary["total_bad"])
            with col_d2:
                st.metric("error 级", dirty_summary.get("error_count", 0))
            with col_d3:
                st.metric("warning 级", dirty_summary.get("warning_count", 0))
            st.caption("按脏数据类型分布（中文描述）:")
            type_rows = [
                {"脏数据类型": v["desc"], "类型编码": dt, "条数": v["bad_count"]}
                for dt, v in dirty_summary.get("by_type_desc", {}).items()
            ]
            type_rows.sort(key=lambda x: -x["条数"])
            st.dataframe(type_rows, use_container_width=True)
        else:
            st.info("暂无脏数据统计记录")

        st.markdown("---")

        # ── 区块5：清洗运行详情 ──
        st.subheader("🔍 清洗运行详情")
        run_ids = [r.get("run_id") for r in explorer.get_clean_runs(limit=50) if r.get("run_id")]
        if run_ids:
            selected = st.selectbox("选择 run_id 查看详情", run_ids, key="dq_run_select")
            detail = explorer.get_run_detail(selected)
            if detail:
                col_l, col_r = st.columns(2)
                with col_l:
                    st.markdown(f"**run_id**: `{detail.get('run_id')}`")
                    st.markdown(f"**标的**: {detail.get('symbol')}　**类型**: {detail.get('data_type')}")
                    st.markdown(f"**状态**: {detail.get('status')}　**质量**: "
                                f"{explorer.grade_desc(detail.get('quality_grade'))}")
                    st.markdown(f"**严重级别**: {detail.get('severity')}　**来源**: {detail.get('source')}")
                    vr = detail.get("valid_rate")
                    st.markdown(f"**统计**: 总数 {detail.get('total')} / 有效 {detail.get('valid')} / "
                                f"脏 {detail.get('bad')}　(合格率 "
                                f"{vr * 100:.1f}%" if vr is not None else "-" + ")")
                    st.markdown(f"**总耗时**: {detail.get('total_duration')} 秒")
                with col_r:
                    st.markdown(f"**processed**: `{detail.get('processed_file') or '-'}`")
                    st.markdown(f"**bad 文件**: `{detail.get('bad_file') or '-'}`")
                    st.markdown(f"**报告文件**: `{detail.get('report_file') or '-'}`")
                    st.markdown(f"**错误信息**: {detail.get('error') or '-'}")
                steps = detail.get("steps_executed")
                if steps:
                    st.markdown("**已执行步骤**: " + " → ".join(steps))
                durations = detail.get("step_durations")
                if durations:
                    st.markdown("**步骤耗时**:")
                    st.json(durations)
            else:
                st.warning("未查询到该 run_id 详情")
        else:
            st.info("暂无运行记录可选")

    # ═══════════════════════════════════════════
    #  页面：数据
    # ═══════════════════════════════════════════

    def _render_data(self) -> None:
        """渲染数据页"""
        st.title("💾 数据管理")
        st.markdown("---")

        # 数据源管理
        st.subheader("📡 数据源")
        sources = [
            ("AkShare", "✅ 已接入", "免费A股/期货/基金数据"),
            ("通达信本地", "✅ 已接入", "本地TDX数据（优先级最高）"),
            ("东方财富", "🔄 待接入", "Web数据源"),
            ("新浪财经", "🔄 待接入", "Web数据源"),
            ("券商实时", "⏳ 预留", "实盘行情推送"),
        ]

        for name, status, desc in sources:
            col1, col2, col3 = st.columns([2, 1, 3])
            with col1:
                st.markdown(f"**{name}**")
            with col2:
                st.markdown(status)
            with col3:
                st.caption(desc)

        st.markdown("---")

        # 数据质量报告
        st.subheader("📊 数据质量报告")
        st.info("🧹 详细数据质量探查（实时指标/清洗运行/等级分布/脏数据统计）请切换到『数据质量』页")
        if self._data_manager:
            st.caption("数据管理器已注入，可在此基础上扩展原始/清洗数据量统计")

        col4, col5, col6 = st.columns(3)
        with col4:
            st.metric("原始数据量", "--")
        with col5:
            st.metric("清洗后数据量", "--")
        with col6:
            st.metric("脏数据占比", "--")

        st.info("📈 数据质量趋势图 / 脏数据类型分布 / 清洗统计（后续迭代实现）")

        st.markdown("---")

        # 数据操作
        st.subheader("⚙️ 数据操作")
        col7, col8, col9 = st.columns(3)
        with col7:
            st.button("⬇️ 下载数据", disabled=True, use_container_width=True)
        with col8:
            st.button("🧹 清洗数据", disabled=True, use_container_width=True)
        with col9:
            st.button("📊 质量报告", disabled=True, use_container_width=True)

    # ═══════════════════════════════════════════
    #  页面：关于
    # ═══════════════════════════════════════════

    def _render_about(self) -> None:
        """渲染关于页"""
        st.title("ℹ️ 关于")
        st.markdown("---")

        st.markdown(f"## {self.title}")
        st.markdown("### v1 基础骨架")

        st.markdown("""
        **已完成模块:**
        - ✅ 项目脚手架 & 基础基础设施
        - ✅ 数据层（多数据源/raw读写/数据管理器）
        - ✅ 数据清洗v1（OHLC校验/去重/异常检测/脏数据归档）
        - ✅ 技术指标（MA/EMA/RSI/MACD/KDJ/布林带/ATR）
        - ✅ 策略基类 + 注册器 + 示例策略
        - ✅ 回测引擎（processed真实数据链路 + sim_broker + 绩效指标）
        - ✅ 实盘基础骨架（data_feed/portfolio/engine + PaperBroker）
        - ✅ 风控模块（10类规则 + 风控管理器 + 风控守护）
        - ✅ 资金分配（等权 + 风险平价 + 目标波动率TODO）
        - ✅ Web看板（v1基础骨架）
        - ✅ 数据质量探查页（任务15：实时指标 + 清洗运行 + 等级分布 + 脏数据统计 + 运行详情）

        **TODO（后续迭代）:**
        - ⏳ K线图交互可视化
        - ⏳ 回测结果完整可视化
        - ⏳ 实盘实时行情刷新
        - ⏳ 策略参数动态配置
        - ⏳ 实盘下单面板
        - ⏳ 风控实时仪表盘
        - ⏳ 用户认证/权限管理

        **技术栈:**
        - Python 3.12+
        - Streamlit（Web看板）
        - tkinter（桌面GUI）
        - pandas/numpy（数据处理）
        - akshare（数据源）
        """)

        st.markdown("---")
        st.caption(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ═══════════════════════════════════════════
    #  Streamlit未安装提示
    # ═══════════════════════════════════════════

    @staticmethod
    def _render_no_streamlit() -> None:
        """Streamlit未安装时的提示"""
        print("=" * 60)
        print("  ⚠️ Streamlit未安装")
        print("=" * 60)
        print()
        print("  Web看板需要Streamlit依赖，请运行以下命令安装:")
        print()
        print("    pip install streamlit")
        print()
        print("  安装后运行:")
        print()
        print("    streamlit run run_web.py")
        print()
        print("=" * 60)
        logger.warning("[web] Streamlit未安装，Web看板不可用")


# ═══════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════

def run_dashboard(title: str = "Pythonwdhmm 量化交易系统") -> None:
    """
    便捷函数：运行Web看板

    Parameters
    ----------
    title : str
        看板标题
    """
    dashboard = Dashboard(title=title)
    dashboard.run()


if __name__ == "__main__":
    run_dashboard()
