// GET /trading/market-study/daily and /intraday (backend routes/trading.py). Every value is nullable:
// snapshots are written by the study collector and older rows lack many fields (pdh/pdl are NULL).

export interface DailySnapshot {
  date:       string;
  underlying: string;
  open:       number | null;
  high:       number | null;
  low:        number | null;
  close:      number | null;
  change_pct: number | null;
  vix:        number | null;
  atm_iv:     number | null;
  iv_pct:     number | null;
  pcr_oi:     number | null;
  max_pain:   number | null;
  max_ce_oi:  number | null;
  max_pe_oi:  number | null;
  trend:      string | null;
  orb_high:   number | null;
  orb_low:    number | null;
  vwap_close: number | null;
  pdh:        number | null;
  pdl:        number | null;
  dte:        number | null;
}

export interface IntradayFeature {
  time:         string;
  spot:         number | null;
  vwap:         number | null;
  spot_vs_vwap: number | null;
  atm:          number | null;
  straddle:     number | null;
  pcr:          number | null;
  rsi:          number | null;
  vix:          number | null;
  orb_high:     number | null;
  orb_low:      number | null;
  [k: string]:  unknown;
}
