from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from datetime import date
from typing import Optional

from app.db.base import get_db
from app.db.models.instrument import Instrument, InstrumentType, OptionType
from app.schemas.instrument import InstrumentResponse, InstrumentSearchRequest
from app.services.broker.instrument_sync import sync_instruments, get_active_expiries
from app.runtime.context import ctx, trading_day

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("/", response_model=list[InstrumentResponse])
async def list_instruments(
    name:            Optional[str] = Query(None, description="NIFTY or BANKNIFTY"),
    instrument_type: Optional[str] = Query(None, description="OPTIDX | FUTIDX | INDEX"),
    expiry:          Optional[str] = Query(None, description="YYYY-MM-DD"),
    option_type:     Optional[str] = Query(None, description="CE | PE"),
    is_active:       bool          = Query(True),
    limit:           int           = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    query = select(Instrument).where(Instrument.is_active == is_active)

    if name:
        query = query.where(Instrument.name == name.upper())
    if instrument_type:
        try:
            itype = InstrumentType(instrument_type.upper())
            query = query.where(Instrument.instrument_type == itype)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid instrument_type: {instrument_type}")
    if expiry:
        query = query.where(Instrument.expiry == date.fromisoformat(expiry))
    if option_type:
        try:
            otype = OptionType(option_type.upper())
            query = query.where(Instrument.option_type == otype)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid option_type: {option_type}")

    query = query.order_by(Instrument.expiry, Instrument.strike).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/expiries/{underlying}", response_model=list[str])
async def get_expiries(underlying: str, db: AsyncSession = Depends(get_db)):
    """Get all active expiry dates for an underlying (NIFTY, BANKNIFTY)."""
    expiries = await get_active_expiries(db, underlying, as_of=trading_day())
    return [e.isoformat() for e in expiries]


@router.get("/search", response_model=list[InstrumentResponse])
async def search_instruments(
    q:        str = Query(..., min_length=2),
    exchange: str = Query("NFO"),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search on symbol or name."""
    result = await db.execute(
        select(Instrument).where(
            Instrument.exchange == exchange.upper(),
            or_(
                Instrument.symbol.ilike(f"%{q}%"),
                Instrument.name.ilike(f"%{q}%"),
            ),
        ).limit(50)
    )
    return result.scalars().all()


@router.get("/{token}", response_model=InstrumentResponse)
async def get_instrument_by_token(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Instrument).where(Instrument.token == token)
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail=f"Instrument token {token} not found")
    return inst


@router.post("/sync")
async def trigger_instrument_sync(db: AsyncSession = Depends(get_db)):
    """Manually trigger instrument sync from AngelOne scrip master."""
    count = await sync_instruments(db)
    ctx().instruments.invalidate()      # next universe read sees the new master
    return {"status": "ok", "synced": count}
