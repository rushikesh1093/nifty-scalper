"""Initial schema with TimescaleDB hypertables

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── instruments ─────────────────────────────────────────────────────────
    op.create_table(
        "instruments",
        sa.Column("id",              sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column("token",           sa.String(20),    nullable=False),
        sa.Column("symbol",          sa.String(60),    nullable=False),
        sa.Column("name",            sa.String(30),    nullable=False),
        sa.Column("expiry",          sa.Date(),        nullable=True),
        sa.Column("strike",          sa.Float(),       nullable=True),
        sa.Column("option_type",     sa.Enum("CE", "PE", name="optiontype"), nullable=True),
        sa.Column("instrument_type", sa.Enum("OPTIDX","FUTIDX","INDEX","OPTSTK","FUTSTK","EQ",
                                             name="instrumenttype"), nullable=False),
        sa.Column("lot_size",        sa.Integer(),     nullable=False, server_default="50"),
        sa.Column("tick_size",       sa.Float(),       nullable=False, server_default="0.05"),
        sa.Column("exchange",        sa.String(10),    nullable=False, server_default="NFO"),
        sa.Column("is_active",       sa.Boolean(),     nullable=False, server_default="true"),
        sa.Column("created_at",      sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",      sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token", "exchange", name="uq_token_exchange"),
    )
    op.create_index("ix_inst_name_expiry",   "instruments", ["name", "expiry"])
    op.create_index("ix_inst_type_active",   "instruments", ["instrument_type", "is_active"])
    op.create_index("ix_inst_strike_optype", "instruments", ["strike", "option_type"])

    # ── ticks ────────────────────────────────────────────────────────────────
    # TimescaleDB requires the partition column (timestamp) to be part of
    # any primary key. We use a composite PK (id, timestamp) so TimescaleDB
    # can create the hypertable without error.
    op.create_table(
        "ticks",
        sa.Column("id",           sa.BigInteger(),  nullable=False, autoincrement=True),
        sa.Column("token",        sa.String(20),    nullable=False),
        sa.Column("symbol",       sa.String(60),    nullable=False),
        sa.Column("exchange",     sa.String(10),    nullable=False),
        sa.Column("ltp",          sa.Float(),       nullable=False),
        sa.Column("open",         sa.Float(),       nullable=True),
        sa.Column("high",         sa.Float(),       nullable=True),
        sa.Column("low",          sa.Float(),       nullable=True),
        sa.Column("close",        sa.Float(),       nullable=True),
        sa.Column("volume",       sa.BigInteger(),  nullable=True),
        sa.Column("oi",           sa.BigInteger(),  nullable=True),
        sa.Column("oi_change",    sa.BigInteger(),  nullable=True),
        sa.Column("best_bid",     sa.Float(),       nullable=True),
        sa.Column("best_ask",     sa.Float(),       nullable=True),
        sa.Column("best_bid_qty", sa.Integer(),     nullable=True),
        sa.Column("best_ask_qty", sa.Integer(),     nullable=True),
        sa.Column("exchange_ts",  sa.BigInteger(),  nullable=True),
        sa.Column("timestamp",    sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "timestamp", name="pk_ticks"),
    )
    # Auto-increment sequence for id (PostgreSQL)
    op.execute("CREATE SEQUENCE IF NOT EXISTS ticks_id_seq OWNED BY ticks.id;")
    op.execute("ALTER TABLE ticks ALTER COLUMN id SET DEFAULT nextval('ticks_id_seq');")
    op.create_index("ix_tick_token_ts",    "ticks", ["token", "timestamp"])
    op.create_index("ix_tick_exchange_ts", "ticks", ["exchange", "timestamp"])

    # Convert ticks to TimescaleDB hypertable (1 day chunks) — skipped on plain PostgreSQL
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('ticks', 'timestamp',
                    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
                ALTER TABLE ticks SET (timescaledb.compress,
                    timescaledb.compress_orderby = 'timestamp DESC',
                    timescaledb.compress_segmentby = 'token');
                PERFORM add_compression_policy('ticks', INTERVAL '7 days', if_not_exists => TRUE);
            END IF;
        END $$;
    """)

    # ── ohlc ─────────────────────────────────────────────────────────────────
    # Composite PK (id, timestamp) required by TimescaleDB for hypertable partitioning.
    op.create_table(
        "ohlc",
        sa.Column("id",         sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("token",      sa.String(20),   nullable=False),
        sa.Column("symbol",     sa.String(60),   nullable=False),
        sa.Column("exchange",   sa.String(10),   nullable=False),
        sa.Column("interval",   sa.String(20),   nullable=False),
        sa.Column("open",       sa.Float(),      nullable=False),
        sa.Column("high",       sa.Float(),      nullable=False),
        sa.Column("low",        sa.Float(),      nullable=False),
        sa.Column("close",      sa.Float(),      nullable=False),
        sa.Column("volume",     sa.BigInteger(), nullable=True),
        sa.Column("oi",         sa.BigInteger(), nullable=True),
        sa.Column("timestamp",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "timestamp", name="pk_ohlc"),
        sa.UniqueConstraint("token", "interval", "timestamp", name="uq_ohlc_token_interval_ts"),
    )
    op.execute("CREATE SEQUENCE IF NOT EXISTS ohlc_id_seq OWNED BY ohlc.id;")
    op.execute("ALTER TABLE ohlc ALTER COLUMN id SET DEFAULT nextval('ohlc_id_seq');")
    op.create_index("ix_ohlc_token_interval_ts", "ohlc", ["token", "interval", "timestamp"])
    op.create_index("ix_ohlc_symbol_interval",   "ohlc", ["symbol", "interval"])

    # Convert ohlc to hypertable (partitioned by timestamp)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('ohlc', 'timestamp',
                    chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
            END IF;
        END $$;
    """)

    # ── option_chain_snapshots ────────────────────────────────────────────────
    # Composite PK (id, snapshot_time) required by TimescaleDB for hypertable partitioning.
    op.create_table(
        "option_chain_snapshots",
        sa.Column("id",             sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("underlying",     sa.String(20),   nullable=False),
        sa.Column("underlying_ltp", sa.Float(),      nullable=False),
        sa.Column("expiry",         sa.Date(),       nullable=False),
        sa.Column("strike",         sa.Float(),      nullable=False),
        sa.Column("option_type",    sa.Enum("CE", "PE", name="optionside"), nullable=False),
        sa.Column("token",          sa.String(20),   nullable=False),
        sa.Column("ltp",            sa.Float(),      nullable=True),
        sa.Column("bid",            sa.Float(),      nullable=True),
        sa.Column("ask",            sa.Float(),      nullable=True),
        sa.Column("iv",             sa.Float(),      nullable=True),
        sa.Column("delta",          sa.Float(),      nullable=True),
        sa.Column("gamma",          sa.Float(),      nullable=True),
        sa.Column("theta",          sa.Float(),      nullable=True),
        sa.Column("vega",           sa.Float(),      nullable=True),
        sa.Column("oi",             sa.BigInteger(), nullable=True),
        sa.Column("oi_change",      sa.BigInteger(), nullable=True),
        sa.Column("volume",         sa.BigInteger(), nullable=True),
        sa.Column("snapshot_time",  sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "snapshot_time", name="pk_option_chain_snapshots"),
    )
    op.execute("CREATE SEQUENCE IF NOT EXISTS option_chain_snapshots_id_seq OWNED BY option_chain_snapshots.id;")
    op.execute("ALTER TABLE option_chain_snapshots ALTER COLUMN id SET DEFAULT nextval('option_chain_snapshots_id_seq');")
    op.create_index("ix_oc_underlying_expiry_ts", "option_chain_snapshots",
                    ["underlying", "expiry", "snapshot_time"])
    op.create_index("ix_oc_strike_type_ts",       "option_chain_snapshots",
                    ["strike", "option_type", "snapshot_time"])
    op.create_index("ix_oc_token_ts",             "option_chain_snapshots",
                    ["token", "snapshot_time"])

    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM create_hypertable('option_chain_snapshots', 'snapshot_time',
                    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            END IF;
        END $$;
    """)

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id",              sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy_name",   sa.String(50),   nullable=True),
        sa.Column("broker_order_id", sa.String(50),   nullable=True),
        sa.Column("token",           sa.String(20),   nullable=False),
        sa.Column("symbol",          sa.String(60),   nullable=False),
        sa.Column("exchange",        sa.String(10),   nullable=False, server_default="NFO"),
        sa.Column("side",         sa.Enum("BUY","SELL", name="orderside"),          nullable=False),
        sa.Column("order_type",   sa.Enum("MARKET","LIMIT","SL","SL-M", name="ordertype"), nullable=False),
        sa.Column("product_type", sa.Enum("INTRADAY","DELIVERY", name="producttype"), nullable=False),
        sa.Column("status",       sa.Enum("PENDING","OPEN","COMPLETE","CANCELLED","REJECTED",
                                          name="orderstatus"), nullable=False, server_default="PENDING"),
        sa.Column("quantity",     sa.Integer(), nullable=False),
        sa.Column("price",        sa.Float(),   nullable=True),
        sa.Column("trigger_price",sa.Float(),   nullable=True),
        sa.Column("avg_price",    sa.Float(),   nullable=True),
        sa.Column("filled_qty",   sa.Integer(), nullable=True, server_default="0"),
        sa.Column("pnl",          sa.Float(),   nullable=True),
        sa.Column("is_paper",     sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("placed_at",    sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at",   sa.DateTime(timezone=True), nullable=True),
        sa.Column("filled_at",    sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_order_broker_id",      "orders", ["broker_order_id"])
    op.create_index("ix_order_strategy_status","orders", ["strategy_name", "status"])
    op.create_index("ix_order_token_placed",   "orders", ["token", "placed_at"])

    # ── trades ────────────────────────────────────────────────────────────────
    op.create_table(
        "trades",
        sa.Column("id",             sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy_name",  sa.String(50),   nullable=True),
        sa.Column("token",          sa.String(20),   nullable=False),
        sa.Column("symbol",         sa.String(60),   nullable=False),
        sa.Column("exchange",       sa.String(10),   nullable=False, server_default="NFO"),
        sa.Column("side",           sa.Enum("LONG","SHORT", name="tradeside"), nullable=False),
        sa.Column("quantity",       sa.Integer(), nullable=False),
        sa.Column("lot_size",       sa.Integer(), nullable=False, server_default="50"),
        sa.Column("lots",           sa.Integer(), nullable=False, server_default="1"),
        sa.Column("entry_price",    sa.Float(),   nullable=False),
        sa.Column("exit_price",     sa.Float(),   nullable=True),
        sa.Column("stop_loss",      sa.Float(),   nullable=True),
        sa.Column("target",         sa.Float(),   nullable=True),
        sa.Column("gross_pnl",      sa.Float(),   nullable=True),
        sa.Column("brokerage",      sa.Float(),   nullable=True, server_default="0.0"),
        sa.Column("net_pnl",        sa.Float(),   nullable=True),
        sa.Column("entry_order_id", sa.BigInteger(), nullable=True),
        sa.Column("exit_order_id",  sa.BigInteger(), nullable=True),
        sa.Column("is_paper",       sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("is_open",        sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("entry_time",     sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",     sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_trade_strategy_entry", "trades", ["strategy_name", "entry_time"])
    op.create_index("ix_trade_token_open",     "trades", ["token", "is_open"])


def downgrade() -> None:
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("option_chain_snapshots")
    op.drop_table("ohlc")
    op.drop_table("ticks")
    op.drop_table("instruments")
    # Drop enums
    for enum_name in ["optiontype","instrumenttype","optionside",
                      "orderside","ordertype","producttype","orderstatus","tradeside"]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name};")
