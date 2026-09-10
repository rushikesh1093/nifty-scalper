"""Phase 2: market study, paper trading, strategy signals, trading config

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-02 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── vix_data ─────────────────────────────────────────────────────────────
    op.create_table(
        "vix_data",
        sa.Column("id",         sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column("date",       sa.Date(),     nullable=False, unique=True),
        sa.Column("open",       sa.Float(),    nullable=True),
        sa.Column("high",       sa.Float(),    nullable=True),
        sa.Column("low",        sa.Float(),    nullable=True),
        sa.Column("close",      sa.Float(),    nullable=True),
        sa.Column("change_pct", sa.Float(),    nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_vix_date", "vix_data", ["date"])

    # ── market_daily_snapshots ────────────────────────────────────────────────
    op.create_table(
        "market_daily_snapshots",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("date",             sa.Date(),       nullable=False),
        sa.Column("underlying",       sa.String(20),   nullable=False),
        sa.Column("open",             sa.Float(),      nullable=True),
        sa.Column("high",             sa.Float(),      nullable=True),
        sa.Column("low",              sa.Float(),      nullable=True),
        sa.Column("close",            sa.Float(),      nullable=True),
        sa.Column("prev_close",       sa.Float(),      nullable=True),
        sa.Column("change_pct",       sa.Float(),      nullable=True),
        sa.Column("india_vix",        sa.Float(),      nullable=True),
        sa.Column("atm_iv",           sa.Float(),      nullable=True),
        sa.Column("iv_percentile",    sa.Float(),      nullable=True),
        sa.Column("hv_20",            sa.Float(),      nullable=True),
        sa.Column("total_ce_oi",      sa.BigInteger(), nullable=True),
        sa.Column("total_pe_oi",      sa.BigInteger(), nullable=True),
        sa.Column("pcr_oi",           sa.Float(),      nullable=True),
        sa.Column("pcr_volume",       sa.Float(),      nullable=True),
        sa.Column("max_pain",         sa.Float(),      nullable=True),
        sa.Column("max_oi_ce_strike", sa.Float(),      nullable=True),
        sa.Column("max_oi_pe_strike", sa.Float(),      nullable=True),
        sa.Column("trend",            sa.String(10),   nullable=True),
        sa.Column("atm_strike",       sa.Float(),      nullable=True),
        sa.Column("nearest_expiry",   sa.Date(),       nullable=True),
        sa.Column("days_to_expiry",   sa.Integer(),    nullable=True),
        sa.Column("orb_high",         sa.Float(),      nullable=True),
        sa.Column("orb_low",          sa.Float(),      nullable=True),
        sa.Column("vwap_close",       sa.Float(),      nullable=True),
        sa.Column("pdh",              sa.Float(),      nullable=True),
        sa.Column("pdl",              sa.Float(),      nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mds_underlying_date", "market_daily_snapshots",
                    ["underlying", "date"], unique=True)

    # ── intraday_market_features ──────────────────────────────────────────────
    op.create_table(
        "intraday_market_features",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_time",    sa.DateTime(timezone=True), nullable=False),
        sa.Column("underlying",       sa.String(20),   nullable=False),
        sa.Column("spot_ltp",         sa.Float(),      nullable=True),
        sa.Column("spot_change_pct",  sa.Float(),      nullable=True),
        sa.Column("vwap",             sa.Float(),      nullable=True),
        sa.Column("spot_vs_vwap_pct", sa.Float(),      nullable=True),
        sa.Column("atm_strike",       sa.Float(),      nullable=True),
        sa.Column("atm_ce_ltp",       sa.Float(),      nullable=True),
        sa.Column("atm_pe_ltp",       sa.Float(),      nullable=True),
        sa.Column("atm_ce_iv",        sa.Float(),      nullable=True),
        sa.Column("atm_pe_iv",        sa.Float(),      nullable=True),
        sa.Column("atm_straddle_val", sa.Float(),      nullable=True),
        sa.Column("atm_ce_spread",    sa.Float(),      nullable=True),
        sa.Column("atm_pe_spread",    sa.Float(),      nullable=True),
        sa.Column("atm_ce_volume",    sa.BigInteger(), nullable=True),
        sa.Column("atm_pe_volume",    sa.BigInteger(), nullable=True),
        sa.Column("ce_oi_change",     sa.BigInteger(), nullable=True),
        sa.Column("pe_oi_change",     sa.BigInteger(), nullable=True),
        sa.Column("pcr_oi",           sa.Float(),      nullable=True),
        sa.Column("spot_5m_return",   sa.Float(),      nullable=True),
        sa.Column("spot_15m_return",  sa.Float(),      nullable=True),
        sa.Column("vix_snapshot",     sa.Float(),      nullable=True),
        sa.Column("ema9",             sa.Float(),      nullable=True),
        sa.Column("ema21",            sa.Float(),      nullable=True),
        sa.Column("rsi14",            sa.Float(),      nullable=True),
        sa.Column("supertrend",       sa.Float(),      nullable=True),
        sa.Column("supertrend_dir",   sa.String(5),    nullable=True),
        sa.Column("bb_upper",         sa.Float(),      nullable=True),
        sa.Column("bb_lower",         sa.Float(),      nullable=True),
        sa.Column("bb_mid",           sa.Float(),      nullable=True),
        sa.Column("orb_high",         sa.Float(),      nullable=True),
        sa.Column("orb_low",          sa.Float(),      nullable=True),
        sa.Column("orb_broken",       sa.String(5),    nullable=True),
        sa.Column("chain_snapshot",   postgresql.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_imf_underlying_ts", "intraday_market_features",
                    ["underlying", "snapshot_time"])

    # ── strategy_signals ──────────────────────────────────────────────────────
    op.create_table(
        "strategy_signals",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy_name",    sa.String(50),   nullable=False),
        sa.Column("underlying",       sa.String(20),   nullable=False),
        sa.Column("direction",        sa.Enum("BUY","SELL", name="signaldirection"), nullable=False),
        sa.Column("status",           sa.Enum("GENERATED","SELECTED","ENTERED","SL_HIT",
                                              "TARGET_HIT","EXPIRED","SKIPPED",
                                              name="signalstatus"),
                  nullable=False, server_default="GENERATED"),
        sa.Column("spot_price",       sa.Float(),      nullable=False),
        sa.Column("vwap",             sa.Float(),      nullable=True),
        sa.Column("signal_time",      sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry",           sa.Date(),       nullable=True),
        sa.Column("suggested_strike", sa.Float(),      nullable=True),
        sa.Column("suggested_token",  sa.String(20),   nullable=True),
        sa.Column("suggested_symbol", sa.String(60),   nullable=True),
        sa.Column("entry_price_ref",  sa.Float(),      nullable=True),
        sa.Column("stop_loss_ref",    sa.Float(),      nullable=True),
        sa.Column("target_ref",       sa.Float(),      nullable=True),
        sa.Column("risk_reward",      sa.Float(),      nullable=True),
        sa.Column("max_loss_pts",     sa.Float(),      nullable=True),
        sa.Column("indicators",       postgresql.JSON(), nullable=True),
        sa.Column("mode",             sa.Enum("PAPER","LIVE", name="tradingmode"),
                  nullable=False, server_default="PAPER"),
        sa.Column("trade_id",         sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_sig_strategy_time",   "strategy_signals", ["strategy_name","signal_time"])
    op.create_index("ix_sig_underlying_time", "strategy_signals", ["underlying","signal_time"])

    # ── paper_positions ───────────────────────────────────────────────────────
    op.create_table(
        "paper_positions",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy_name",    sa.String(50),   nullable=True),
        sa.Column("signal_id",        sa.BigInteger(), nullable=True),
        sa.Column("token",            sa.String(20),   nullable=False),
        sa.Column("symbol",           sa.String(60),   nullable=False),
        sa.Column("underlying",       sa.String(20),   nullable=False),
        sa.Column("strike",           sa.Float(),      nullable=True),
        sa.Column("option_type",      sa.String(5),    nullable=True),
        sa.Column("expiry",           sa.Date(),       nullable=True),
        sa.Column("exchange",         sa.String(10),   nullable=False, server_default="NFO"),
        sa.Column("direction",        sa.Enum("BUY","SELL", name="signaldirection"),  nullable=False),
        sa.Column("lots",             sa.Integer(),    nullable=False, server_default="1"),
        sa.Column("lot_size",         sa.Integer(),    nullable=False, server_default="50"),
        sa.Column("quantity",         sa.Integer(),    nullable=False),
        sa.Column("entry_price",      sa.Float(),      nullable=False),
        sa.Column("entry_slippage",   sa.Float(),      nullable=False, server_default="0"),
        sa.Column("actual_entry",     sa.Float(),      nullable=False),
        sa.Column("exit_price",       sa.Float(),      nullable=True),
        sa.Column("exit_slippage",    sa.Float(),      nullable=True),
        sa.Column("actual_exit",      sa.Float(),      nullable=True),
        sa.Column("exit_reason",      sa.String(20),   nullable=True),
        sa.Column("stop_loss",        sa.Float(),      nullable=True),
        sa.Column("target",           sa.Float(),      nullable=True),
        sa.Column("trailing_sl",      sa.Float(),      nullable=True),
        sa.Column("max_loss_per_lot", sa.Float(),      nullable=True),
        sa.Column("gross_pnl",        sa.Float(),      nullable=True),
        sa.Column("brokerage",        sa.Float(),      nullable=True, server_default="0"),
        sa.Column("net_pnl",          sa.Float(),      nullable=True),
        sa.Column("is_open",          sa.Boolean(),    nullable=False, server_default="true"),
        sa.Column("mode",             sa.Enum("PAPER","LIVE", name="tradingmode"), nullable=False, server_default="PAPER"),
        sa.Column("entry_time",       sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time",        sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",       sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_pp_strategy_open",   "paper_positions", ["strategy_name","is_open"])
    op.create_index("ix_pp_token_open",      "paper_positions", ["token","is_open"])
    op.create_index("ix_pp_underlying_date", "paper_positions", ["underlying","entry_time"])

    # ── daily_pnl_summary ─────────────────────────────────────────────────────
    op.create_table(
        "daily_pnl_summary",
        sa.Column("id",             sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("date",           sa.Date(),       nullable=False),
        sa.Column("strategy_name",  sa.String(50),   nullable=False),
        sa.Column("mode",           sa.Enum("PAPER","LIVE", name="tradingmode"), nullable=False),
        sa.Column("total_trades",   sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("losing_trades",  sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("win_rate",       sa.Float(),      nullable=True),
        sa.Column("gross_pnl",      sa.Float(),      nullable=False, server_default="0"),
        sa.Column("brokerage",      sa.Float(),      nullable=False, server_default="0"),
        sa.Column("net_pnl",        sa.Float(),      nullable=False, server_default="0"),
        sa.Column("max_drawdown",   sa.Float(),      nullable=True),
        sa.Column("avg_rr",         sa.Float(),      nullable=True),
        sa.Column("total_lots",     sa.Integer(),    nullable=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dpnl_date_strategy", "daily_pnl_summary",
                    ["date","strategy_name"], unique=True)

    # ── trading_config ────────────────────────────────────────────────────────
    op.create_table(
        "trading_config",
        sa.Column("id",                  sa.Integer(),  primary_key=True, server_default="1"),
        sa.Column("mode",                sa.Enum("PAPER","LIVE", name="tradingmode"),
                  nullable=False, server_default="PAPER"),
        sa.Column("max_daily_loss",      sa.Float(),    nullable=False, server_default="5000"),
        sa.Column("max_daily_loss_pct",  sa.Float(),    nullable=False, server_default="2"),
        sa.Column("max_open_positions",  sa.Integer(),  nullable=False, server_default="3"),
        sa.Column("max_lots_per_trade",  sa.Integer(),  nullable=False, server_default="1"),
        sa.Column("capital",             sa.Float(),    nullable=False, server_default="100000"),
        sa.Column("slippage_pct",        sa.Float(),    nullable=False, server_default="0.2"),
        sa.Column("strategy_flags",      postgresql.JSON(), nullable=True),
        sa.Column("square_off_time",     sa.String(5),  nullable=False, server_default="15:15"),
        sa.Column("live_mode_confirmed", sa.Boolean(),  nullable=False, server_default="false"),
        sa.Column("updated_at",          sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Default config row is inserted by scripts/seed_config.py after migration


def downgrade() -> None:
    for t in ["trading_config","daily_pnl_summary","paper_positions",
              "strategy_signals","intraday_market_features",
              "market_daily_snapshots","vix_data"]:
        op.drop_table(t)
    for e in ["signaldirection","signalstatus","tradingmode"]:
        op.execute(f"DROP TYPE IF EXISTS {e};")
