"""
Wheel Options Strategy Screener
Scores liquid US stocks on IV rank, premium yield, trend, and liquidity
then recommends the best cash-secured put to open today.

Scoring criteria (100 pts total):
  30 pts — IV Rank (52w): higher = richer premium environment
  25 pts — Premium Yield (annualised): monthly credit / strike × 12
  20 pts — Option Liquidity: avg daily option volume
  15 pts — Trend Health: % above 52w low (want room below us)
  10 pts — Capital Efficiency: score inversely proportional to share price
"""

import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    ticker: str
    price: float
    annual_iv: float          # fraction, e.g. 0.30 = 30 %
    iv_rank_52w: float        # fraction, e.g. 0.92 = 92nd percentile
    hist_vol_annual: float    # fraction
    avg_option_vol: int       # average daily option contracts (calls + puts)
    low_52w: float
    high_52w: float
    open_52w: float           # price at start of year for trend direction
    # Filled in by scoring
    trend_pct_above_low: float = 0.0
    is_uptrend: bool = True
    monthly_yield_estimate: float = 0.0
    composite_score: float = 0.0
    recommended_strike: Optional[float] = None
    premium_estimate: Optional[float] = None
    annualised_return: Optional[float] = None


# ---------------------------------------------------------------------------
# Live snapshot — pulled 2026-07-22 via IBKR MCP tools
# ---------------------------------------------------------------------------

CANDIDATES = [
    Candidate(
        ticker="AAPL",
        price=325.76,
        annual_iv=0.292,
        iv_rank_52w=0.916,
        hist_vol_annual=0.295,
        avg_option_vol=1_684_585,
        low_52w=200.88,
        high_52w=334.99,
        open_52w=212.49,
    ),
    Candidate(
        ticker="AMD",
        price=549.64,
        annual_iv=0.848,
        iv_rank_52w=0.984,
        hist_vol_annual=0.867,
        avg_option_vol=476_016,
        low_52w=149.22,
        high_52w=584.73,
        open_52w=156.20,
    ),
    Candidate(
        ticker="TSLA",
        price=358.85,
        annual_iv=0.478,
        iv_rank_52w=0.665,
        hist_vol_annual=0.475,
        avg_option_vol=2_471_512,
        low_52w=297.82,
        high_52w=498.83,
        open_52w=329.74,
    ),
    Candidate(
        ticker="SOFI",
        price=17.05,
        annual_iv=0.634,
        iv_rank_52w=0.590,
        hist_vol_annual=0.589,
        avg_option_vol=409_567,
        low_52w=14.92,
        high_52w=32.73,
        open_52w=20.79,
    ),
    Candidate(
        ticker="PLTR",
        price=125.00,
        annual_iv=0.689,
        iv_rank_52w=0.972,
        hist_vol_annual=0.575,
        avg_option_vol=507_973,
        low_52w=106.37,
        high_52w=207.52,
        open_52w=150.85,
    ),
    Candidate(
        ticker="META",
        price=619.21,
        annual_iv=0.531,
        iv_rank_52w=0.964,
        hist_vol_annual=0.470,
        avg_option_vol=772_225,
        low_52w=520.26,
        high_52w=794.42,
        open_52w=714.53,
    ),
    Candidate(
        ticker="NVDA",
        price=210.90,
        annual_iv=0.384,
        iv_rank_52w=0.422,
        hist_vol_annual=0.395,
        avg_option_vol=3_796_025,
        low_52w=164.04,
        high_52w=236.54,
        open_52w=171.31,
    ),
    Candidate(
        ticker="MSTR",
        price=99.36,
        annual_iv=0.812,
        iv_rank_52w=0.861,
        hist_vol_annual=0.915,
        avg_option_vol=350_228,
        low_52w=81.81,
        high_52w=424.00,
        open_52w=428.84,
    ),
]


# ---------------------------------------------------------------------------
# Black-Scholes put pricer (simplified, risk-free rate 5 %)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Abramowitz & Stegun 26.2.17 approximation, accurate to 7e-8."""
    if x < 0:
        return 1.0 - _norm_cdf(-x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    return 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x * x) * poly


def bs_put(S: float, K: float, T: float, sigma: float, r: float = 0.05) -> float:
    """Black-Scholes put price. T in years."""
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def otm_put_strike(price: float, pct_otm: float) -> float:
    """Round to nearest $2.50 strike below price × (1 - pct_otm)."""
    raw = price * (1 - pct_otm)
    return round(raw / 2.5) * 2.5


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_iv_rank(rank: float) -> float:
    """30 pts — linear mapping 0 → 0, 1 → 30."""
    return rank * 30


def score_premium_yield(yield_monthly: float) -> float:
    """25 pts — target 8 % monthly for full score, capped."""
    return min(yield_monthly / 0.08, 1.0) * 25


def score_liquidity(avg_vol: int) -> float:
    """20 pts — log scale, 100k = 10 pts, 1M = 17 pts, 3M+ = 20 pts."""
    if avg_vol <= 0:
        return 0.0
    return min(math.log10(avg_vol / 100_000) / math.log10(30), 1.0) * 20


def score_trend(pct_above_low: float, is_uptrend: bool) -> float:
    """15 pts — 10% above low = 5 pts, 30%+ = 15 pts; halved if downtrend."""
    raw = min(pct_above_low / 0.30, 1.0) * 15
    return raw if is_uptrend else raw * 0.5


def score_capital_efficiency(price: float) -> float:
    """10 pts — cheaper stocks need less capital; $20 → 10, $100 → 7, $600+ → 0."""
    return max(0.0, (1 - (price - 20) / 580)) * 10


def score_candidate(c: Candidate, dte: int = 30) -> None:
    """Compute all derived metrics and composite score in-place."""
    # Trend
    c.trend_pct_above_low = (c.price - c.low_52w) / c.price
    c.is_uptrend = c.price > c.open_52w

    # Target a ~5 % OTM put for premium estimate
    T = dte / 365.0
    K = otm_put_strike(c.price, 0.05)
    premium = bs_put(c.price, K, T, c.annual_iv)
    c.recommended_strike = K
    c.premium_estimate = round(premium, 2)
    c.monthly_yield_estimate = premium / K
    c.annualised_return = (premium / K) * (365 / dte)

    # Sub-scores
    s_ivr = score_iv_rank(c.iv_rank_52w)
    s_yield = score_premium_yield(c.monthly_yield_estimate)
    s_liq = score_liquidity(c.avg_option_vol)
    s_trend = score_trend(c.trend_pct_above_low, c.is_uptrend)
    s_cap = score_capital_efficiency(c.price)

    c.composite_score = s_ivr + s_yield + s_liq + s_trend + s_cap


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

DIVIDER = "─" * 72

def print_screener(candidates: list, dte: int = 30) -> None:
    for c in candidates:
        score_candidate(c, dte)

    ranked = sorted(candidates, key=lambda x: x.composite_score, reverse=True)

    print(f"\n{'WHEEL STRATEGY SCREENER':^72}")
    print(f"{'Live snapshot: 2026-07-22  |  Target expiry: 30 DTE (Aug 21)':^72}")
    print(DIVIDER)

    header = f"{'#':<3} {'Ticker':<7} {'Price':>7} {'IV%':>6} {'IVR%':>6} {'Up?':>4} {'Strike':>7} {'Prem$':>6} {'Yield/Mo':>9} {'Ann%':>6} {'Score':>6}"
    print(header)
    print(DIVIDER)

    for i, c in enumerate(ranked, 1):
        trend_arrow = "✅" if c.is_uptrend else "❌"
        print(
            f"{i:<3} {c.ticker:<7} "
            f"${c.price:>6.2f} "
            f"{c.annual_iv*100:>5.1f}% "
            f"{c.iv_rank_52w*100:>5.1f}% "
            f"{trend_arrow:>4} "
            f"${c.recommended_strike:>6.1f} "
            f"${c.premium_estimate:>5.2f} "
            f"{c.monthly_yield_estimate*100:>8.2f}% "
            f"{c.annualised_return*100:>5.1f}% "
            f"{c.composite_score:>6.1f}"
        )

    print(DIVIDER)

    winner = ranked[0]
    capital_needed = winner.recommended_strike * 100

    print(f"\n{'★  RECOMMENDED TRADE  ★':^72}")
    print(DIVIDER)
    print(f"  Action   : SELL TO OPEN — Cash-Secured Put")
    print(f"  Ticker   : {winner.ticker}  (current: ${winner.price:.2f})")
    print(f"  Strike   : ${winner.recommended_strike:.1f}")
    print(f"  Expiry   : Aug 21, 2026 (30 DTE)")
    print(f"  Premium  : ~${winner.premium_estimate:.2f}/share  (${winner.premium_estimate*100:.0f}/contract)")
    print(f"  Capital  : ${capital_needed:,.0f} per contract (cash-secured)")
    print(f"  Yield    : {winner.monthly_yield_estimate*100:.2f}% / 30 days  →  {winner.annualised_return*100:.1f}% annualised")
    print(f"  IV Rank  : {winner.iv_rank_52w*100:.0f}th percentile (52w)")
    print(f"  Trend    : {'▲ Uptrend — price is up {:.0f}% YTD'.format((winner.price/winner.open_52w - 1)*100) if winner.is_uptrend else '▼ Downtrend — caution'}")
    print(DIVIDER)

    print(f"\n  Wheel Cycle Plan:")
    print(f"  ① Sell ${winner.recommended_strike:.0f} Put (Aug 21) → collect ${winner.premium_estimate*100:.0f}")
    print(f"     — If expires worthless: repeat next cycle, compounding")
    print(f"     — If assigned: own {winner.ticker} at ${winner.recommended_strike - winner.premium_estimate:.2f} effective cost")
    print(f"  ② If assigned, immediately sell covered calls at or above")
    print(f"     ${winner.recommended_strike:.0f} strike to monetise while waiting for recovery")
    print(f"  ③ If called away: return to step ① with fresh premium")
    print(f"\n  Max risk : Assigned stock falls below ${winner.recommended_strike - winner.premium_estimate:.2f}")
    print(f"  Breakeven: ${winner.recommended_strike - winner.premium_estimate:.2f} (effective cost basis)")

    print(f"\n  Why {winner.ticker}?")
    for c in ranked[:3]:
        trend_label = f"up {(c.price/c.open_52w-1)*100:.0f}% YTD" if c.is_uptrend else f"down {abs(c.price/c.open_52w-1)*100:.0f}% YTD"
        print(f"    #{ranked.index(c)+1} {c.ticker}: score {c.composite_score:.1f}  |  IV rank {c.iv_rank_52w*100:.0f}%  |  {trend_label}")
    print(DIVIDER)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print_screener(CANDIDATES, dte=30)
