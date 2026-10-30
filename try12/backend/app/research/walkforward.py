"""
Chronological walk-forward, cost scenarios and evidence-based strategy
status (Parts 9, 11, 12). Pure functions over trade rows (dicts with day,
r_gross, risk_pts, hold_min, expiry, regime, candidate).

Protocol (fixed before running):
  * DESIGN window = the first 12 months. Variant thresholds come from it;
    no test month inside it is scored.
  * Fold f: train = 3 calendar months, validation = the next month, test =
    the month after. Roll by one month. Only test months after the DESIGN
    window count as out-of-sample.
  * Selection per fold, per strategy: among its candidates (entry variants,
    each with and without a regime filter learned on train), the highest
    train net expectancy with train n >= MIN_TRAIN_N and expectancy > 0; it
    trades the test month only if its validation expectancy is also > 0.
    Otherwise the strategy stands aside (NO_TRADE) that month.
  * The test month is never used to choose anything.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from app.research import metrics

MIN_TRAIN_N = 20
MIN_REGIME_N = 10


@dataclass(frozen=True)
class CostModel:
    """Option round-trip cost in SPOT points: fixed (spread + slippage +
    charges at ~0.53 delta) plus premium decay per hour held."""
    name: str
    fixed_pts: float
    decay_pts_per_hour: float
    expiry_decay_pts_per_hour: float

    def pts(self, hold_min: float, expiry: bool) -> float:
        rate = self.expiry_decay_pts_per_hour if expiry else self.decay_pts_per_hour
        return self.fixed_pts + rate * max(0.0, hold_min) / 60.0


# MEASURED: recorded quotes, 11 sessions Oct 2026 (options.execution_study):
# round-trip spread ~0.8 pts, charges ~Rs 63/lot ~= 1.8 pts, decay 1.5 / 4.5
# pts per hour (non-expiry / expiry; the non-expiry figure is rounded UP from a
# noisy ~0.4-1.0 because 11 sessions cannot pin down theta).
# HARSH: the earlier research's 4 pts + 4 pts/h.
COST_MODELS = {
    "ZERO": CostModel("ZERO", 0.0, 0.0, 0.0),
    "MEASURED": CostModel("MEASURED", 2.6, 1.5, 4.5),
    "HARSH": CostModel("HARSH", 4.0, 4.0, 4.0),
}
PRIMARY_COST = "MEASURED"


def apply_costs(row: dict, model: CostModel) -> dict:
    risk = row["risk_pts"]
    cost = model.pts(row["hold_min"], bool(row.get("expiry")))
    r = dict(row)
    r["cost_r"] = round(cost / risk, 4) if risk > 0 else 0.0
    r["r_net"] = round(row["r_gross"] - r["cost_r"], 4)
    return r


@dataclass(frozen=True)
class PromotionCriteria:
    """Research gates, not proof of future profit (Part 12)."""
    min_trades: int = 100
    preferred_trades: int = 200
    min_sessions: int = 60
    min_profit_factor: float = 1.20
    max_drawdown_r: float = -25.0
    min_positive_fold_share: float = 0.5
    require_ci_lower_above: float = 0.0


def month_key(d: str) -> str:
    return d[:7]


def months_between(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last[:7]:
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def folds(all_months: list[str], design_end: str) -> list[dict]:
    out = []
    for i in range(len(all_months) - 4):
        test = all_months[i + 4]
        if test <= design_end[:7]:
            continue
        out.append({"train": all_months[i:i + 3], "val": all_months[i + 3], "test": test})
    return out


def _exp(rows):
    return sum(r["r_net"] for r in rows) / len(rows) if rows else None


def learn_regime_filter(rows: list[dict]) -> Optional[frozenset]:
    """Regimes with n >= MIN_REGIME_N and positive net expectancy in *rows*."""
    by = defaultdict(list)
    for r in rows:
        by[r.get("regime")].append(r)
    keep = frozenset(k for k, v in by.items() if len(v) >= MIN_REGIME_N and _exp(v) > 0)
    return keep or None


def walk_forward(rows_by_candidate: dict[str, list[dict]], all_months: list[str],
                 design_end: str) -> dict:
    """*rows_by_candidate*: costed rows per entry candidate of ONE strategy."""
    fold_out, oos = [], []
    for f in folds(all_months, design_end):
        best = None
        for cand, rows in rows_by_candidate.items():
            tr = [r for r in rows if month_key(r["day"]) in f["train"]]
            for filt_name in ("ALL", "REGIME"):
                allowed = None
                if filt_name == "REGIME":
                    allowed = learn_regime_filter(tr)
                    if allowed is None:
                        continue
                sel = [r for r in tr if allowed is None or r.get("regime") in allowed]
                e = _exp(sel)
                if len(sel) < MIN_TRAIN_N or e is None or e <= 0:
                    continue
                if best is None or e > best[2]:
                    best = (cand, allowed, e, filt_name, len(sel))
        rec = {"test": f["test"], "train": f["train"], "val": f["val"], "choice": "NO_TRADE"}
        if best is not None:
            cand, allowed, e, filt_name, n = best
            keep = lambda r: allowed is None or r.get("regime") in allowed  # noqa: E731
            val = [r for r in rows_by_candidate[cand] if month_key(r["day"]) == f["val"] and keep(r)]
            ve = _exp(val)
            rec.update(candidate=cand, filter=filt_name,
                       allowed_regimes=sorted(allowed) if allowed else None,
                       train_n=n, train_exp=round(e, 4), val_n=len(val),
                       val_exp=round(ve, 4) if ve is not None else None)
            if ve is not None and ve > 0:
                test = [r for r in rows_by_candidate[cand] if month_key(r["day"]) == f["test"] and keep(r)]
                rec.update(choice=f"{cand}/{filt_name}", test_n=len(test),
                           test_exp=round(_exp(test), 4) if test else None,
                           test_total_r=round(sum(r["r_net"] for r in test), 3))
                oos.extend(test)
        fold_out.append(rec)
    traded = [f for f in fold_out if f.get("test_n")]
    oos.sort(key=lambda r: (r["day"], r.get("entry_time", "")))
    fold_r = [f["test_total_r"] for f in traded]
    return {
        "folds": fold_out,
        "oos": metrics.summarize(oos) if oos else {"n": 0},
        "oos_rows": oos,
        "folds_total": len(fold_out), "folds_traded": len(traded),
        "folds_positive": sum(1 for x in fold_r if x > 0),
        "worst_fold_r": min(fold_r) if fold_r else None,
        "best_fold_r": max(fold_r) if fold_r else None,
        "fold_r_std": (round((sum((x - sum(fold_r) / len(fold_r)) ** 2 for x in fold_r)
                              / (len(fold_r) - 1)) ** 0.5, 3) if len(fold_r) > 1 else None),
    }


def classify_status(oos: dict, in_sample_exp: Optional[float], folds_traded: int,
                    folds_positive: int, crit: PromotionCriteria = PromotionCriteria()) -> tuple[str, list[str]]:
    """INSUFFICIENT_EVIDENCE / DISABLED_FOR_RESEARCH / OVERFIT / ELIGIBLE_FOR_PAPER, and why."""
    why: list[str] = []
    n = oos.get("n", 0)
    exp = oos.get("expectancy_r")
    ci = oos.get("expectancy_ci95") or (None, None)
    if n == 0:
        return "INSUFFICIENT_EVIDENCE", ["no out-of-sample trades (stood aside every month)"]
    if exp is not None and exp <= 0:
        if ci[1] is not None and ci[1] < 0 or n >= crit.min_trades:
            if in_sample_exp is not None and in_sample_exp > 0:
                return "OVERFIT", [f"in-sample {in_sample_exp:+.3f}R, out-of-sample {exp:+.3f}R (n={n})"]
            return "DISABLED_FOR_RESEARCH", [f"out-of-sample {exp:+.3f}R (n={n}, CI {ci})"]
        return "INSUFFICIENT_EVIDENCE", [f"out-of-sample {exp:+.3f}R on only {n} trades"]
    if n < crit.min_trades:
        why.append(f"{n} trades < {crit.min_trades}")
    if oos.get("sessions", 0) < crit.min_sessions:
        why.append(f"{oos.get('sessions')} sessions < {crit.min_sessions}")
    if (oos.get("profit_factor") or 0) < crit.min_profit_factor:
        why.append(f"profit factor {oos.get('profit_factor')} < {crit.min_profit_factor}")
    if ci[0] is None or ci[0] <= crit.require_ci_lower_above:
        why.append(f"expectancy CI {ci} includes 0")
    if (oos.get("expectancy_without_best_session") or 0) <= 0:
        why.append("not positive without its best session")
    if (oos.get("max_drawdown_r") or 0) < crit.max_drawdown_r:
        why.append(f"max drawdown {oos.get('max_drawdown_r')}R beyond {crit.max_drawdown_r}R")
    if folds_traded and folds_positive / folds_traded < crit.min_positive_fold_share:
        why.append(f"{folds_positive}/{folds_traded} traded test months positive")
    return ("ELIGIBLE_FOR_PAPER", ["all gates passed"]) if not why else ("INSUFFICIENT_EVIDENCE", why)


def criteria_dict(crit: PromotionCriteria = PromotionCriteria()) -> dict:
    return asdict(crit)
