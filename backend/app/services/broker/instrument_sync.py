"""
Instrument sync service.
Downloads the AngelOne scrip master daily and upserts into the instruments table.
Filters only NFO OPTIDX (Nifty/BankNifty options) + FUTIDX + NSE INDEX.
"""
import logging
from datetime import datetime, date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.services.broker.angel_client import angel_client
from app.db.models.instrument import Instrument, InstrumentType, OptionType

logger = logging.getLogger(__name__)

# Underlyings we care about
TRACKED_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


def _parse_expiry(expiry_str: str) -> Optional[date]:
    """Parse AngelOne expiry strings like '29AUG2024' or '2024-08-29'."""
    if not expiry_str:
        return None
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(expiry_str.upper(), fmt).date()
        except ValueError:
            continue
    return None


def _map_instrument_type(itype: str) -> Optional[InstrumentType]:
    mapping = {
        "OPTIDX": InstrumentType.OPTIDX,
        "FUTIDX": InstrumentType.FUTIDX,
        "INDEX":  InstrumentType.INDEX,
        "OPTSTK": InstrumentType.OPTSTK,
        "FUTSTK": InstrumentType.FUTSTK,
        "EQ":     InstrumentType.EQ,
    }
    return mapping.get(itype.upper())


def _map_option_type(symbol: str) -> Optional[OptionType]:
    sym = symbol.upper()
    if sym.endswith("CE"):
        return OptionType.CE
    if sym.endswith("PE"):
        return OptionType.PE
    return None


async def sync_instruments(db: AsyncSession) -> int:
    """
    Download scrip master from AngelOne and upsert instruments table.
    Returns count of upserted records.
    """
    logger.info("Starting instrument sync from AngelOne scrip master...")
    raw_list = await angel_client.get_instrument_dump()

    to_upsert = []
    for item in raw_list:
        itype_str = item.get("instrumenttype", "")
        itype = _map_instrument_type(itype_str)
        if itype is None:
            continue

        name = item.get("name", "").upper()
        exchange = item.get("exch_seg", "")

        # Filter: only track our underlyings on NFO/NSE
        if itype in (InstrumentType.OPTIDX, InstrumentType.FUTIDX):
            if name not in TRACKED_NAMES:
                continue
            if exchange not in ("NFO", "BSE"):
                continue
        elif itype == InstrumentType.INDEX:
            if name not in TRACKED_NAMES:
                continue
        else:
            # Skip stocks for now
            continue

        symbol = item.get("symbol", "")
        token = item.get("token", "")
        expiry = _parse_expiry(item.get("expiry", ""))
        strike = float(item.get("strike", 0) or 0) / 100  # AngelOne gives strike * 100
        lot_size = int(item.get("lotsize", 50) or 50)
        tick_size = float(item.get("tick_size", 0.05) or 0.05)
        option_type = _map_option_type(symbol) if itype == InstrumentType.OPTIDX else None

        to_upsert.append({
            "token":           token,
            "symbol":          symbol,
            "name":            name,
            "expiry":          expiry,
            "strike":          strike,
            "option_type":     option_type,
            "instrument_type": itype,
            "lot_size":        lot_size,
            "tick_size":       tick_size,
            "exchange":        exchange,
            "is_active":       True,
        })

    if not to_upsert:
        logger.warning("No instruments to upsert after filtering.")
        return 0

    # Batch upsert in chunks of 500
    chunk_size = 500
    total = 0
    for i in range(0, len(to_upsert), chunk_size):
        chunk = to_upsert[i : i + chunk_size]
        stmt = pg_insert(Instrument).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_token_exchange",
            set_={
                "symbol":          stmt.excluded.symbol,
                "name":            stmt.excluded.name,
                "expiry":          stmt.excluded.expiry,
                "strike":          stmt.excluded.strike,
                "option_type":     stmt.excluded.option_type,
                "instrument_type": stmt.excluded.instrument_type,
                "lot_size":        stmt.excluded.lot_size,
                "tick_size":       stmt.excluded.tick_size,
                "is_active":       True,
                "updated_at":      datetime.utcnow(),
            },
        )
        await db.execute(stmt)
        total += len(chunk)

    await db.commit()
    logger.info("Instrument sync complete. Upserted %d records.", total)
    return total


async def get_active_expiries(db: AsyncSession, name: str) -> list[date]:
    """Return sorted list of active expiry dates for an underlying."""
    result = await db.execute(
        select(Instrument.expiry)
        .where(
            Instrument.name == name.upper(),
            Instrument.instrument_type == InstrumentType.OPTIDX,
            Instrument.is_active == True,
            Instrument.expiry >= date.today(),
        )
        .distinct()
        .order_by(Instrument.expiry)
    )
    return [row[0] for row in result.fetchall()]


async def get_option_tokens(
    db: AsyncSession,
    name: str,
    expiry: date,
    strike_range: Optional[tuple[float, float]] = None,
) -> list[Instrument]:
    """
    Fetch CE+PE instruments for a given underlying and expiry.
    Optionally filter by strike range (low, high).
    """
    query = select(Instrument).where(
        Instrument.name == name.upper(),
        Instrument.instrument_type == InstrumentType.OPTIDX,
        Instrument.expiry == expiry,
        Instrument.is_active == True,
    )
    if strike_range:
        query = query.where(
            Instrument.strike >= strike_range[0],
            Instrument.strike <= strike_range[1],
        )
    query = query.order_by(Instrument.strike, Instrument.option_type)
    result = await db.execute(query)
    return result.scalars().all()
