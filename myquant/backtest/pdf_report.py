# 文件名：myquant/backtest/pdf_report.py
"""
backtest/pdf_report.py - PDF 报告生成器
========================================
基于 fpdf2（纯 Python、免费、本地离线、无系统依赖）生成专业可打印/可归档的回测 PDF 报告。

设计原则（规则2：为实盘无缝对接准备）:
  - PdfReportGenerator 回测/实盘共用，输入 BacktestResult 即可生成 PDF
  - 实盘运行时可定期生成日报/周报 PDF 归档
  - 中文字体自动探测系统字体（黑体 simhei.ttf 优先，备选等线/雅黑）
  - 图表用 fpdf2 原生绘图原语（折线/矩形），不依赖外部图表库，全程离线

报告结构:
  1. 标题区（策略名/期间/标的/初始资金/运行ID/生成时间）
  2. 核心绩效表（收益/风险/风险调整收益）
  3. 交易统计表（次数/胜率/盈亏比/连胜连亏/成本）
  4. 净值曲线图（策略净值 + 基准，fpdf2 折线）
  5. 回撤曲线图（fpdf2 折线）
  6. 蒙特卡洛分析（统计表 + 收益分布直方图 + 回撤分布直方图 + 分位带折线，含风险提示）
  7. 交易明细表（自动分页，表头随页重复）
  8. 标的列表附录（完整标的池，9pt 表格多列，打印可读，自动分页）
  9. 页脚（页码 + 生成时间 + 统计模拟风险提示）

防未来函数（规则4）:
  - PDF 只展示已实现的回测结果（净值/成交/绩效）
  - 蒙特卡洛只重采样已实现的历史收益（统计模拟），报告标注非未来预测

使用方式:
    from myquant.backtest.pdf_report import PdfReportGenerator
    gen = PdfReportGenerator()
    path = gen.generate_pdf(result, "outputs/backtest_report.pdf")
"""

__version__ = "0.1.0"
__author__ = "Pythonwdhmm"

import os
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import numpy as np
from fpdf import FPDF

from myquant.backtest.monte_carlo import MonteCarloAnalyzer, MonteCarloResult
from myquant.core.logger import get_logger
from myquant.models.backtest import BacktestResult, EquityPoint

logger = get_logger(__name__)

# A4 页面与边距（mm）
_PAGE_W = 210.0
_PAGE_H = 297.0
_MARGIN = 10.0
_PLOT_W = _PAGE_W - 2 * _MARGIN   # 190
_PLOT_H = 58.0                     # 图表高度
_ROW_H = 7.0                       # 表格行高

# PDF 分页与图表布局常量（mm）
_PAGE_BREAK_MARGIN: float = 15.0    # 自动分页安全边距（与 set_auto_page_break margin 一致）
_PLOT_BLOCK_EXTRA: float = 12.0     # 绘图区下方（标签+图例+间距）预留高度
_HIST_BLOCK_EXTRA: float = 8.0      # 直方图下方（极值标签+间距）预留高度
_TRADES_PAGE_BOTTOM_MARGIN: float = 30.0  # 交易明细表分页底线余量（保证表头可重复绘制）

# 交易明细渲染上限（对齐 HTML 报告"最近50笔"口径，防止超大成交生成巨型 PDF）
_MAX_TRADES_IN_REPORT: int = 50

# 净值/回撤曲线渲染点数上限（渲染层降采样，工程优化保留）
# mixed 引擎按 bar 逐点记录（301万点），PDF 逐点画线会使单页内容流解压后超1亿字节、
# 文件体积达数 MB，此处仅压缩渲染数据，不影响指标计算与 CSV 导出。
# 301万点按日期取每日最后一条 → 约1455个逐日点。
# 注：Acrobat 报错"本页面上存在错误"的根因并非曲线点数，而是标的列表超长文本
# 字符串（见 _MAX_SYMBOLS_IN_REPORT），降采样作为通用体积优化继续保留。
_MAX_CURVE_POINTS: int = 2000

# 标的列表在 PDF 报告中的显示数量上限
# 根因（Acrobat 兼容性）：cfg.symbols 全量可达 3207 只，join 后约 3.5 万字符，
# 编码为单条 literal string 后超 64KB（72785 字节），Adobe Acrobat 解析此类
# 超长字符串失败，弹"本页面上存在错误"（qpdf/MuPDF/pdfminer 均容忍，无法复现）。
# PDF 报告正文按上限截断显示（前 N 只 + 总数）；完整标的池见报告末尾
# 附录页（_render_symbols_appendix）与 HTML 报告。
# 取 6 只可保证 9pt 一行放得下（A4 内容宽 190mm），不溢出页面。
_MAX_SYMBOLS_IN_REPORT: int = 6

# 标的列表附录页渲染参数（打印可读优先）
# 正文截断后，报告末尾追加"完整标的列表"附录（自动分页）：
#  - 字号 9pt（≈印刷小五号），为打印清晰可读的硬约束；行高 3.6mm 留足行距
#  - 5 列 × 每格 2 只 = 每行 10 只，列宽 35.6mm 用满 A4 内容宽 190mm
#  - 每格文本仅约 30 字节，天然规避超长 literal string（Acrobat 64KB 上限）
#  - 单元格逐行输出 + 自动分页，浅灰行线辅助打印后逐行扫描
_SYMBOLS_APPENDIX_FONT_SIZE: float = 9.0
_SYMBOLS_APPENDIX_ROW_H: float = 3.6
_SYMBOLS_APPENDIX_COLS: int = 5
_SYMBOLS_PER_CELL: int = 2
_SYMBOLS_COL_GAP: float = 3.0

# 中文字体候选（单 TTF 优先，Windows 系统字体，本地离线）
_FONT_CANDIDATES: List[str] = [
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体
    r"C:\Windows\Fonts\Deng.ttf",        # 等线
    r"C:\Windows\Fonts\msyh.ttc",        # 微软雅黑（TTC 集合）
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体（TTC 集合）
]


# ═══════════════════════════════════════════════
#  FPDF 子类：页眉/页脚
# ═══════════════════════════════════════════════



# ═══════════════════════════════════════════════
#  净值曲线渲染层降采样（Acrobat 兼容性修复）
# ═══════════════════════════════════════════════

def downsample_equity_curve(
    curve: Sequence[EquityPoint],
    max_points: int = _MAX_CURVE_POINTS,
) -> List[EquityPoint]:
    """渲染层降采样：按日期取每日最后一条；仍超上限则均匀抽稀（保首尾）。

    仅用于图表绘制（PDF/HTML），不改变指标计算与 CSV 导出。
    mixed 引擎逐 bar 记录（301万点）会让 PDF 逐点画线产生超1亿字节内容流，
    Acrobat 无法解析；按日期取末点后约为1455个逐日点，落在 Acrobat 兼容范围。
    """
    if not curve:
        return list(curve)
    last_by_date: Dict[str, EquityPoint] = {}
    for ep in curve:
        last_by_date[ep.date] = ep  # dict 保持首次插入顺序 = 时间顺序，覆盖即取当日最后一条
    daily = list(last_by_date.values())
    if len(daily) <= max_points:
        return daily
    step = (len(daily) - 1) / (max_points - 1)
    idx = [round(i * step) for i in range(max_points)]
    return [daily[i] for i in idx]

class _ReportPdf(FPDF):
    """报告专用 FPDF：固定字体、页眉标题、页脚页码"""

    def __init__(self, title: str):
        super().__init__(format="A4", unit="mm")
        self.report_title = title
        self.set_auto_page_break(auto=True, margin=15.0)
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)

    def header(self) -> None:
        if self.page_no() <= 1:
            return  # 首页由标题区渲染
        self.set_font("cjk", "", 9)
        self.set_text_color(102, 126, 234)
        self.cell(0, 8, self.report_title, align="L")
        self.ln(10)

    def footer(self) -> None:
        self.set_y(-13)
        self.set_font("cjk", "", 7)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"第 {self.page_no()} / {{nb}} 页    生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", align="C")
        self.ln(2)
        self.set_font("cjk", "", 6)
        self.set_text_color(180, 180, 180)
        self.cell(0, 4, "风险提示：本报告基于历史数据，蒙特卡洛为统计模拟，不代表未来实际表现。", align="C")


# ═══════════════════════════════════════════════
#  PdfReportGenerator - PDF 报告生成器
# ═══════════════════════════════════════════════

class PdfReportGenerator:
    """
    PDF 报告生成器

    核心功能:
      - generate_pdf(result, output_path, ...): 生成完整 PDF 报告
      - 覆盖: 绩效表 / 交易统计 / 净值与回撤曲线 / 蒙特卡洛分析 / 交易明细
    """

    def __init__(self, title: str = "鲲鹏量化交易系统 - 回测报告"):
        """初始化 PDF 报告生成器"""
        self.title = title
        logger.info(f"PDF报告生成器初始化: title={title}")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate_pdf(
        self,
        result: BacktestResult,
        output_path: str = "outputs/backtest_report.pdf",
        include_monte_carlo: bool = True,
        mc_n_simulations: int = 2000,
        mc_horizon_days: int = 252,
        mc_seed: int = 42,
    ) -> str:
        """
        生成PDF报告

        参数:
            result: 回测结果
            output_path: 输出路径
            include_monte_carlo: 是否包含蒙特卡洛分析（默认True）
            mc_n_simulations: 蒙特卡洛模拟次数
            mc_horizon_days: 蒙特卡洛模拟期数/交易日
            mc_seed: 蒙特卡洛随机种子（固定可复现）

        返回:
            str: 报告文件路径
        """
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        pdf = _ReportPdf(self.title)
        if not self._load_font(pdf):
            logger.warning("未找到中文字体，中文可能无法正常显示")

        mc = self._run_monte_carlo(
            result, include_monte_carlo, mc_n_simulations, mc_horizon_days, mc_seed,
        )

        self._build_pdf(pdf, result, mc)
        pdf.output(output_path)

        logger.info(f"PDF报告已生成: {output_path}")
        return output_path

    # ------------------------------------------------------------------
    # 内容构建
    # ------------------------------------------------------------------

    def _build_pdf(
        self, pdf: _ReportPdf, result: BacktestResult, mc: Optional[MonteCarloResult],
    ) -> None:
        """按顺序构建 PDF 各区块"""
        pdf.add_page()
        self._render_title(pdf, result)
        self._render_metrics(pdf, result)
        self._render_trade_stats(pdf, result)

        pdf.add_page()
        self._render_equity_curve(pdf, result)
        self._render_drawdown_curve(pdf, result)

        if mc is not None:
            pdf.add_page()
            self._render_monte_carlo(pdf, mc)

        self._render_trades(pdf, result)
        self._render_symbols_appendix(pdf, result.config.symbols)

    # ── 标题区 ──
    def _render_title(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """标题与元信息"""
        pdf.set_font("cjk", "", 18)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(0, 16, self.title, border=0, fill=True, align="C")
        pdf.ln(20)

        cfg = result.config
        pdf.set_text_color(60, 60, 60)
        pdf.set_font("cjk", "", 10)
        # 标的列表：全量列出会生成超 64KB 的单条 literal string，Adobe Acrobat
        # 解析失败报"本页面上存在错误"，故按 _MAX_SYMBOLS_IN_REPORT 截断显示，
        # 完整标的池见 HTML 报告（HTML 无此长度限制）。
        if len(cfg.symbols) > _MAX_SYMBOLS_IN_REPORT:
            head_symbols = ", ".join(cfg.symbols[:_MAX_SYMBOLS_IN_REPORT])
            symbols_text = f"{head_symbols}, …（共 {len(cfg.symbols)} 只）"
        else:
            symbols_text = ", ".join(cfg.symbols)
        meta = [
            ("策略名称", cfg.strategy_name), ("运行ID", result.run_id),
            ("回测期间", f"{cfg.start_date} ~ {cfg.end_date}"),
            ("标的列表", symbols_text),
            ("初始资金", f"{cfg.initial_capital:,.0f}"),
            ("运行耗时", f"{result.duration_seconds:.2f}秒"),
        ]
        for label, value in meta:
            pdf.set_font("cjk", "", 9)
            pdf.set_text_color(140, 140, 140)
            pdf.cell(24, _ROW_H, label)
            pdf.set_text_color(40, 40, 40)
            pdf.cell(0, _ROW_H, value)
            pdf.ln()
        pdf.ln(4)

    # ── 核心绩效表 ──
    def _render_metrics(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """核心绩效表"""
        m = result.metrics
        final_value = result.config.initial_capital * result.final_equity
        self._section_title(pdf, "核心绩效")
        rows = [
            ("总收益率", f"{m.total_return:.2%}"),
            ("年化收益率", f"{m.annualized_return:.2%}"),
            ("年化波动率", f"{m.annualized_volatility:.2%}"),
            ("夏普比率", f"{m.sharpe_ratio:.4f}"),
            ("索提诺比率", f"{m.sortino_ratio:.4f}"),
            ("Calmar比率", f"{m.calmar_ratio:.4f}"),
            ("最大回撤", f"{m.max_drawdown:.2%}"),
            ("最大回撤天数", f"{m.max_drawdown_duration}天"),
            ("最终净值", f"{result.final_equity:.4f}"),
            ("最终资产", f"{final_value:,.0f}"),
        ]
        self._draw_key_value_table(pdf, rows, col1=60, col2=130)

    # ── 交易统计表 ──
    def _render_trade_stats(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """交易统计表"""
        m = result.metrics
        self._section_title(pdf, "交易统计")
        rows = [
            ("总交易次数", f"{m.total_trades}"),
            ("盈利 / 亏损次数", f"{m.winning_trades} / {m.losing_trades}"),
            ("胜率", f"{m.win_rate:.2%}"),
            ("盈亏比", "∞（全盈利）" if m.profit_loss_ratio == float('inf') else f"{m.profit_loss_ratio:.4f}"),
            ("平均盈利 / 平均亏损", f"{m.avg_win:.2f} / {m.avg_loss:.2f}"),
            ("最大连胜 / 最大连亏", f"{m.max_consecutive_wins} / {m.max_consecutive_losses}"),
            ("总手续费", f"{m.total_commission:.2f}"),
            ("总滑点", f"{m.total_slippage:.2f}"),
            ("换手率", f"{m.turnover:.2f}"),
        ]
        self._draw_key_value_table(pdf, rows, col1=60, col2=130)

    # ── 净值曲线图 ──
    def _render_equity_curve(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """净值曲线折线图（策略净值 + 基准净值）"""
        self._section_title(pdf, "净值曲线")
        curve = downsample_equity_curve(result.equity_curve)
        if not curve or len(curve) < 2:
            pdf.set_font("cjk", "", 9)
            pdf.cell(0, _ROW_H, "净值数据不足，无法绘制")
            return
        equities = [ep.equity for ep in curve]
        benches = [ep.benchmark_equity for ep in curve] or None
        self._draw_plot_area(pdf, y_label="净值")
        self._draw_lines(
            pdf,
            series_list=[("策略净值", equities, (102, 126, 234)),
                         ("基准净值", benches, (150, 150, 150))] if benches and benches[0] else [("策略净值", equities, (102, 126, 234))],
            y_min=min(min(equities), min(benches) if benches else min(equities)),
            y_max=max(max(equities), max(benches) if benches else max(equities)),
        )

    # ── 回撤曲线图 ──
    def _render_drawdown_curve(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """回撤曲线折线图（%）"""
        self._section_title(pdf, "回撤曲线")
        curve = downsample_equity_curve(result.equity_curve)
        if not curve or len(curve) < 2:
            pdf.set_font("cjk", "", 9)
            pdf.cell(0, _ROW_H, "净值数据不足，无法绘制")
            return
        drawdowns = [ep.drawdown * 100.0 for ep in curve]
        self._draw_plot_area(pdf, y_label="回撤(%)")
        self._draw_lines(
            pdf,
            series_list=[("回撤(%)", drawdowns, (231, 76, 60))],
            y_min=min(drawdowns + [0.0]),
            y_max=0.0,
        )

    # ── 蒙特卡洛分析 ──
    def _render_monte_carlo(self, pdf: _ReportPdf, mc: MonteCarloResult) -> None:
        """蒙特卡洛统计表 + 直方图 + 分位带"""
        self._section_title(
            pdf, f"蒙特卡洛分析（{mc.n_simulations}次模拟 × {mc.horizon_days}个交易日）",
        )
        rows = [
            ("中位收益(模拟)", f"{mc.median_final_return:.2%}"),
            ("均值收益", f"{mc.mean_final_return:.2%}"),
            ("P5 / P95 收益", f"{mc.p5_final_return:.2%} / {mc.p95_final_return:.2%}"),
            ("亏损概率", f"{mc.prob_negative:.2%}"),
            ("中位最大回撤", f"{mc.median_max_drawdown:.2%}"),
            ("最大回撤95%CI", f"{mc.max_drawdown_ci_low:.2%} ~ {mc.max_drawdown_ci_high:.2%}"),
            ("破产概率", f"{mc.prob_ruin:.2%}"),
        ]
        self._draw_key_value_table(pdf, rows, col1=60, col2=130)

        # 收益分布直方图
        pdf.ln(4)
        self._section_title(pdf, "最终收益分布（蒙特卡洛）")
        if len(mc.final_returns) > 0:
            self._draw_histogram(pdf, mc.final_returns, bins=30, color=(102, 126, 234), fmt=".1%")

        pdf.ln(4)
        self._section_title(pdf, "最大回撤分布（蒙特卡洛）")
        if len(mc.max_drawdowns) > 0:
            self._draw_histogram(pdf, mc.max_drawdowns, bins=30, color=(231, 76, 60), fmt=".1%")

        # 分位带折线
        pdf.ln(4)
        self._section_title(pdf, "收益路径分位带（蒙特卡洛模拟）")
        if mc.quantile_equity.size > 0:
            qe = mc.quantile_equity
            pcts = mc.quantile_percentiles
            colors = [(149, 165, 166), (189, 195, 199), (102, 126, 234), (189, 195, 199), (149, 165, 166)]
            self._draw_plot_area(pdf, y_label="模拟净值")
            series = [
                (f"P{int(p)}", [float(v) for v in qe[i]], colors[i % len(colors)])
                for i, p in enumerate(pcts)
            ]
            self._draw_lines(pdf, series, y_min=float(qe.min()), y_max=float(qe.max()))
            pdf.ln(4)
            pdf.set_font("cjk", "", 7)
            pdf.set_text_color(160, 120, 40)
            pdf.cell(0, 5, "风险提示：以上为基于历史收益的统计模拟，不代表未来实际表现。", align="L")

    # ── 交易明细 ──
    def _render_trades(self, pdf: _ReportPdf, result: BacktestResult) -> None:
        """交易明细表（自动分页，表头随页重复；最多渲染最近 50 笔，对齐 HTML 口径）"""
        trades = result.trades[-_MAX_TRADES_IN_REPORT:]
        n_total = len(result.trades)
        if n_total > _MAX_TRADES_IN_REPORT:
            title = f"交易明细（最近 {len(trades)} 笔，共 {n_total} 笔）"
        else:
            title = f"交易明细（共 {n_total} 笔）"
        self._section_title(pdf, title)
        if not trades:
            pdf.set_font("cjk", "", 9)
            pdf.cell(0, _ROW_H, "无成交记录")
            return

        headers = ["日期", "标的", "方向", "数量", "价格", "盈亏", "手续费", "出场原因"]
        widths = [24, 22, 18, 20, 22, 24, 18, 42]
        col_aligns = ["C", "C", "C", "C", "C", "C", "C", "L"]

        def _header_row():
            pdf.set_font("cjk", "", 8)
            pdf.set_fill_color(102, 126, 234)
            pdf.set_text_color(255, 255, 255)
            for i, h in enumerate(headers):
                pdf.cell(widths[i], 6, h, border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_text_color(40, 40, 40)

        _header_row()
        pdf.set_font("cjk", "", 8)
        for t in trades:
            if pdf.get_y() > _PAGE_H - _TRADES_PAGE_BOTTOM_MARGIN:
                pdf.add_page()
                _header_row()
                pdf.set_font("cjk", "", 8)
            date = datetime.fromtimestamp(t.timestamp).strftime("%Y-%m-%d") if t.timestamp else ""
            vals = [
                date, t.symbol, t.side, f"{t.quantity:.0f}", f"{t.price:.2f}",
                f"{t.pnl:.2f}", f"{t.commission:.2f}", t.exit_reason or "",
            ]
            if t.pnl < 0:
                pdf.set_text_color(231, 76, 60)
            elif t.pnl > 0:
                pdf.set_text_color(39, 174, 96)
            else:
                pdf.set_text_color(40, 40, 40)
            for i, v in enumerate(vals):
                pdf.cell(widths[i], 6, str(v), border=1, align=col_aligns[i])
            pdf.ln()
        pdf.set_text_color(40, 40, 40)

    # ── 标的列表附录 ──
    def _render_symbols_appendix(self, pdf: _ReportPdf, symbols: Sequence[str]) -> None:
        """报告末尾附加完整标的列表页（表格多列，9pt 打印可读）。

        Args:
            pdf: 目标 PDF 对象。
            symbols: 完整标的池列表。

        设计说明:
        - 字号取 9pt（≈印刷小五号），保证打印/缩放后代码清晰可辨；
        - _SYMBOLS_APPENDIX_COLS 列 × 每格 _SYMBOLS_PER_CELL 只，用满 A4
          内容宽（190mm），列宽按 (190 - 列间距) / 列数 计算；
        - 每格文本仅约 30 字节，天然规避 Acrobat 超长 literal string 问题；
        - 单元格逐行输出，触底自动分页（set_auto_page_break），浅灰行线
          辅助打印后逐行扫描。
        """
        if not symbols:
            return
        pdf.add_page()
        self._section_title(pdf, f"标的列表（共 {len(symbols)} 只）")
        pdf.set_font("cjk", "", _SYMBOLS_APPENDIX_FONT_SIZE)
        pdf.set_text_color(40, 40, 40)
        col_w = (190 - (_SYMBOLS_APPENDIX_COLS - 1) * _SYMBOLS_COL_GAP) / _SYMBOLS_APPENDIX_COLS
        per_row = _SYMBOLS_APPENDIX_COLS * _SYMBOLS_PER_CELL
        pdf.set_draw_color(230, 230, 230)
        pdf.set_line_width(0.15)
        for i in range(0, len(symbols), per_row):
            row_syms = symbols[i:i + per_row]
            for c in range(_SYMBOLS_APPENDIX_COLS):
                cell_syms = row_syms[c * _SYMBOLS_PER_CELL:(c + 1) * _SYMBOLS_PER_CELL]
                text = "  ".join(cell_syms) if cell_syms else ""
                pdf.cell(col_w, _SYMBOLS_APPENDIX_ROW_H, text, border=0, align="L")
            pdf.ln(_SYMBOLS_APPENDIX_ROW_H)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 190, pdf.get_y())

    # ------------------------------------------------------------------
    # 绘图工具
    # ------------------------------------------------------------------

    def _section_title(self, pdf: _ReportPdf, text: str) -> None:
        """区块小标题"""
        if pdf.get_y() > _PAGE_H - 40:
            pdf.add_page()
        pdf.set_font("cjk", "", 12)
        pdf.set_text_color(44, 62, 80)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(2, 8, "", fill=True)
        pdf.cell(4, 8, "")
        pdf.cell(0, 8, text)
        pdf.ln(12)

    def _draw_key_value_table(
        self, pdf: _ReportPdf, rows: List[Tuple[str, str]], col1: float, col2: float,
    ) -> None:
        """两列键值表"""
        pdf.set_font("cjk", "", 9)
        for i, (k, v) in enumerate(rows):
            pdf.set_fill_color(248, 249, 250) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(col1, _ROW_H, k, border=1, fill=True, align="L")
            pdf.set_text_color(40, 40, 40)
            pdf.cell(col2, _ROW_H, v, border=1, fill=True, align="L")
            pdf.ln()
        pdf.ln(2)

    def _draw_plot_area(self, pdf: _ReportPdf, y_label: str) -> None:
        """绘制坐标区框架（含轴线标签）"""
        # 空间预检：绘图区+图例须完整落在本页，防 ln 触发自动分页导致坐标错位
        if pdf.get_y() + _PLOT_H + _PLOT_BLOCK_EXTRA > _PAGE_H - _PAGE_BREAK_MARGIN:
            pdf.add_page()
        x0, y0 = _MARGIN, pdf.get_y()
        pdf.set_draw_color(200, 200, 200)
        pdf.rect(x0, y0, _PLOT_W, _PLOT_H)
        pdf.set_font("cjk", "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.set_xy(x0 + 1, y0 + _PLOT_H - 3)
        pdf.cell(_PLOT_W - 2, 3, y_label, align="R")
        # 显式定位到绘图区下方（矩形+标签+图例行），供 _draw_lines 回退计算绘图区起点
        pdf.set_y(y0 + _PLOT_H + 6)

    def _draw_lines(
        self, pdf: _ReportPdf,
        series_list: List[Tuple[str, Sequence[float], Tuple[int, int, int]]],
        y_min: float, y_max: float,
    ) -> None:
        """在上一绘图区绘制多条折线（series_list: (名称, 值序列, RGB)）"""
        x0, y0 = _MARGIN, pdf.get_y() - (_PLOT_H + 6)
        span = (y_max - y_min) or 1.0

        def _point(v: float, i: int, n: int) -> Tuple[float, float]:
            px = x0 + (i / max(n - 1, 1)) * _PLOT_W
            py = y0 + (1.0 - (v - y_min) / span) * _PLOT_H
            return px, py

        for name, values, rgb in series_list:
            if not values or len(values) < 2:
                continue
            pdf.set_draw_color(*rgb)
            pdf.set_line_width(0.4)
            n = len(values)
            prev = None
            for i, v in enumerate(values):
                pt = _point(float(v), i, n)
                if prev:
                    pdf.line(prev[0], prev[1], pt[0], pt[1])
                prev = pt
        # 图例
        pdf.set_font("cjk", "", 7)
        lx = x0 + 2
        ly = pdf.get_y() - 4
        for name, _, rgb in series_list:
            pdf.set_draw_color(*rgb)
            pdf.line(lx, ly, lx + 6, ly)
            pdf.set_text_color(80, 80, 80)
            pdf.set_xy(lx + 7, ly - 2)
            pdf.cell(30, 4, name)
            lx += 34
        pdf.set_xy(x0, pdf.get_y() + 2)

    def _draw_histogram(
        self, pdf: _ReportPdf, values: Sequence[float],
        bins: int = 30, color: Tuple[int, int, int] = (102, 126, 234), fmt: str = ".2f",
    ) -> None:
        """绘制直方图（fpdf2 矩形柱）"""
        # 空间预检：直方图+极值标签须完整落在本页，防分页错位
        if pdf.get_y() + _PLOT_H + _HIST_BLOCK_EXTRA > _PAGE_H - _PAGE_BREAK_MARGIN:
            pdf.add_page()
        arr = np.asarray(values, dtype=float)
        counts, edges = np.histogram(arr, bins=bins)
        if counts.max() == 0:
            return
        x0, y0 = _MARGIN, pdf.get_y()
        plot_w = _PLOT_W
        plot_h = _PLOT_H
        pdf.set_draw_color(200, 200, 200)
        pdf.rect(x0, y0, plot_w, plot_h)
        bar_w = plot_w / len(counts)
        pdf.set_fill_color(*color)
        for i, c in enumerate(counts):
            bh = (c / counts.max()) * (plot_h - 8)
            pdf.rect(x0 + i * bar_w, y0 + plot_h - 4 - bh, bar_w * 0.8, bh, style="F")
        # 极值标签
        pdf.set_font("cjk", "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.set_xy(x0, y0 + plot_h - 4)
        pdf.cell(60, 4, f"min {edges[0]:{fmt}}", align="L")
        pdf.set_xy(x0 + plot_w - 60, y0 + plot_h - 4)
        pdf.cell(60, 4, f"max {edges[-1]:{fmt}}", align="R")
        # 显式定位到直方图下方（避免 ln 从 set_xy 后的 y 重复推进触发分页）
        pdf.set_y(y0 + plot_h + _HIST_BLOCK_EXTRA)

    # ------------------------------------------------------------------
    # 蒙特卡洛
    # ------------------------------------------------------------------

    def _run_monte_carlo(
        self,
        result: BacktestResult,
        include: bool,
        n_simulations: int,
        horizon_days: int,
        seed: int,
    ) -> Optional[MonteCarloResult]:
        """执行蒙特卡洛分析（净值曲线过短时跳过）"""
        if not include:
            return None
        if not result.equity_curve or len(result.equity_curve) < 30:
            logger.info("净值曲线过短(<30点)，跳过蒙特卡洛区块")
            return None
        analyzer = MonteCarloAnalyzer(
            n_simulations=n_simulations,
            horizon_days=horizon_days,
            seed=seed,
            initial_capital=result.config.initial_capital,
        )
        return analyzer.analyze(result.equity_curve)

    # ------------------------------------------------------------------
    # 字体
    # ------------------------------------------------------------------

    @staticmethod
    def _load_font(pdf: _ReportPdf) -> bool:
        """加载中文字体（系统字体，本地离线）；返回是否成功"""
        for path in _FONT_CANDIDATES:
            if os.path.exists(path):
                try:
                    pdf.add_font("cjk", "", path)
                    return True
                except Exception as exc:  # TTC 或字体解析失败时尝试下一个
                    logger.warning(f"字体加载失败 {path}: {exc}")
                    continue
        return False
