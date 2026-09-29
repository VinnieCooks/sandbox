# Routine: wheel-screen-live

Runs the existing wheel screener logic on **live** IBKR (Interactive Brokers) data. This is
the part that cannot run on GitHub Actions or a VPS: those authenticate Claude with a
`CLAUDE_CODE_OAUTH_TOKEN`, which can make model requests but cannot load claude.ai
connectors such as IBKR. A Cloud Routine can.

| Setting | Value |
|---|---|
| Trigger | Schedule, weekdays 10:07 America/New_York (after the open; off the hour, since on-the-hour runs can start several minutes late) |
| Repository | `VinnieCooks/sandbox` (for the scoring functions in `wheel_screener.py`) |
| Connectors | Interactive Brokers only. Remove every other connector: a routine may call any tool of an included connector without asking |
| Environment | Default (Trusted network) is enough; connector traffic does not need allowlisting |
| Notifications | Push to the Claude app (and email, optionally) |
| Daily cap cost | 1 run per weekday (caps: Pro 5, Max 15, Team/Enterprise 25 runs/day) |

Data source: a single IBKR `get_price_snapshot` call per ticker returns every input the
screener needs (`misc_statistics` carries the 52-week high, low and year-ago open).

Routines created from a Claude Code session carry no connectors; add Interactive
Brokers on the routine's edit page at claude.ai/code/routines.

## Prompt

Paste everything below the line into the routine's instructions.

---

You are running unattended as a scheduled routine; nobody can answer questions.
Goal: today's wheel-strategy cash-secured-put shortlist from LIVE IBKR data, scored with the
existing logic in `wheel_screener.py`.

Hard rules:
- Read-only. Use only these IBKR tools: search_contracts, get_price_snapshot,
  get_option_parameters, get_option_data. Never call a tool that creates, modifies or
  cancels orders, order instructions, alerts or watchlists.
- Never invent a number. If a field is missing from a response, write "n/a", leave that
  ticker out of the ranking, and name the missing field.

Steps:
1. If this session has no Interactive Brokers tools, reply with exactly one line,
   "wheel-screen-live: no IBKR connector - add Interactive Brokers to this routine at
   claude.ai/code/routines", and stop.
2. Find `wheel_screener.py` at the repository root. If it is not in your working directory,
   run `git clone --depth 1 https://github.com/VinnieCooks/sandbox` and work in that folder.
3. Universe: the tickers in `CANDIDATES` in `wheel_screener.py`.
4. For each ticker: call `search_contracts` with the ticker and take the row whose symbol
   matches exactly and is the US primary listing. Then make one `get_price_snapshot` call
   with market_data_names: last, misc_statistics, implied_vol_underlying,
   implied_volatility_percentile, historical_vol, underlying_avg_option_volume, top_status.
   Response keys use hyphens, e.g. `implied-vol-underlying`.
5. Map the response to the `Candidate` fields and save the list to `/tmp/snapshot.json`:
   - price: last
   - low_52w, high_52w: the 52-week low and high in misc-statistics; open_52w: its price
     52 weeks ago
   - annual_iv: implied-vol-underlying (a fraction)
   - iv_rank_52w: the 52-week value of implied-volatility-percentile (a fraction; the
     script documents this field as a percentile)
   - hist_vol_annual: historical-vol
   - avg_option_vol: underlying-avg-option-volume (calls plus puts, as an integer)
6. Score with the repository's own code, not a re-implementation: build
   `Candidate(**row)` for each row, call `score_candidate(c, dte=30)`, and rank by
   `composite_score`. Its premium is a Black-Scholes estimate from implied volatility,
   not a market quote.
7. Reality check for the #1 pick only: with `get_option_parameters`, choose the regular
   monthly expiration 25 to 45 days out; with `get_option_data` (strikes bounded around
   the recommended strike), find the put at that strike; read its bid/ask with
   `get_price_snapshot`. If any step fails, write "quote unavailable" and continue.
8. Final message, at most 12 plain-text lines for a phone notification:
   - the data time and whether data was REALTIME or DELAYED (top-status)
   - the top 3 as `TICKER $price | put $strike | est $premium | yield/mo | IV pct | trend vs 52w ago | score`
   - the #1 pick's real bid/ask next to the estimate
   - one line naming any ticker left out, and why
