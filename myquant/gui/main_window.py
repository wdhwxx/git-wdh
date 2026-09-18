# 文件名：myquant/gui/main_window.py
"""
main_window.py - GUI主窗口
===========================
量化交易系统桌面图形界面主窗口。

v1实现范围（基础骨架）:
  - 使用tkinter（Python内置，无需额外依赖）
  - 主窗口：标题、菜单栏、状态栏、基本布局
  - 菜单：文件、策略、回测、实盘、风控、数据、帮助
  - 状态栏：显示当前状态、时间、连接状态
  - 主体区域：占位面板（后续迭代添加K线图、持仓表、订单表等）
  - 与核心模块集成接口（策略管理、回测运行、实盘控制）

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（实盘控制面板、实时行情显示全部预留）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — GUI只展示当前已知数据，不预测未来

防未来函数说明:
  - GUI只展示当前已知的账户/持仓/行情/回测结果数据
  - 不展示任何未来数据或预测数据
  - 回测结果展示严格按时间顺序，不泄露未来信息
  - 实盘面板只展示当前实时数据，不预判未来走势

TODO(后续迭代):
  - K线图控件（pyqtgraph/matplotlib集成）
  - 持仓表格、订单表格、成交明细
  - 策略参数配置面板
  - 回测结果可视化（净值曲线、回撤图、绩效指标）
  - 实盘控制面板（下单、撤单、风控状态）
  - 数据质量探查页面
  - 多标签页布局
  - 深色主题
  - 快捷键支持
  - 窗口布局保存/恢复
  - 迁移到PyQt5/PySide6（更专业的GUI框架）

依赖: tkinter, datetime, typing, myquant.core.logger
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from myquant.core.logger import get_logger

logger = get_logger("gui.main_window")


# ═══════════════════════════════════════════════
#  主窗口
# ═══════════════════════════════════════════════

class MainWindow:
    """
    量化交易系统GUI主窗口

    v1基础骨架：菜单栏 + 状态栏 + 主体占位面板。

    使用方式:
        from myquant.gui import MainWindow

        app = MainWindow(title="Pythonwdhmm 量化交易系统")
        app.run()

    与核心模块集成（后续迭代）:
        # 注入策略管理器
        app.set_strategy_manager(strategy_mgr)
        # 注入回测引擎
        app.set_backtest_engine(backtest_engine)
        # 注入实盘引擎
        app.set_live_engine(live_engine)
        # 注入风控管理器
        app.set_risk_manager(risk_mgr)
    """

    def __init__(
        self,
        title: str = "Pythonwdhmm 量化交易系统",
        width: int = 1200,
        height: int = 800,
    ):
        """
        初始化主窗口

        Parameters
        ----------
        title : str
            窗口标题
        width : int
            窗口宽度（像素）
        height : int
            窗口高度（像素）
        """
        self.title = title
        self.width = width
        self.height = height

        # 核心模块引用（后续迭代注入）
        self._strategy_manager: Optional[Any] = None
        self._backtest_engine: Optional[Any] = None
        self._live_engine: Optional[Any] = None
        self._risk_manager: Optional[Any] = None
        self._data_manager: Optional[Any] = None

        # 状态变量
        self._status_text = None
        self._connection_text = None
        self._time_text = None

        # 回调注册表（菜单动作）
        self._callbacks: Dict[str, Callable] = {}

        # 创建窗口（必须先创建root，才能创建StringVar）
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(800, 600)

        # 现在root已创建，可以创建StringVar
        self._status_text = tk.StringVar(value="就绪")
        self._connection_text = tk.StringVar(value="未连接")
        self._time_text = tk.StringVar(value="")

        # 构建UI
        self._build_menu()
        self._build_body()
        self._build_status_bar()

        # 启动时钟更新
        self._update_clock()

        logger.info(f"[gui] 主窗口初始化: {title}, {width}x{height}")

    # ═══════════════════════════════════════════
    #  菜单栏
    # ═══════════════════════════════════════════

    def _build_menu(self) -> None:
        """构建菜单栏"""
        menubar = tk.Menu(self.root)

        # 文件菜单
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开配置...", command=self._on_open_config, accelerator="Ctrl+O")
        file_menu.add_command(label="保存配置", command=self._on_save_config, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="导出回测结果", command=self._on_export_results)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_exit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="文件", menu=file_menu)

        # 策略菜单
        strategy_menu = tk.Menu(menubar, tearoff=0)
        strategy_menu.add_command(label="策略管理", command=self._on_strategy_manager)
        strategy_menu.add_command(label="策略参数配置", command=self._on_strategy_params)
        strategy_menu.add_separator()
        strategy_menu.add_command(label="刷新策略列表", command=self._on_refresh_strategies)
        menubar.add_cascade(label="策略", menu=strategy_menu)

        # 回测菜单
        backtest_menu = tk.Menu(menubar, tearoff=0)
        backtest_menu.add_command(label="新建回测", command=self._on_new_backtest, accelerator="Ctrl+B")
        backtest_menu.add_command(label="运行回测", command=self._on_run_backtest, accelerator="F5")
        backtest_menu.add_command(label="回测报告", command=self._on_backtest_report)
        backtest_menu.add_separator()
        backtest_menu.add_command(label="回测历史", command=self._on_backtest_history)
        menubar.add_cascade(label="回测", menu=backtest_menu)

        # 实盘菜单
        live_menu = tk.Menu(menubar, tearoff=0)
        live_menu.add_command(label="连接券商", command=self._on_connect_broker)
        live_menu.add_command(label="断开连接", command=self._on_disconnect_broker)
        live_menu.add_separator()
        live_menu.add_command(label="启动实盘", command=self._on_start_live, accelerator="F6")
        live_menu.add_command(label="停止实盘", command=self._on_stop_live, accelerator="F7")
        live_menu.add_separator()
        live_menu.add_command(label="手动下单", command=self._on_manual_order)
        menubar.add_cascade(label="实盘", menu=live_menu)

        # 风控菜单
        risk_menu = tk.Menu(menubar, tearoff=0)
        risk_menu.add_command(label="风控状态", command=self._on_risk_status)
        risk_menu.add_command(label="风控规则配置", command=self._on_risk_config)
        risk_menu.add_separator()
        risk_menu.add_command(label="风控事件日志", command=self._on_risk_events)
        risk_menu.add_command(label="暂停交易", command=self._on_pause_trading)
        risk_menu.add_command(label="恢复交易", command=self._on_resume_trading)
        menubar.add_cascade(label="风控", menu=risk_menu)

        # 数据菜单
        data_menu = tk.Menu(menubar, tearoff=0)
        data_menu.add_command(label="数据下载", command=self._on_data_download)
        data_menu.add_command(label="数据清洗", command=self._on_data_clean)
        data_menu.add_command(label="数据质量报告", command=self._on_data_quality)
        data_menu.add_separator()
        data_menu.add_command(label="数据源管理", command=self._on_data_sources)
        menubar.add_cascade(label="数据", menu=data_menu)

        # 视图菜单
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="全屏", command=self._on_toggle_fullscreen, accelerator="F11")
        view_menu.add_command(label="重置布局", command=self._on_reset_layout)
        menubar.add_cascade(label="视图", menu=view_menu)

        # 帮助菜单
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="使用文档", command=self._on_docs)
        help_menu.add_command(label="快捷键", command=self._on_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self._on_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.root.config(menu=menubar)

        # 绑定快捷键
        self.root.bind("<Control-o>", lambda e: self._on_open_config())
        self.root.bind("<Control-s>", lambda e: self._on_save_config())
        self.root.bind("<Control-q>", lambda e: self._on_exit())
        self.root.bind("<Control-b>", lambda e: self._on_new_backtest())
        self.root.bind("<F5>", lambda e: self._on_run_backtest())
        self.root.bind("<F6>", lambda e: self._on_start_live())
        self.root.bind("<F7>", lambda e: self._on_stop_live())
        self.root.bind("<F11>", lambda e: self._on_toggle_fullscreen())

    # ═══════════════════════════════════════════
    #  主体区域
    # ═══════════════════════════════════════════

    def _build_body(self) -> None:
        """构建主体区域（v1占位面板）"""
        # 主容器
        main_frame = tk.Frame(self.root, bg="#f0f0f0")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 顶部工具栏
        toolbar = tk.Frame(main_frame, bg="#e0e0e0", height=40)
        toolbar.pack(fill=tk.X, pady=(0, 5))
        toolbar.pack_propagate(False)

        tk.Button(toolbar, text="▶ 运行回测", command=self._on_run_backtest,
                  bg="#4CAF50", fg="white", relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(toolbar, text="● 启动实盘", command=self._on_start_live,
                  bg="#2196F3", fg="white", relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(toolbar, text="■ 停止", command=self._on_stop_live,
                  bg="#f44336", fg="white", relief=tk.FLAT, padx=15).pack(side=tk.LEFT, padx=5, pady=5)

        tk.Label(toolbar, text="|", bg="#e0e0e0", fg="#999").pack(side=tk.LEFT, padx=10)

        tk.Button(toolbar, text="策略", command=self._on_strategy_manager,
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=2, pady=5)
        tk.Button(toolbar, text="风控", command=self._on_risk_status,
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=2, pady=5)
        tk.Button(toolbar, text="数据", command=self._on_data_quality,
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, padx=2, pady=5)

        # 主体内容区（左右分栏）
        content = tk.Frame(main_frame, bg="#f0f0f0")
        content.pack(fill=tk.BOTH, expand=True)

        # 左侧面板（占位）
        left_panel = tk.Frame(content, bg="white", width=300, relief=tk.SUNKEN, bd=1)
        left_panel.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        left_panel.pack_propagate(False)

        tk.Label(left_panel, text="📊 策略列表", font=("Arial", 11, "bold"),
                 bg="white", anchor="w").pack(fill=tk.X, padx=10, pady=(10, 5))
        tk.Label(left_panel, text="（v1占位，后续迭代实现）",
                 font=("Arial", 9), fg="#999", bg="white").pack(pady=20)

        tk.Label(left_panel, text="💼 持仓概览", font=("Arial", 11, "bold"),
                 bg="white", anchor="w").pack(fill=tk.X, padx=10, pady=(20, 5))
        tk.Label(left_panel, text="（v1占位，后续迭代实现）",
                 font=("Arial", 9), fg="#999", bg="white").pack(pady=20)

        # 右侧主区域（占位）
        right_panel = tk.Frame(content, bg="white", relief=tk.SUNKEN, bd=1)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 欢迎信息
        welcome = tk.Frame(right_panel, bg="white")
        welcome.pack(expand=True)

        tk.Label(welcome, text="Pythonwdhmm 量化交易系统",
                 font=("Arial", 24, "bold"), bg="white", fg="#333").pack(pady=(0, 10))
        tk.Label(welcome, text="v1 基础骨架",
                 font=("Arial", 14), bg="white", fg="#666").pack(pady=(0, 30))

        info_text = (
            "✅ 菜单栏：文件/策略/回测/实盘/风控/数据/视图/帮助\n"
            "✅ 工具栏：运行回测/启动实盘/停止/策略/风控/数据\n"
            "✅ 状态栏：状态/连接/时间\n"
            "⏳ 后续迭代：K线图、持仓表、回测可视化、实盘控制面板\n"
        )
        tk.Label(welcome, text=info_text, font=("Arial", 11),
                 bg="white", fg="#555", justify=tk.LEFT).pack(pady=10)

        tk.Label(welcome, text="按 F5 运行回测 | F6 启动实盘 | F11 全屏",
                 font=("Arial", 10), bg="white", fg="#999").pack(pady=(30, 0))

    # ═══════════════════════════════════════════
    #  状态栏
    # ═══════════════════════════════════════════

    def _build_status_bar(self) -> None:
        """构建状态栏"""
        status_bar = tk.Frame(self.root, bg="#e0e0e0", height=28)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        status_bar.pack_propagate(False)

        # 左侧：状态
        tk.Label(status_bar, textvariable=self._status_text,
                 bg="#e0e0e0", anchor="w", padx=10).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 中间：连接状态
        tk.Label(status_bar, textvariable=self._connection_text,
                 bg="#e0e0e0", anchor="center", padx=20).pack(side=tk.LEFT)

        # 右侧：时间
        tk.Label(status_bar, textvariable=self._time_text,
                 bg="#e0e0e0", anchor="e", padx=10).pack(side=tk.RIGHT)

    def _update_clock(self) -> None:
        """更新时钟（每秒调用）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._time_text.set(now)
        self.root.after(1000, self._update_clock)

    # ═══════════════════════════════════════════
    #  状态管理
    # ═══════════════════════════════════════════

    def set_status(self, text: str) -> None:
        """设置状态栏文本"""
        self._status_text.set(text)
        logger.debug(f"[gui] 状态: {text}")

    def set_connection(self, text: str) -> None:
        """设置连接状态"""
        self._connection_text.set(text)

    # ═══════════════════════════════════════════
    #  核心模块注入（后续迭代）
    # ═══════════════════════════════════════════

    def set_strategy_manager(self, manager: Any) -> None:
        """注入策略管理器"""
        self._strategy_manager = manager
        logger.info("[gui] 策略管理器已注入")

    def set_backtest_engine(self, engine: Any) -> None:
        """注入回测引擎"""
        self._backtest_engine = engine
        logger.info("[gui] 回测引擎已注入")

    def set_live_engine(self, engine: Any) -> None:
        """注入实盘引擎"""
        self._live_engine = engine
        logger.info("[gui] 实盘引擎已注入")

    def set_risk_manager(self, manager: Any) -> None:
        """注入风控管理器"""
        self._risk_manager = manager
        logger.info("[gui] 风控管理器已注入")

    def set_data_manager(self, manager: Any) -> None:
        """注入数据管理器"""
        self._data_manager = manager
        logger.info("[gui] 数据管理器已注入")

    def register_callback(self, name: str, callback: Callable) -> None:
        """注册菜单动作回调"""
        self._callbacks[name] = callback

    def _trigger_callback(self, name: str) -> None:
        """触发回调（如果已注册）"""
        callback = self._callbacks.get(name)
        if callback:
            try:
                callback()
            except Exception as e:
                logger.error(f"[gui] 回调执行失败 {name}: {e}")
                self.set_status(f"错误: {e}")
        else:
            self.set_status(f"功能未实现: {name}（v1占位）")
            logger.info(f"[gui] 功能未实现: {name}")

    # ═══════════════════════════════════════════
    #  菜单动作（v1占位，触发回调或显示提示）
    # ═══════════════════════════════════════════

    def _on_open_config(self) -> None: self._trigger_callback("open_config")
    def _on_save_config(self) -> None: self._trigger_callback("save_config")
    def _on_export_results(self) -> None: self._trigger_callback("export_results")
    def _on_exit(self) -> None:
        self.set_status("退出中...")
        logger.info("[gui] 用户退出")
        self.root.quit()

    def _on_strategy_manager(self) -> None: self._trigger_callback("strategy_manager")
    def _on_strategy_params(self) -> None: self._trigger_callback("strategy_params")
    def _on_refresh_strategies(self) -> None: self._trigger_callback("refresh_strategies")

    def _on_new_backtest(self) -> None: self._trigger_callback("new_backtest")
    def _on_run_backtest(self) -> None:
        self.set_status("运行回测中...")
        self._trigger_callback("run_backtest")
    def _on_backtest_report(self) -> None: self._trigger_callback("backtest_report")
    def _on_backtest_history(self) -> None: self._trigger_callback("backtest_history")

    def _on_connect_broker(self) -> None:
        self.set_connection("连接中...")
        self._trigger_callback("connect_broker")
    def _on_disconnect_broker(self) -> None:
        self.set_connection("未连接")
        self._trigger_callback("disconnect_broker")
    def _on_start_live(self) -> None:
        self.set_status("实盘运行中...")
        self.set_connection("已连接")
        self._trigger_callback("start_live")
    def _on_stop_live(self) -> None:
        self.set_status("已停止")
        self._trigger_callback("stop_live")
    def _on_manual_order(self) -> None: self._trigger_callback("manual_order")

    def _on_risk_status(self) -> None: self._trigger_callback("risk_status")
    def _on_risk_config(self) -> None: self._trigger_callback("risk_config")
    def _on_risk_events(self) -> None: self._trigger_callback("risk_events")
    def _on_pause_trading(self) -> None:
        self.set_status("交易已暂停")
        self._trigger_callback("pause_trading")
    def _on_resume_trading(self) -> None:
        self.set_status("交易已恢复")
        self._trigger_callback("resume_trading")

    def _on_data_download(self) -> None: self._trigger_callback("data_download")
    def _on_data_clean(self) -> None: self._trigger_callback("data_clean")
    def _on_data_quality(self) -> None: self._trigger_callback("data_quality")
    def _on_data_sources(self) -> None: self._trigger_callback("data_sources")

    def _on_toggle_fullscreen(self) -> None:
        current = self.root.attributes("-fullscreen")
        self.root.attributes("-fullscreen", not current)

    def _on_reset_layout(self) -> None:
        self.root.geometry(f"{self.width}x{self.height}")
        self.set_status("布局已重置")

    def _on_docs(self) -> None: self._trigger_callback("docs")
    def _on_shortcuts(self) -> None:
        self.set_status("快捷键: F5回测 | F6实盘 | F7停止 | F11全屏 | Ctrl+O打开 | Ctrl+S保存 | Ctrl+Q退出")

    def _on_about(self) -> None:
        about = tk.Toplevel(self.root)
        about.title("关于")
        about.geometry("400x300")
        about.resizable(False, False)
        tk.Label(about, text="Pythonwdhmm", font=("Arial", 20, "bold")).pack(pady=(30, 5))
        tk.Label(about, text="量化交易系统", font=("Arial", 14)).pack(pady=5)
        tk.Label(about, text="v1 基础骨架", font=("Arial", 11), fg="#666").pack(pady=5)
        tk.Label(about, text="\n模块：数据/清洗/指标/策略/回测/实盘/风控/资金分配",
                 font=("Arial", 9), fg="#888", justify=tk.CENTER).pack(pady=10)
        tk.Button(about, text="确定", command=about.destroy, width=10).pack(pady=20)

    # ═══════════════════════════════════════════
    #  运行
    # ═══════════════════════════════════════════

    def run(self) -> None:
        """运行主窗口（阻塞）"""
        logger.info("[gui] 主窗口启动")
        self.set_status("就绪")
        self.root.mainloop()

    def __repr__(self) -> str:
        return f"MainWindow(title={self.title}, size={self.width}x{self.height})"
