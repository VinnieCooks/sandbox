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

## Prompt

Paste everything below the line into the routine's instructions.

---

You are running unattended as a scheduled routine; nobody can answer questions.
Goal: today's wheel-strategy cash-secured-put shortlist from LIVE data, scored with the
existing logic in `wheel_screener.py` at the repository root.

Hard rules:
- Read-only. Never create, modify or cancel orders, order instructions, alerts or
  watchlists. Call only IBKR tools that read data.
- Never invent a number. If the connector cannot provide a field, write "n/a", leave that
  ticker out of the ranking, and say which field was missing.

Steps:
1. Universe: the tickers in `CANDIDATES` in `wheel_screener.py`.
2. For each ticker, read from the IBKR connector: last price; 52-week high and low; the
   price 52 weeks ago (`open_52w`); current 30-day implied volatility; 52-week IV rank
   (from IV history if the connector does not report it directly); 30-day historical
   volatility; average daily option volume. Note the data timestamp.
3. Save the values to `/tmp/snapshot.json` using the `Candidate` field names.
4. Score them with the repository's own code, not a re-implementation, for example:
   `python -c "import json; from wheel_screener import Candidate, score_candidate; ..."`,
   calling `score_candidate(c, dte=30)` and ranking by `composite_score`.
5. Final message, at most 12 plain-text lines for a phone notification: data timestamp,
   then the top 3 as `TICKER price | strike | est. premium | yield/mo | IVR | trend | score`,
   then one line listing any ticker excluded for missing data.
