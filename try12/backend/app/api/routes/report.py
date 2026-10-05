"""
Trade Report — PDF download endpoint.

GET /trading/report/pdf
  ?mode=PAPER|LIVE|ALL   (default ALL)
  &days=30               (default 30, max 365)
  &date_from=YYYY-MM-DD  (optional, overrides days)
  &date_to=YYYY-MM-DD    (optional, defaults to today)

Generates a professional multi-section PDF:
  1. Cover header with branding + period summary
  2. KPI cards row (8 key metrics)
  3. Per-strategy breakdown table
  4. Daily P&L table with cumulative column
  5. Full trade log (all closed positions)
  6. Footer on every page
"""
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.db.models.paper_trade import (
    PaperPosition, DailyPnLSummary, TradingConfig, TradingMode,
)
from app.services.trading.mode_manager import mode_manager

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak,
)
from reportlab.platypus.flowables import Flowable

router = APIRouter(prefix="/trading", tags=["trading"])
logger = logging.getLogger(__name__)

# ── Palette ───────────────────────────────────────────────────────────────────
C_BG         = colors.HexColor("#0d0f16")   # near-black page background
C_SURFACE    = colors.HexColor("#161922")   # card / table row background
C_SURFACE2   = colors.HexColor("#1c2030")   # alternate row
C_BORDER     = colors.HexColor("#2a2f45")   # subtle border
C_ACCENT     = colors.HexColor("#6366f1")   # indigo brand accent
C_ACCENT2    = colors.HexColor("#818cf8")   # lighter indigo for headings
C_GREEN      = colors.HexColor("#22c55e")
C_GREEN_DIM  = colors.HexColor("#14532d")
C_RED        = colors.HexColor("#ef4444")
C_RED_DIM    = colors.HexColor("#7f1d1d")
C_YELLOW     = colors.HexColor("#f59e0b")
C_YELLOW_DIM = colors.HexColor("#78350f")
C_WHITE      = colors.HexColor("#f1f5f9")
C_GRAY1      = colors.HexColor("#94a3b8")   # secondary text
C_GRAY2      = colors.HexColor("#475569")   # muted text / dividers
C_HEADER_BG  = colors.HexColor("#1e2235")   # table header background

IST = timezone(timedelta(hours=5, minutes=30))
PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm
USABLE_W = PAGE_W - 2 * MARGIN


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pnl_color(val: float) -> colors.Color:
    if val > 0:  return C_GREEN
    if val < 0:  return C_RED
    return C_GRAY1


def _fmt_inr(v: Optional[float], sign: bool = True) -> str:
    if v is None: return "—"
    s = "+" if (sign and v > 0) else ""
    return f"{s}₹{abs(v):,.2f}" if v < 0 else f"{s}₹{v:,.2f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None: return "—"
    s = "+" if v > 0 else ""
    return f"{s}{v:.1f}%"


def _ist(dt: Optional[datetime]) -> str:
    if dt is None: return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %y %H:%M")


def _ist_date(dt: Optional[datetime]) -> str:
    if dt is None: return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%d %b %Y")


# ── Styled paragraph helpers ──────────────────────────────────────────────────

def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(str(text), style)


def _styles() -> dict:
    return {
        "title": ParagraphStyle("title",
            fontSize=26, textColor=C_WHITE, fontName="Helvetica-Bold",
            alignment=TA_LEFT, spaceAfter=1, leading=30),
        "subtitle": ParagraphStyle("subtitle",
            fontSize=10, textColor=C_GRAY1, fontName="Helvetica",
            alignment=TA_LEFT, spaceAfter=2),
        "section": ParagraphStyle("section",
            fontSize=11, textColor=C_ACCENT2, fontName="Helvetica-Bold",
            spaceBefore=6, spaceAfter=3, leading=14),
        "kpi_label": ParagraphStyle("kpi_label",
            fontSize=8, textColor=C_GRAY1, fontName="Helvetica",
            alignment=TA_CENTER, leading=10),
        "kpi_value": ParagraphStyle("kpi_value",
            fontSize=16, textColor=C_WHITE, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=20),
        "kpi_value_green": ParagraphStyle("kpi_value_green",
            fontSize=16, textColor=C_GREEN, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=20),
        "kpi_value_red": ParagraphStyle("kpi_value_red",
            fontSize=16, textColor=C_RED, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=20),
        "kpi_value_yellow": ParagraphStyle("kpi_value_yellow",
            fontSize=16, textColor=C_YELLOW, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=20),
        "note": ParagraphStyle("note",
            fontSize=7.5, textColor=C_GRAY2, fontName="Helvetica-Oblique",
            alignment=TA_CENTER),
        "cell": ParagraphStyle("cell",
            fontSize=8, textColor=C_WHITE, fontName="Helvetica",
            alignment=TA_LEFT, leading=10),
        "cell_c": ParagraphStyle("cell_c",
            fontSize=8, textColor=C_WHITE, fontName="Helvetica",
            alignment=TA_CENTER, leading=10),
        "cell_r": ParagraphStyle("cell_r",
            fontSize=8, textColor=C_WHITE, fontName="Helvetica",
            alignment=TA_RIGHT, leading=10),
        "hdr": ParagraphStyle("hdr",
            fontSize=8, textColor=C_GRAY1, fontName="Helvetica-Bold",
            alignment=TA_CENTER, leading=10),
        "hdr_l": ParagraphStyle("hdr_l",
            fontSize=8, textColor=C_GRAY1, fontName="Helvetica-Bold",
            alignment=TA_LEFT, leading=10),
    }


# ── KPI card row builder ──────────────────────────────────────────────────────

def _kpi_table(kpis: list[tuple[str, str, str]], width: float) -> Table:
    """
    kpis = [(label, value, color_key), ...]
    color_key: 'white' | 'green' | 'red' | 'yellow'
    """
    s = _styles()
    color_map = {
        "white":  s["kpi_value"],
        "green":  s["kpi_value_green"],
        "red":    s["kpi_value_red"],
        "yellow": s["kpi_value_yellow"],
    }
    n = len(kpis)
    cell_w = width / n
    header_row = [_p(label,  s["kpi_label"]) for label, _, _ in kpis]
    value_row  = [_p(value,  color_map.get(ck, s["kpi_value"]))
                  for _, value, ck in kpis]
    data = [header_row, value_row]
    t = Table(data, colWidths=[cell_w] * n)
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_SURFACE),
        ("ROWBACKGROUNDS",(0, 0), (-1, -1), [C_SURFACE, C_SURFACE]),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("LINEAFTER",     (0, 0), (-2, -1), 0.5, C_BORDER),
        ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


# ── Generic table builder ─────────────────────────────────────────────────────

def _data_table(
    rows: list[list],
    col_widths: list[float],
    pnl_cols: list[int] = None,
    pnl_vals: list[float] = None,
    left_cols: list[int] = None,
) -> Table:
    """
    rows[0]   = header row (plain strings)
    rows[1:]  = data rows (plain strings)
    pnl_cols  = column indices to colour by pnl_vals
    pnl_vals  = float values matching len(rows)-1 for those columns
    left_cols = column indices to left-align
    """
    s = _styles()
    left_cols = left_cols or []

    styled_rows = []
    for ri, row in enumerate(rows):
        styled_row = []
        for ci, cell in enumerate(row):
            if ri == 0:
                st = s["hdr_l"] if ci in left_cols else s["hdr"]
            else:
                st = s["cell"] if ci in left_cols else s["cell_c"]
            styled_row.append(_p(str(cell), st))
        styled_rows.append(styled_row)

    t = Table(styled_rows, colWidths=col_widths, repeatRows=1)

    style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  C_HEADER_BG),
        ("LINEBELOW",     (0, 0), (-1, 0),  0.8, C_ACCENT),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("LINEBELOW",     (0, 1), (-1, -1), 0.3, C_BORDER),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]

    # Alternating row backgrounds
    for i in range(1, len(rows)):
        bg = C_SURFACE if i % 2 == 1 else C_SURFACE2
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))

    # P&L column colouring
    if pnl_cols and pnl_vals:
        for ci in pnl_cols:
            for ri, val in enumerate(pnl_vals, 1):
                style_cmds.append(("TEXTCOLOR", (ci, ri), (ci, ri), _pnl_color(val)))

    t.setStyle(TableStyle(style_cmds))
    return t


# ── Page background + footer on every page ────────────────────────────────────

def _on_page(canvas, doc):
    canvas.saveState()
    # Solid dark background
    canvas.setFillColor(C_BG)
    canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    # Thin accent bar at very top
    canvas.setFillColor(C_ACCENT)
    canvas.rect(0, PAGE_H - 2.5, PAGE_W, 2.5, fill=1, stroke=0)
    # Footer divider
    canvas.setStrokeColor(C_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 10 * mm, PAGE_W - MARGIN, 10 * mm)
    # Footer text
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(C_GRAY2)
    canvas.drawString(MARGIN, 7 * mm, "Nifty Scalper — Automated Options Trading System")
    canvas.drawRightString(PAGE_W - MARGIN, 7 * mm,
                           f"Page {doc.page}  |  Generated {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}")
    canvas.restoreState()


# ── Main PDF builder ──────────────────────────────────────────────────────────

def _build_pdf(
    positions:   list,
    daily_rows:  list,
    cfg:         Optional[TradingConfig],
    date_from:   date,
    date_to:     date,
    mode_filter: str,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN + 4 * mm,
        bottomMargin=14 * mm,
        title="Nifty Scalper — Trade Report",
        author="Nifty Scalper",
    )

    s    = _styles()
    W    = USABLE_W
    story = []

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 1 — Cover header
    # ═══════════════════════════════════════════════════════════════════════════
    period_str = (f"{date_from.strftime('%d %b %Y')}  →  "
                  f"{date_to.strftime('%d %b %Y')}")
    mode_label = {"PAPER": "Paper Trading", "LIVE": "Live Trading",
                  "ALL": "All Modes"}.get(mode_filter, mode_filter)

    # Two-column header: title left, metadata right
    header_data = [[
        _p("Nifty Scalper", s["title"]),
        _p(f"<b>Period</b><br/>{period_str}<br/>"
           f"<b>Mode</b>  {mode_label}",
           ParagraphStyle("meta", fontSize=9, textColor=C_GRAY1,
                          fontName="Helvetica", alignment=TA_RIGHT,
                          leading=14)),
    ]]
    header_t = Table(header_data, colWidths=[W * 0.6, W * 0.4])
    header_t.setStyle(TableStyle([
        ("VALIGN",    (0, 0), (-1, -1), "BOTTOM"),
        ("TOPPADDING",(0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(header_t)
    story.append(Spacer(1, 2 * mm))
    story.append(HRFlowable(width=W, thickness=1.5, color=C_ACCENT, spaceAfter=5))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 2 — KPI cards
    # ═══════════════════════════════════════════════════════════════════════════
    closed        = [p for p in positions if not p.is_open]
    total_trades  = len(closed)
    wins          = sum(1 for p in closed if (p.net_pnl or 0) > 0)
    losses        = total_trades - wins
    win_rate      = wins / total_trades * 100 if total_trades else 0.0
    gross_pnl     = sum(p.gross_pnl or 0 for p in closed)
    brokerage_tot = sum(p.brokerage or 0 for p in closed)
    net_pnl       = sum(p.net_pnl  or 0 for p in closed)
    capital       = cfg.capital if cfg else 0.0
    net_pct       = net_pnl / capital * 100 if capital else 0.0

    rr_vals = []
    for p in closed:
        if p.target and p.actual_entry and p.stop_loss:
            denom = p.actual_entry - p.stop_loss
            if denom > 0:
                rr_vals.append((p.target - p.actual_entry) / denom)
    avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else None

    # Best / worst single day
    by_date: dict[str, float] = {}
    for r in daily_rows:
        d = str(r.date)
        by_date[d] = by_date.get(d, 0.0) + (r.net_pnl or 0)
    best_day  = max(by_date.values()) if by_date else 0.0
    worst_day = min(by_date.values()) if by_date else 0.0

    def _kc(val: float) -> str:
        if val > 0: return "green"
        if val < 0: return "red"
        return "white"

    kpis = [
        ("Total Trades",   str(total_trades),                    "white"),
        ("Win Rate",        _fmt_pct(win_rate),                   _kc(win_rate - 50)),
        ("Wins / Losses",  f"{wins} / {losses}",                  "white"),
        ("Net P&L",         _fmt_inr(net_pnl),                    _kc(net_pnl)),
        ("Return %",        _fmt_pct(net_pct),                    _kc(net_pct)),
        ("Avg R:R",         f"{avg_rr:.2f}" if avg_rr else "—",  "yellow"),
        ("Best Day",        _fmt_inr(best_day),                   _kc(best_day)),
        ("Worst Day",       _fmt_inr(worst_day),                  _kc(worst_day)),
    ]
    story.append(_kpi_table(kpis, W))
    story.append(Spacer(1, 5 * mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 3 — Strategy breakdown
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(_p("Strategy Performance", s["section"]))

    strat_map: dict[str, dict] = {}
    for p in closed:
        k = p.strategy_name or "UNKNOWN"
        if k not in strat_map:
            strat_map[k] = {"trades": 0, "wins": 0, "net": 0.0,
                             "gross": 0.0, "brok": 0.0, "rr_sum": 0.0, "rr_n": 0}
        strat_map[k]["trades"] += 1
        strat_map[k]["wins"]   += 1 if (p.net_pnl or 0) > 0 else 0
        strat_map[k]["net"]    += p.net_pnl   or 0
        strat_map[k]["gross"]  += p.gross_pnl or 0
        strat_map[k]["brok"]   += p.brokerage or 0
        if p.target and p.actual_entry and p.stop_loss:
            denom = p.actual_entry - p.stop_loss
            if denom > 0:
                strat_map[k]["rr_sum"] += (p.target - p.actual_entry) / denom
                strat_map[k]["rr_n"]   += 1

    if strat_map:
        s_header = ["Strategy", "Trades", "Wins", "Losses",
                    "Win Rate", "Avg R:R", "Gross P&L", "Brokerage", "Net P&L"]
        s_rows   = [s_header]
        s_net_vals = []
        for name, d in sorted(strat_map.items(), key=lambda x: -x[1]["net"]):
            wr  = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            arr = d["rr_sum"] / d["rr_n"] if d["rr_n"] else None
            s_rows.append([
                name,
                str(d["trades"]),
                str(d["wins"]),
                str(d["trades"] - d["wins"]),
                _fmt_pct(wr),
                f"{arr:.2f}" if arr else "—",
                _fmt_inr(d["gross"]),
                f'-₹{d["brok"]:,.2f}',
                _fmt_inr(d["net"]),
            ])
            s_net_vals.append(d["net"])

        scw = [W*0.18, W*0.06, W*0.06, W*0.06, W*0.08,
               W*0.07, W*0.13, W*0.11, W*0.13]
        story.append(_data_table(s_rows, scw,
                                  pnl_cols=[8], pnl_vals=s_net_vals,
                                  left_cols=[0]))
    else:
        story.append(_p("No closed trades in this period.", s["note"]))

    story.append(Spacer(1, 5 * mm))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 4 — Daily P&L
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(_p("Daily P&L Summary", s["section"]))

    agg: dict[str, dict] = {}
    for r in daily_rows:
        d = str(r.date)
        if d not in agg:
            agg[d] = {"net": 0.0, "gross": 0.0, "trades": 0, "wins": 0}
        agg[d]["net"]    += r.net_pnl or 0
        agg[d]["gross"]  += r.gross_pnl or 0
        agg[d]["trades"] += r.total_trades or 0
        agg[d]["wins"]   += r.winning_trades or 0

    if agg:
        d_header = ["Date", "Trades", "Wins", "Win Rate",
                    "Gross P&L", "Net P&L", "Cumulative P&L"]
        d_rows = [d_header]
        d_net_vals  = []
        d_cum_vals  = []
        cum = 0.0
        for d in sorted(agg.keys()):
            row_d = agg[d]
            cum  += row_d["net"]
            wr    = row_d["wins"] / row_d["trades"] * 100 if row_d["trades"] else 0
            d_rows.append([
                d,
                str(row_d["trades"]),
                str(row_d["wins"]),
                _fmt_pct(wr),
                _fmt_inr(row_d["gross"]),
                _fmt_inr(row_d["net"]),
                _fmt_inr(cum),
            ])
            d_net_vals.append(row_d["net"])
            d_cum_vals.append(cum)

        dcw = [W*0.12, W*0.07, W*0.06, W*0.09,
               W*0.16, W*0.14, W*0.16]

        # Build table manually so we can colour both net and cum columns
        ds = _styles()
        styled = []
        for ri, row in enumerate(d_rows):
            sr = []
            for ci, cell in enumerate(row):
                st = ds["hdr"] if ri == 0 else ds["cell_c"]
                if ri == 0 and ci == 0: st = ds["hdr_l"]
                if ri > 0  and ci == 0: st = ds["cell"]
                sr.append(_p(str(cell), st))
            styled.append(sr)

        dt = Table(styled, colWidths=dcw, repeatRows=1)
        dt_style = [
            ("BACKGROUND",    (0, 0), (-1, 0),  C_HEADER_BG),
            ("LINEBELOW",     (0, 0), (-1, 0),  0.8, C_ACCENT),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("LINEBELOW",     (0, 1), (-1, -1), 0.3, C_BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(d_rows)):
            bg = C_SURFACE if i % 2 == 1 else C_SURFACE2
            dt_style.append(("BACKGROUND", (0, i), (-1, i), bg))
            dt_style.append(("TEXTCOLOR", (5, i), (5, i), _pnl_color(d_net_vals[i-1])))
            dt_style.append(("TEXTCOLOR", (6, i), (6, i), _pnl_color(d_cum_vals[i-1])))
        dt.setStyle(TableStyle(dt_style))
        story.append(dt)
    else:
        story.append(_p("No daily P&L data in this period.", s["note"]))

    story.append(PageBreak())

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 5 — Full trade log
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(_p("Trade Log — Closed Positions", s["section"]))

    if closed:
        t_header = [
            "#", "Strategy", "Symbol",
            "Lots", "Entry ₹", "Exit ₹", "SL ₹", "Target ₹",
            "Gross P&L", "Brok.", "Net P&L",
            "R:R", "Reason", "Entry Time", "Exit Time",
        ]
        t_rows    = [t_header]
        t_net_vals = []

        for idx, p in enumerate(sorted(closed, key=lambda x: x.entry_time or datetime.min), 1):
            rr_str = "—"
            if p.target and p.actual_entry and p.stop_loss:
                denom = p.actual_entry - p.stop_loss
                if denom > 0:
                    rr_str = f"{(p.target - p.actual_entry) / denom:.2f}"

            # Colour-code the exit reason pill
            reason = p.exit_reason or "—"

            t_rows.append([
                str(idx),
                (p.strategy_name or "—")[:16],
                (p.symbol        or "—")[:18],
                str(p.lots or "—"),
                f"₹{p.actual_entry:.2f}" if p.actual_entry else "—",
                f"₹{p.actual_exit:.2f}"  if p.actual_exit  else "—",
                f"₹{p.stop_loss:.2f}"    if p.stop_loss    else "—",
                f"₹{p.target:.2f}"       if p.target       else "—",
                _fmt_inr(p.gross_pnl),
                f'₹{p.brokerage:.0f}'    if p.brokerage    else "—",
                _fmt_inr(p.net_pnl),
                rr_str,
                reason,
                _ist(p.entry_time),
                _ist(p.exit_time),
            ])
            t_net_vals.append(p.net_pnl or 0)

        tcw = [
            W*0.03,  # #
            W*0.10,  # strategy
            W*0.11,  # symbol
            W*0.04,  # lots
            W*0.06,  # entry
            W*0.06,  # exit
            W*0.06,  # sl
            W*0.06,  # target
            W*0.07,  # gross
            W*0.05,  # brok
            W*0.07,  # net
            W*0.04,  # rr
            W*0.07,  # reason
            W*0.09,  # entry time
            W*0.09,  # exit time
        ]

        # Build trade table manually (need per-row net P&L colour)
        ts = _styles()
        styled_t = []
        for ri, row in enumerate(t_rows):
            sr = []
            for ci, cell in enumerate(row):
                st = ts["hdr"] if ri == 0 else ts["cell_c"]
                if ri == 0 and ci in (1, 2):   st = ts["hdr_l"]
                if ri > 0  and ci in (1, 2):   st = ts["cell"]
                sr.append(_p(str(cell), st))
            styled_t.append(sr)

        tt = Table(styled_t, colWidths=tcw, repeatRows=1)
        tt_style = [
            ("BACKGROUND",    (0, 0), (-1, 0),  C_HEADER_BG),
            ("LINEBELOW",     (0, 0), (-1, 0),  0.8, C_ACCENT),
            ("FONTSIZE",      (0, 0), (-1, -1), 7.5),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ("LINEBELOW",     (0, 1), (-1, -1), 0.25, C_BORDER),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(t_rows)):
            bg = C_SURFACE if i % 2 == 1 else C_SURFACE2
            tt_style.append(("BACKGROUND", (0, i), (-1, i), bg))
            net_v = t_net_vals[i - 1]
            tt_style.append(("TEXTCOLOR", (10, i), (10, i), _pnl_color(net_v)))
            # Colour exit reason
            reason_v = t_rows[i][12]
            if reason_v == "TARGET":
                tt_style.append(("TEXTCOLOR", (12, i), (12, i), C_GREEN))
            elif reason_v == "SL":
                tt_style.append(("TEXTCOLOR", (12, i), (12, i), C_RED))
            elif reason_v == "EOD":
                tt_style.append(("TEXTCOLOR", (12, i), (12, i), C_YELLOW))
            elif reason_v == "TIMEOUT":
                tt_style.append(("TEXTCOLOR", (12, i), (12, i), C_GRAY1))

        tt.setStyle(TableStyle(tt_style))
        story.append(tt)
    else:
        story.append(_p("No closed trades in this period.", s["note"]))

    # ═══════════════════════════════════════════════════════════════════════════
    # Footer note
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width=W, thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 2 * mm))
    story.append(_p(
        "All P&L figures are net of simulated brokerage (₹20/lot round-trip). "
        "Paper trades use configurable slippage against live market prices. "
        "This report is for informational and backtesting purposes only.",
        s["note"],
    ))

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    buf.seek(0)
    return buf.read()


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.get("/report/pdf")
async def download_trade_report(
    mode:      str           = Query("ALL", description="PAPER | LIVE | ALL"),
    days:      int           = Query(30,  ge=1, le=365),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to:   Optional[str] = Query(None, description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    """Download a PDF trade report covering all closed positions and daily P&L."""
    d_to   = date.fromisoformat(date_to)   if date_to   else date.today()
    d_from = date.fromisoformat(date_from) if date_from else d_to - timedelta(days=days - 1)
    mode_filter = mode.upper()

    q = (
        select(PaperPosition)
        .where(
            PaperPosition.is_open == False,
            PaperPosition.entry_time >= datetime.combine(d_from, datetime.min.time()),
            PaperPosition.entry_time <= datetime.combine(d_to,   datetime.max.time()),
        )
        .order_by(PaperPosition.entry_time)
    )
    if mode_filter != "ALL":
        try:
            q = q.where(PaperPosition.mode == TradingMode(mode_filter))
        except ValueError:
            pass
    positions = (await db.execute(q)).scalars().all()

    dq = (
        select(DailyPnLSummary)
        .where(DailyPnLSummary.date >= d_from, DailyPnLSummary.date <= d_to)
        .order_by(DailyPnLSummary.date)
    )
    if mode_filter != "ALL":
        try:
            dq = dq.where(DailyPnLSummary.mode == TradingMode(mode_filter))
        except ValueError:
            pass
    daily_rows = (await db.execute(dq)).scalars().all()

    cfg = await mode_manager.get_config()

    import asyncio
    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(
        None, _build_pdf,
        list(positions), list(daily_rows), cfg, d_from, d_to, mode_filter,
    )

    filename = f"nifty_scalper_report_{d_from}_{d_to}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
