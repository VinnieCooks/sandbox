# Claude Cloud Routines

Prompts for work that needs Claude **with your claude.ai connectors** (IBKR, Gmail, Drive
and so on). They run on Anthropic's infrastructure, not on GitHub or your server, because
the token those engines use (`CLAUDE_CODE_OAUTH_TOKEN`) "can only make model requests":
it cannot load claude.ai connectors.

| File | Trigger | Connectors |
|---|---|---|
| [wheel-screen-live.md](wheel-screen-live.md) | schedule, weekdays 10:07 ET | Interactive Brokers |
| [deep-dive.md](deep-dive.md) | API, fired by jobs with `routine: deep_dive` | Interactive Brokers |

## Create one

1. Go to [claude.ai/code/routines](https://claude.ai/code/routines), click **New routine**.
2. Paste the prompt from the file (everything below its `---` line) and pick the model.
3. Add the repository `VinnieCooks/sandbox`, then the trigger from the file's table.
4. Under **Connectors**, remove everything except the one listed. All connected connectors
   are included by default, and a routine can call any tool of an included connector,
   writes included, without asking. Prompt instructions such as "read-only" are guidance,
   not a permission boundary.
5. Click **Create**, then **Run now** once and read the transcript. A green run status only
   means the session started and exited cleanly; it does not mean the task succeeded.

From a Claude Code CLI session you can do the same with `/schedule`. API triggers (for
`deep-dive`) can only be added on the web. A routine that Claude creates for you from a
cloud session starts with **no connectors**: open it at claude.ai/code/routines, choose
**Edit**, and add the connector yourself.

Created so far: `wheel-screen-live` (trigger `trig_01LD4syDD9vbvWHJ3YA5rRjJ`).

## Limits to plan around

| Limit | Value |
|---|---|
| Runs per day | Pro 5, Max 15, Team/Enterprise 25 (one-off runs don't count) |
| Shortest schedule | 1 hour |
| API fires | 30 per hour per routine, 100 per hour per account |
| Status | Research preview: behavior and limits may change |

## Notifications

Routines notify through the Claude app (push) and email. In the Default environment, the
network allowlist blocks ntfy.sh, api.telegram.org, Slack and Discord (`403
host_not_allowed`). So a routine cannot reach the channels your Python jobs use unless you
add those domains to the environment's allowed domains. Connectors are not affected, since
their traffic goes through Anthropic's servers.
