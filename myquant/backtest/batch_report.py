# -*- coding: utf-8 -*-
# 文件名：myquant/backtest/batch_report.py
"""
批量回测汇总报告生成器
========================
基于批量回测的 per_symbol 汇总行（与批量回测汇总 CSV 同构）生成
策略级汇总文档，一次批量回测同时产出两种附加格式：
  - HTML（批量统计卡 + 收益/夏普分布直方图 + Top N 榜，Chart.js 渲染）
  - PDF（同内容的可打印/可归档版，fpdf2 离线渲染，无系统依赖）
汇总 CSV 由 myquant/gui/batch_backtest_tab.py 导出，三者同目录同时间戳，
构成"批量回测汇总"一套文档。

设计原则（docs/py文件代码规则.md）：
  - 全量类型注解 + Google docstring + 防御式校验 + logging
  - 报告只展示已实现的批量回测结果（防未来函数铁律，不涉及未来数据）
  - 只消费汇总行摘要字段；metrics 字段缺失/非法时降级跳过，不中断
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from myquant.backtest.pdf_report import _ReportPdf, PdfReportGenerator
from myquant.core.logger import get_logger

logger = get_logger(__name__)

# ── 常量（模块顶部，禁止魔法数字）──────────────────────────────────────
DEFAULT_TITLE: str = "鲲鹏量化交易系统 - 批量回测汇总报告"
MAX_TOP_ROWS: int = 30        # Top N 榜行数（与单次回测报告口径对齐）
HIST_BINS: int = 30           # 收益/夏普分布直方图分箱数

# A4 页面与边距（mm，与 pdf_report.py 对齐）
_PAGE_W: float = 210.0
_PAGE_H: float = 297.0
_MARGIN: float = 10.0
_ROW_H: float = 7.0
_PLOT_H: float = 52.0
_PLOT_W: float = _PAGE_W - 2 * _MARGIN   # 190
_TABLE_BOTTOM_MARGIN: float = 30.0       # Top N 表分页底线余量

# 汇总行英文字段 → 中文表头（与 csv_utils.CSV_HEADER_MAPS["batch_summary"] 一致）
ROW_HEADERS_CN: Dict[str, str] = {
    "symbol": "标的代码",
    "status": "状态",
    "total_return": "总收益率",
    "sharpe_ratio": "夏普比率",
    "max_drawdown": "最大回撤",
    "trade_count": "交易次数",
    "equity_length": "净值长度",
    "mode": "引擎模式",
    "source": "数据来源",
    "error": "错误信息",
}


@dataclass(frozen=True)
class BatchReportMeta:
    """批量汇总报告元信息（来自批量回测参数与调度计划）。

    Attributes:
        strategy_name: 策略注册名。
        start_date: 回测起始日期 YYYY-MM-DD。
        end_date: 回测结束日期 YYYY-MM-DD。
        initial_capital: 初始资金。
        base_mode: 基准引擎模式（auto 时已按策略能力解析）。
        force_full: 是否强制全量重算。
        total: 标的总数。
        recomputed: 本次重算标的数。
        reused: 复用缓存标的数。
    """

    strategy_name: str
    start_date: str
    end_date: str
    initial_capital: float
    base_mode: str
    force_full: bool
    total: int
    recomputed: int
    reused: int


def _to_float(value: Any) -> Optional[float]:
    """把值安全转换为 float；None/非法返回 None（防御式）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BatchSummaryReportGenerator:
    """批量回测汇总报告生成器。

    核心功能:
      - generate_html(rows, meta, output_path): 生成 HTML 汇总报告（含图表）。
      - generate_pdf(rows, meta, output_path): 生成 PDF 汇总报告（离线渲染）。
      - generate_full_report(rows, meta, output_dir, ts): 一次生成 HTML + PDF。

    输入 rows 与批量回测汇总 CSV 的行结构同构（英文列）：
      symbol/status/total_return/sharpe_ratio/max_drawdown/trade_count/
      equity_length/mode/source/error。
    """

    def __init__(self, title: str = DEFAULT_TITLE) -> None:
        """初始化报告生成器。

        Args:
            title: 报告标题。
        """
        self.title = title
        logger.info("批量汇总报告生成器初始化: title=%s", title)

    # ─────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────

    def generate_full_report(
        self,
        rows: Sequence[Dict[str, Any]],
        meta: BatchReportMeta,
        output_dir: Path,
        ts: Optional[str] = None,
    ) -> Dict[str, str]:
        """一次生成 HTML + PDF 汇总报告（与汇总 CSV 同时间戳一套）。

        Args:
            rows: 批量汇总行列表（见类 docstring）。
            meta: 报告元信息。
            output_dir: 输出目录（策略子目录）。
            ts: 时间戳字符串；None 时按当前时间生成。

        Returns:
            {"html": HTML 路径, "pdf": PDF 路径}。

        Raises:
            ValueError: rows 为空时抛出。
        """
        if not rows:
            raise ValueError("汇总行列表为空，无法生成批量汇总报告")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = ts or datetime.now().strftime("%Y%m%d_%H%M%S")

        html_path = self.generate_html(
            rows, meta, output_dir / f"批量回测汇总_{ts}.html",
        )
        pdf_path = self.generate_pdf(
            rows, meta, output_dir / f"批量回测汇总_{ts}.pdf",
        )
        logger.info("批量汇总报告已生成: html=%s, pdf=%s", html_path, pdf_path)
        return {"html": str(html_path), "pdf": str(pdf_path)}

    # ─────────────────────────────────────────────
    # 数据统计（HTML/PDF 共用）
    # ─────────────────────────────────────────────

    def _compute_stats(
        self, rows: Sequence[Dict[str, Any]],
    ) -> Tuple[List[float], List[float], int, int, float, float, float, float, float]:
        """从汇总行计算批量统计指标。

        Args:
            rows: 批量汇总行列表。

        Returns:
            (收益序列, 夏普序列, completed 数, error 数,
             平均收益, 正收益占比, 平均夏普, 平均最大回撤, 平均交易次数)。
            有效样本不足时相应均值为 0.0。
        """
        returns: List[float] = []
        sharpes: List[float] = []
        drawdowns: List[float] = []
        trades: List[int] = []
        completed = 0
        errors = 0
        for row in rows:
            if row.get("status") != "completed":
                errors += 1
                continue
            completed += 1
            ret = _to_float(row.get("total_return"))
            if ret is not None:
                returns.append(ret)
            sharpe = _to_float(row.get("sharpe_ratio"))
            if sharpe is not None:
                sharpes.append(sharpe)
            dd = _to_float(row.get("max_drawdown"))
            if dd is not None:
                drawdowns.append(dd)
            tc = row.get("trade_count")
            if tc is not None:
                try:
                    trades.append(int(tc))
                except (TypeError, ValueError) as e:
                    logger.debug("trade_count 字段容错跳过: %s", e)

        avg_return = float(np.mean(returns)) if returns else 0.0
        positive_ratio = (
            sum(1 for r in returns if r > 0) / len(returns) if returns else 0.0
        )
        avg_sharpe = float(np.mean(sharpes)) if sharpes else 0.0
        avg_drawdown = float(np.mean(drawdowns)) if drawdowns else 0.0
        avg_trades = float(np.mean(trades)) if trades else 0.0
        return (
            returns, sharpes, completed, errors,
            avg_return, positive_ratio, avg_sharpe, avg_drawdown, avg_trades,
        )

    def _top_rows(self, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按总收益降序取前 MAX_TOP_ROWS 行（仅 completed 且有收益值）。"""
        valid = [
            row for row in rows
            if row.get("status") == "completed"
            and _to_float(row.get("total_return")) is not None
        ]
        valid.sort(
            key=lambda r: float(_to_float(r.get("total_return")) or 0.0),
            reverse=True,
        )
        return valid[:MAX_TOP_ROWS]

    @staticmethod
    def _histogram_data(
        values: Sequence[float], bins: int = HIST_BINS,
    ) -> Tuple[List[str], List[int]]:
        """数值数组 → 直方图数据（标签=区间中点百分比，计数）。

        Args:
            values: 数值序列。
            bins: 分箱数。

        Returns:
            (标签列表, 计数列表)；空输入返回两个空列表。
        """
        arr = np.asarray(values, dtype=float)
        if len(arr) == 0:
            return [], []
        counts, edges = np.histogram(arr, bins=bins)
        labels = [f"{(edges[i] + edges[i + 1]) / 2:.1%}" for i in range(len(counts))]
        return labels, [int(c) for c in counts]

    # ─────────────────────────────────────────────
    # HTML 报告
    # ─────────────────────────────────────────────

    def generate_html(
        self,
        rows: Sequence[Dict[str, Any]],
        meta: BatchReportMeta,
        output_path: Path,
    ) -> str:
        """生成 HTML 汇总报告（Chart.js 分布图 + Top N 榜）。

        Args:
            rows: 批量汇总行列表。
            meta: 报告元信息。
            output_path: 输出路径。

        Returns:
            str: 报告文件路径。
        """
        (
            returns, sharpes, completed, errors,
            avg_return, positive_ratio, avg_sharpe, avg_drawdown, avg_trades,
        ) = self._compute_stats(rows)
        top_rows = self._top_rows(rows)

        ret_labels, ret_counts = self._histogram_data(returns)
        sharpe_labels, sharpe_counts = self._histogram_data(sharpes)
        schedule = "强制全量" if meta.force_full else "增量"

        # Top N 表行（html 转义标的值，防注入）
        top_rows_html = ""
        for i, row in enumerate(top_rows, start=1):
            ret = _to_float(row.get("total_return"))
            sharpe = _to_float(row.get("sharpe_ratio"))
            dd = _to_float(row.get("max_drawdown"))
            tc = row.get("trade_count")
            ret_cls = "positive" if (ret or 0) >= 0 else "negative"
            top_rows_html += f"""
            <tr>
                <td>{i}</td>
                <td>{row.get('symbol', '')}</td>
                <td>{'完成' if row.get('status') == 'completed' else row.get('status', '')}</td>
                <td class="{ret_cls}">{ret:.2%}</td>
                <td>{sharpe:.4f}</td>
                <td class="negative">{dd:.2%}</td>
                <td>{tc if tc is not None else '-'}</td>
                <td>{row.get('source', '-')}</td>
            </tr>"""

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{self.title}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f7fa; color: #333; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 12px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header .meta {{ opacity: 0.9; font-size: 14px; }}
        .card {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .card h2 {{ font-size: 18px; margin-bottom: 16px; color: #2c3e50; border-left: 4px solid #667eea; padding-left: 12px; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; }}
        .metric-item {{ background: #f8f9fa; padding: 16px; border-radius: 8px; }}
        .metric-item .label {{ font-size: 12px; color: #7f8c8d; margin-bottom: 4px; }}
        .metric-item .value {{ font-size: 20px; font-weight: 600; color: #2c3e50; }}
        .metric-item .value.positive {{ color: #27ae60; }}
        .metric-item .value.negative {{ color: #e74c3c; }}
        .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        .chart-container {{ position: relative; height: 300px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; font-weight: 600; color: #7f8c8d; }}
        tr:hover {{ background: #f8f9fa; }}
        .footer {{ text-align: center; color: #95a5a6; font-size: 12px; padding: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{self.title}</h1>
            <div class="meta">
                策略: {meta.strategy_name} |
                期间: {meta.start_date} ~ {meta.end_date} |
                初始资金: {meta.initial_capital:,.0f} |
                引擎: {meta.base_mode} |
                调度: {schedule}
            </div>
        </div>

        <div class="card">
            <h2>批量统计</h2>
            <div class="metrics-grid">
                <div class="metric-item"><div class="label">标的总数</div><div class="value">{meta.total}</div></div>
                <div class="metric-item"><div class="label">重算 / 复用</div><div class="value">{meta.recomputed} / {meta.reused}</div></div>
                <div class="metric-item"><div class="label">完成 / 失败</div><div class="value">{completed} / {errors}</div></div>
                <div class="metric-item"><div class="label">平均收益</div><div class="value {'positive' if avg_return >= 0 else 'negative'}">{avg_return:.2%}</div></div>
                <div class="metric-item"><div class="label">正收益占比</div><div class="value">{positive_ratio:.2%}</div></div>
                <div class="metric-item"><div class="label">平均夏普</div><div class="value">{avg_sharpe:.4f}</div></div>
                <div class="metric-item"><div class="label">平均最大回撤</div><div class="value negative">{avg_drawdown:.2%}</div></div>
                <div class="metric-item"><div class="label">平均交易次数</div><div class="value">{avg_trades:.1f}</div></div>
            </div>
        </div>

        <div class="card">
            <h2>收益 / 夏普分布（{len(returns)} 只完成标的）</h2>
            <div class="chart-row">
                <div class="chart-container"><canvas id="retChart"></canvas></div>
                <div class="chart-container"><canvas id="sharpeChart"></canvas></div>
            </div>
        </div>

        <div class="card">
            <h2>Top {len(top_rows)}（按总收益降序）</h2>
            <table>
                <thead><tr><th>#</th><th>标的</th><th>状态</th><th>总收益</th><th>夏普</th><th>回撤</th><th>交易数</th><th>来源</th></tr></thead>
                <tbody>{top_rows_html}</tbody>
            </table>
        </div>

        <div class="footer">
            生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 鲲鹏量化交易系统 Pythonwdhmm
        </div>
    </div>

    <script>
        const retLabels = {json.dumps(ret_labels, ensure_ascii=False)};
        const retCounts = {json.dumps(ret_counts)};
        const sharpeLabels = {json.dumps(sharpe_labels, ensure_ascii=False)};
        const sharpeCounts = {json.dumps(sharpe_counts)};

        new Chart(document.getElementById('retChart'), {{
            type: 'bar',
            data: {{ labels: retLabels, datasets: [{{
                label: '标的数', data: retCounts,
                backgroundColor: 'rgba(39, 174, 96, 0.6)', borderColor: '#27ae60', borderWidth: 1
            }}] }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ x: {{ ticks: {{ maxRotation: 45 }}, title: {{ display: true, text: '总收益率' }} }},
                          y: {{ title: {{ display: true, text: '标的数' }} }} }} }}
        }});

        new Chart(document.getElementById('sharpeChart'), {{
            type: 'bar',
            data: {{ labels: sharpeLabels, datasets: [{{
                label: '标的数', data: sharpeCounts,
                backgroundColor: 'rgba(102, 126, 234, 0.6)', borderColor: '#667eea', borderWidth: 1
            }}] }},
            options: {{ responsive: true, maintainAspectRatio: false,
                plugins: {{ legend: {{ display: false }} }},
                scales: {{ x: {{ ticks: {{ maxRotation: 45 }}, title: {{ display: true, text: '夏普比率' }} }},
                          y: {{ title: {{ display: true, text: '标的数' }} }} }} }}
        }});
    </script>
</body>
</html>"""
        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("批量汇总 HTML 报告已生成: %s", output_path)
        return str(output_path)

    # ─────────────────────────────────────────────
    # PDF 报告
    # ─────────────────────────────────────────────

    def generate_pdf(
        self,
        rows: Sequence[Dict[str, Any]],
        meta: BatchReportMeta,
        output_path: Path,
    ) -> str:
        """生成 PDF 汇总报告（fpdf2 离线渲染）。

        Args:
            rows: 批量汇总行列表。
            meta: 报告元信息。
            output_path: 输出路径。

        Returns:
            str: 报告文件路径。
        """
        (
            returns, sharpes, completed, errors,
            avg_return, positive_ratio, avg_sharpe, avg_drawdown, avg_trades,
        ) = self._compute_stats(rows)
        top_rows = self._top_rows(rows)

        pdf = _ReportPdf(self.title)
        if not PdfReportGenerator._load_font(pdf):
            logger.warning("未找到中文字体，PDF 中文可能无法正常显示")
        self._render_pdf_title(pdf, meta)
        self._render_pdf_stats(pdf, meta, completed, errors,
                               avg_return, positive_ratio, avg_sharpe,
                               avg_drawdown, avg_trades)
        self._render_pdf_histogram(pdf, returns, "总收益率分布", (39, 174, 96), ".1%")
        self._render_pdf_histogram(pdf, sharpes, "夏普比率分布", (102, 126, 234), ".2f")
        self._render_pdf_top_table(pdf, top_rows)
        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)
        pdf.output(str(output_path))
        logger.info("批量汇总 PDF 报告已生成: %s", output_path)
        return str(output_path)

    def _render_pdf_title(self, pdf: _ReportPdf, meta: BatchReportMeta) -> None:
        """PDF 标题区与元信息。"""
        pdf.add_page()
        pdf.set_font("cjk", "", 16)
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(0, 14, self.title, border=0, fill=True, align="C")
        pdf.ln(18)
        schedule = "强制全量" if meta.force_full else "增量"
        meta_rows: List[Tuple[str, str]] = [
            ("策略名称", meta.strategy_name),
            ("回测期间", f"{meta.start_date} ~ {meta.end_date}"),
            ("初始资金", f"{meta.initial_capital:,.0f}"),
            ("引擎模式", meta.base_mode),
            ("调度模式", schedule),
            ("标的总数", str(meta.total)),
            ("重算 / 复用", f"{meta.recomputed} / {meta.reused}"),
        ]
        pdf.set_font("cjk", "", 9)
        for label, value in meta_rows:
            pdf.set_text_color(140, 140, 140)
            pdf.cell(28, _ROW_H, label)
            pdf.set_text_color(40, 40, 40)
            pdf.cell(0, _ROW_H, value)
            pdf.ln()
        pdf.ln(4)

    def _render_pdf_stats(
        self, pdf: _ReportPdf, meta: BatchReportMeta,
        completed: int, errors: int,
        avg_return: float, positive_ratio: float,
        avg_sharpe: float, avg_drawdown: float, avg_trades: float,
    ) -> None:
        """PDF 批量统计表。"""
        self._pdf_section_title(pdf, "批量统计")
        rows: List[Tuple[str, str]] = [
            ("完成 / 失败标的", f"{completed} / {errors}"),
            ("平均收益", f"{avg_return:.2%}"),
            ("正收益占比", f"{positive_ratio:.2%}"),
            ("平均夏普", f"{avg_sharpe:.4f}"),
            ("平均最大回撤", f"{avg_drawdown:.2%}"),
            ("平均交易次数", f"{avg_trades:.1f}"),
        ]
        self._pdf_key_value_table(pdf, rows)

    def _render_pdf_histogram(
        self, pdf: _ReportPdf, values: Sequence[float],
        title: str, color: Tuple[int, int, int], fmt: str,
    ) -> None:
        """PDF 直方图（fpdf2 矩形柱，仿 pdf_report 渲染）。"""
        self._pdf_section_title(pdf, f"{title}（{len(values)} 只完成标的）")
        arr = np.asarray(values, dtype=float)
        if len(arr) == 0:
            pdf.set_font("cjk", "", 9)
            pdf.cell(0, _ROW_H, "无有效数据，无法绘制")
            pdf.ln(6)
            return
        counts, edges = np.histogram(arr, bins=HIST_BINS)
        if counts.max() == 0:
            return
        if pdf.get_y() + _PLOT_H + 10 > _PAGE_H - 15:
            pdf.add_page()
        x0, y0 = _MARGIN, pdf.get_y()
        pdf.set_draw_color(200, 200, 200)
        pdf.rect(x0, y0, _PLOT_W, _PLOT_H)
        bar_w = _PLOT_W / len(counts)
        pdf.set_fill_color(*color)
        for i, c in enumerate(counts):
            bh = (c / counts.max()) * (_PLOT_H - 8)
            pdf.rect(x0 + i * bar_w, y0 + _PLOT_H - 4 - bh, bar_w * 0.8, bh, style="F")
        pdf.set_font("cjk", "", 7)
        pdf.set_text_color(150, 150, 150)
        pdf.set_xy(x0, y0 + _PLOT_H - 4)
        pdf.cell(60, 4, f"min {edges[0]:{fmt}}", align="L")
        pdf.set_xy(x0 + _PLOT_W - 60, y0 + _PLOT_H - 4)
        pdf.cell(60, 4, f"max {edges[-1]:{fmt}}", align="R")
        pdf.set_y(y0 + _PLOT_H + 10)

    def _render_pdf_top_table(
        self, pdf: _ReportPdf, top_rows: Sequence[Dict[str, Any]],
    ) -> None:
        """PDF Top N 表（自动分页，表头随页重复）。"""
        self._pdf_section_title(pdf, f"Top {len(top_rows)}（按总收益降序）")
        headers = ["#", "标的", "状态", "总收益", "夏普", "回撤", "交易数", "来源"]
        widths = [10, 34, 18, 26, 26, 26, 22, 28]
        aligns = ["C", "L", "C", "R", "R", "R", "R", "L"]

        def _header_row() -> None:
            pdf.set_font("cjk", "", 8)
            pdf.set_fill_color(102, 126, 234)
            pdf.set_text_color(255, 255, 255)
            for i, h in enumerate(headers):
                pdf.cell(widths[i], 6, h, border=1, fill=True, align="C")
            pdf.ln()
            pdf.set_text_color(40, 40, 40)

        _header_row()
        pdf.set_font("cjk", "", 8)
        for i, row in enumerate(top_rows, start=1):
            if pdf.get_y() > _PAGE_H - _TABLE_BOTTOM_MARGIN:
                pdf.add_page()
                _header_row()
                pdf.set_font("cjk", "", 8)
            ret = _to_float(row.get("total_return"))
            sharpe = _to_float(row.get("sharpe_ratio"))
            dd = _to_float(row.get("max_drawdown"))
            tc = row.get("trade_count")
            vals = [
                str(i), str(row.get("symbol", "")),
                "完成" if row.get("status") == "completed" else str(row.get("status", "")),
                f"{ret:.2%}" if ret is not None else "-",
                f"{sharpe:.4f}" if sharpe is not None else "-",
                f"{dd:.2%}" if dd is not None else "-",
                str(tc) if tc is not None else "-",
                str(row.get("source", "-")),
            ]
            if (ret or 0) < 0:
                pdf.set_text_color(231, 76, 60)
            elif (ret or 0) > 0:
                pdf.set_text_color(39, 174, 96)
            else:
                pdf.set_text_color(40, 40, 40)
            for j, v in enumerate(vals):
                pdf.cell(widths[j], 6, v, border=1, align=aligns[j])
            pdf.ln()
        pdf.set_text_color(40, 40, 40)

    # ─────────────────────────────────────────────
    # PDF 绘制工具
    # ─────────────────────────────────────────────

    def _pdf_section_title(self, pdf: _ReportPdf, text: str) -> None:
        """PDF 区块小标题（触底自动分页）。"""
        if pdf.get_y() > _PAGE_H - 40:
            pdf.add_page()
        pdf.set_font("cjk", "", 12)
        pdf.set_text_color(44, 62, 80)
        pdf.set_fill_color(102, 126, 234)
        pdf.cell(2, 8, "", fill=True)
        pdf.cell(4, 8, "")
        pdf.cell(0, 8, text)
        pdf.ln(12)

    @staticmethod
    def _pdf_key_value_table(
        pdf: _ReportPdf, rows: Sequence[Tuple[str, str]],
    ) -> None:
        """PDF 两列键值表（交替底色）。"""
        pdf.set_font("cjk", "", 9)
        for i, (k, v) in enumerate(rows):
            pdf.set_fill_color(248, 249, 250) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            pdf.set_text_color(120, 120, 120)
            pdf.cell(52, _ROW_H, k, border=1, fill=True, align="L")
            pdf.set_text_color(40, 40, 40)
            pdf.cell(138, _ROW_H, v, border=1, fill=True, align="L")
            pdf.ln()
        pdf.ln(2)
