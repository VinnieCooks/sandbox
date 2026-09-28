"""
MSFT Call Debit Spread Re-strike
Re-applies the Call Debit Spread Playbook v1.0 gates to an option-chain snapshot
and ranks the structures that pass with a rule-aware Monte Carlo.

Gates (as applied by the 24 Sep 2026 "MSFT Spread Resize"):
  F1   expiry ATM IV / HV30 >= 1.20
  F2   short strike >= S * (1 + ATM IV * sqrt(DTE/365))   (the 1-sigma move)
  F3   each leg: bid-ask <= 5% of mid, open interest >= 1,000
  F4   90 <= DTE <= 150
  F5   break-even (long strike + debit) <= S * 1.08
  Cap  debit * 100 + $4 commissions <= $2,000   (4% of the $50k sleeve)

Exits simulated: S2 close at 75% of max profit (daily, GTC), S3 stop at 50% of
the debit (checked on Friday closes), S1 close two trading days before expiry.
Paths are GBM at HV30 plus a one-day earnings jump sized to the market-implied
move. Fills are conservative: buy the ask, sell the bid, $1/contract/side.

Usage (needs numpy):
  python msft_cds_restrike.py                      # Fri 25 Sep 2026 snapshot, entry Wed 30 Sep
  python msft_cds_restrike.py --bands 495:540      # which structure passes at each $1 of spot
  python msft_cds_restrike.py --chain data/<live>.csv --chain-spot 509.40 \
      --chain-date 2026-09-30 --entry 2026-09-30   # re-run on live quotes
"""

import argparse
import csv
import datetime as dt
import math
import statistics
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# Defaults — snapshot of Fri 25 Sep 2026 close (OPRA composite, 15-min delayed)
# ---------------------------------------------------------------------------

CHAIN_CSV = "data/msft_calls_2027-01-15_asof_2026-09-25.csv"
CLOSES_CSV = "data/msft_closes_asof_2026-09-25.csv"
CHAIN_SPOT = 516.17                   # IBKR close, Fri 25 Sep 2026
CHAIN_DATE = dt.date(2026, 9, 25)
ENTRY = dt.date(2026, 9, 30)          # funds land in Questrade Wed 30 Sep
EXPIRY = dt.date(2027, 1, 15)         # only listed expiry inside F4 (no Feb 2027 MSFT options)
S1_EXIT = dt.date(2027, 1, 13)
EARNINGS = dt.date(2026, 10, 28)      # Q1 FY27, after the close, unconfirmed
EARN_SD = 0.0627                      # implied from the Oct 23 / Oct 30 weekly straddles
RATE = 0.0375
CARRY = 0.0363                        # r - q implied by put-call parity on the Jan-27 chain
VIEW = 0.15                           # desk directional view, per year
CAP = 2000.0
COMMISSION = 4.0

HOLIDAYS = {dt.date(2026, 11, 26), dt.date(2026, 12, 25), dt.date(2027, 1, 1)}


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def b76_call(F: float, K: float, tau: float, vol: float, DF: float) -> float:
    """Black-76 call on the forward F, discounted by DF."""
    if tau <= 0 or vol <= 0:
        return DF * max(F - K, 0.0)
    s = vol * math.sqrt(tau)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    return DF * (F * _ncdf(d1) - K * _ncdf(d1 - s))


def implied_vol(price: float, F: float, K: float, tau: float, DF: float) -> float | None:
    if price <= DF * max(F - K, 0.0) + 1e-9:
        return None
    lo, hi = 1e-4, 3.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if b76_call(F, K, tau, mid, DF) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def _erf_np(x):
    # Abramowitz & Stegun 7.1.26, |error| < 1.5e-7
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
    return sign * (1.0 - poly * np.exp(-x * x))


def b76_call_np(F, K, tau, vol, DF):
    s = vol * math.sqrt(max(tau, 1e-8))
    d1 = (np.log(F / K) + 0.5 * s * s) / s
    ncdf = lambda z: 0.5 * (1.0 + _erf_np(z / math.sqrt(2.0)))
    return DF * (F * ncdf(d1) - K * ncdf(d1 - s))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    strike: float
    bid: float
    ask: float
    oi: int
    iv: float = 0.0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def width(self) -> float:
        return self.ask - self.bid


def load_chain(path: str, spot: float, asof: dt.date) -> dict[float, Leg]:
    tau = (EXPIRY - asof).days / 365
    DF, F = math.exp(-RATE * tau), spot * math.exp(CARRY * tau)
    chain = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            leg = Leg(float(row["strike"]), float(row["bid"]), float(row["ask"]), int(row["open_interest"]))
            iv = implied_vol(leg.mid, F, leg.strike, tau, DF)
            if iv:
                leg.iv = iv
                chain[leg.strike] = leg
    return chain


def load_closes(path: str) -> list[float]:
    with open(path) as f:
        return [float(row["close"]) for row in csv.DictReader(f)]


def hv30(closes: list[float], extra_log_moves: tuple = ()) -> float:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:])] + list(extra_log_moves)
    return statistics.stdev(rets[-30:]) * math.sqrt(252)


def smile(chain: dict[float, Leg], K: float) -> float:
    ks = sorted(chain)
    if K <= ks[0]:
        return chain[ks[0]].iv
    if K >= ks[-1]:
        return chain[ks[-1]].iv
    for a, b in zip(ks, ks[1:]):
        if a <= K <= b:
            return chain[a].iv + (chain[b].iv - chain[a].iv) * (K - a) / (b - a)


def reprice(chain: dict[float, Leg], spot: float, on: dt.date) -> dict[float, Leg]:
    """Sticky-strike reprice to another spot/date, keeping each strike's IV and absolute bid-ask width."""
    tau = (EXPIRY - on).days / 365
    DF, F = math.exp(-RATE * tau), spot * math.exp(CARRY * tau)
    out = {}
    for K, leg in chain.items():
        mid = b76_call(F, K, tau, leg.iv, DF)
        out[K] = Leg(K, mid - leg.width / 2, mid + leg.width / 2, leg.oi, leg.iv)
    return out


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

@dataclass
class Structure:
    long: Leg
    short: Leg
    spot: float
    dte: int
    atm_iv: float
    hv30: float
    fails: list = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.short.strike - self.long.strike

    @property
    def debit(self) -> float:          # conservative: buy the ask, sell the bid
        return self.long.ask - self.short.bid

    @property
    def mid(self) -> float:
        return self.long.mid - self.short.mid

    @property
    def cost(self) -> float:
        return self.debit * 100 + COMMISSION

    @property
    def breakeven(self) -> float:
        return self.long.strike + self.debit

    @property
    def rr(self) -> float:
        return ((self.width - self.debit) * 100 - COMMISSION) / self.cost

    @property
    def label(self) -> str:
        return f"{self.long.strike:g}/{self.short.strike:g}"


def check(long: Leg, short: Leg, spot: float, dte: int, atm_iv: float, hv: float, strict_f2: bool = True) -> Structure:
    s = Structure(long, short, spot, dte, atm_iv, hv)
    if atm_iv / hv < 1.20:
        s.fails.append("F1")
    if strict_f2 and short.strike < spot * (1 + atm_iv * math.sqrt(dte / 365)):
        s.fails.append("F2")
    if max(long.width / long.mid, short.width / short.mid) > 0.05 or min(long.oi, short.oi) < 1000:
        s.fails.append("F3")
    if not 90 <= dte <= 150:
        s.fails.append("F4")
    if s.breakeven > spot * 1.08:
        s.fails.append("F5")
    if s.cost > CAP:
        s.fails.append("CAP")
    return s


def screen(chain: dict[float, Leg], spot: float, entry: dt.date, hv: float, strict_f2: bool = True) -> list[Structure]:
    dte = (EXPIRY - entry).days
    atm = smile(chain, spot)
    out = []
    for k1 in sorted(chain):
        for k2 in sorted(chain):
            if k1 < spot * 0.9 or k2 <= k1 + 20 or k2 > spot * 1.4:
                continue
            out.append(check(chain[k1], chain[k2], spot, dte, atm, hv, strict_f2))
    return out


# ---------------------------------------------------------------------------
# Rule-aware Monte Carlo
# ---------------------------------------------------------------------------

def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    days, d = [], start + dt.timedelta(days=1)
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def simulate(s: Structure, entry: dt.date, drift: float, paths: int = 40000, earn_sd: float = EARN_SD,
             debit: float | None = None, seed: int = 7) -> dict:
    """P&L per contract in $, after S1/S2/S3 exits and conservative fills."""
    rng = np.random.default_rng(seed)
    days = trading_days(entry, S1_EXIT)
    debit = s.debit if debit is None else debit
    half = paths // 2
    z = rng.standard_normal((half, len(days)))
    z = np.vstack([z, -z])
    jz = rng.standard_normal(half)
    jump = -0.5 * earn_sd ** 2 + earn_sd * np.concatenate([jz, -jz])

    tau0 = (EXPIRY - entry).days / 365
    ev = earn_sd ** 2
    base = [math.sqrt(max(leg.iv ** 2 - ev / tau0, 1e-6)) for leg in (s.long, s.short)]
    exit_cost = 0.5 * (s.long.width + s.short.width)
    s2_level = debit + 0.75 * (s.width - debit)
    s3_level = 0.5 * debit

    log_s = np.full(paths, math.log(s.spot))
    alive = np.ones(paths, bool)
    pnl = np.zeros(paths)
    how = np.zeros(paths, np.int8)                    # 1 = S2, 2 = S3, 3 = S1
    dt_ = 1 / 252
    for i, d in enumerate(days):
        step = (drift - 0.5 * s.hv30 ** 2) * dt_ + s.hv30 * math.sqrt(dt_) * z[:, i]
        if d == EARNINGS + dt.timedelta(days=1):
            step = step + jump
        log_s += step
        tau = (EXPIRY - d).days / 365
        F = np.exp(log_s) * math.exp(CARRY * tau)
        DF = math.exp(-RATE * tau)
        v1, v2 = (b if d > EARNINGS else math.sqrt(b ** 2 + ev / tau) for b in base)
        value = b76_call_np(F, s.long.strike, tau, v1, DF) - b76_call_np(F, s.short.strike, tau, v2, DF) - exit_cost
        value = np.clip(value, 0.0, s.width)
        rules = ((1, lambda: value >= s2_level, True),
                 (2, lambda: value <= s3_level, d.weekday() == 4),
                 (3, lambda: np.ones(paths, bool), d == S1_EXIT))
        for code, condition, active in rules:
            if not active:
                continue
            hit = alive & condition()
            pnl[hit] = (value[hit] - debit) * 100 - COMMISSION
            how[hit] = code
            alive &= ~hit
    return {"ev": pnl.mean(), "pop": (pnl > 0).mean(), "p10": np.percentile(pnl, 10),
            "p90": np.percentile(pnl, 90), "s2": (how == 1).mean(), "s3": (how == 2).mean()}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

DIVIDER = "─" * 78


def report(chain, closes, spot, entry, paths):
    hv = hv30(closes)
    atm = smile(chain, spot)
    dte = (EXPIRY - entry).days
    print(f"\n{'MSFT CALL DEBIT SPREAD RE-STRIKE':^78}")
    print(f"{f'Spot {spot:.2f} · entry {entry} · Jan-15-2027 · {dte} DTE':^78}")
    print(DIVIDER)
    print(f"  F1  ATM IV {atm:.1%} / HV30 {hv:.1%} = {atm / hv:.2f}  (needs >= 1.20)")
    print(f"  F2  short strike >= {spot * (1 + atm * math.sqrt(dte / 365)):.1f}")
    print(f"  F5  break-even <= {spot * 1.08:.2f}   ·   cap: debit <= {(CAP - COMMISSION) / 100:.2f}")
    print(DIVIDER)

    passing = [s for s in screen(chain, spot, entry, hv) if not s.fails]
    near = sorted((s for s in screen(chain, spot, entry, hv) if len(s.fails) == 1), key=lambda s: -s.rr)[:6]
    if not passing:
        print("  No structure passes every gate at this spot: the playbook answer is no trade.")
    for s in passing:
        up, flat = simulate(s, entry, VIEW, paths), simulate(s, entry, 0.0, paths)
        print(f"  PASS {s.label:>8}  debit {s.debit:5.2f} (mid {s.mid:5.2f})  cost ${s.cost:,.0f}  "
              f"BE {s.breakeven:.2f} ({s.breakeven / spot - 1:+.1%})  R:R {s.rr:.2f}")
        print(f"         EV +{VIEW:.0%}/yr ${up['ev']:+,.0f}  POP {up['pop']:.0%}  stop hit {up['s3']:.0%}  "
              f"p10 ${up['p10']:+,.0f}  p90 ${up['p90']:+,.0f}  ·  EV 0% ${flat['ev']:+,.0f}")
    print(DIVIDER)
    print("  One gate short: " + ", ".join(f"{s.label} ({s.fails[0]})" for s in near))


def bands(chain, closes, lo, hi, entry):
    print(f"\n{'WHICH STRUCTURE PASSES AT EACH $1 OF SPOT':^78}")
    print(f"{'Sticky-strike reprice of the snapshot; move spread evenly over the days to entry':^78}")
    print(DIVIDER)
    days_to_entry = max(len(trading_days(CHAIN_DATE, entry)), 1)
    for spot in range(lo, hi + 1):
        move = math.log(spot / CHAIN_SPOT) / days_to_entry
        hv = hv30(closes, (move,) * days_to_entry)
        priced = reprice(chain, spot, entry)
        passing = sorted((s for s in screen(priced, spot, entry, hv) if not s.fails), key=lambda s: -s.rr)
        text = "  ".join(f"{s.label} @{s.debit:.2f} BE {s.breakeven / spot - 1:+.1%}" for s in passing) or "no trade"
        print(f"  {spot:>4}  {text}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", default=CHAIN_CSV)
    ap.add_argument("--closes", default=CLOSES_CSV)
    ap.add_argument("--chain-spot", type=float, default=CHAIN_SPOT, help="underlying price when the chain was quoted")
    ap.add_argument("--chain-date", type=dt.date.fromisoformat, default=CHAIN_DATE)
    ap.add_argument("--entry", type=dt.date.fromisoformat, default=ENTRY)
    ap.add_argument("--spot", type=float, help="spot at entry (default: chain spot)")
    ap.add_argument("--paths", type=int, default=40000)
    ap.add_argument("--bands", help="spot range lo:hi for the lookup table, e.g. 495:540")
    args = ap.parse_args()

    chain = load_chain(args.chain, args.chain_spot, args.chain_date)
    closes = load_closes(args.closes)
    spot = args.spot or args.chain_spot
    priced = chain if (spot == args.chain_spot and args.entry == args.chain_date) else reprice(chain, spot, args.entry)
    report(priced, closes, spot, args.entry, args.paths)
    if args.bands:
        lo, hi = (int(x) for x in args.bands.split(":"))
        bands(chain, closes, lo, hi, args.entry)


if __name__ == "__main__":
    main()
