# Tropis Telegram bot

Talk to the agents you built in [Agent Studio](https://tropishq.com/agent-studio)
from Telegram. `@tropisHQ_bot`.

Deployed to Vercel as Python functions. It holds **no state of its own** —
every link, session and thread lives in VoyageCalc, reached only over the
public HTTPS API at `/api/bot/*` with a service token.

---

## How a message flows

```
Telegram ──webhook──► api/telegram.py          VoyageCalc (tropishq.com)
                      verify secret            ┌────────────────────────┐
                      shape-check              │                        │
                      self-invoke ──┐          │                        │
                      200 in <600ms │          │                        │
                                    ▼          │                        │
                      api/worker.py ──────────►│ POST /api/bot/turn     │ 30–90s
                      claim (dedupe)           │   agent runs, reply    │
                      run + render ◄───────────│   assembled here       │
                      edit placeholder         └────────────────────────┘
```

The webhook does nothing slow. Telegram re-delivers any update it doesn't get
a 200 for within ~60s, and repeated timeouts become a retry storm where the
same message arrives dozens of times — so the turn happens in a second
function and the first one acks immediately.

`waitUntil` would be the obvious way to do that, but it **has no Python
surface** on Vercel (it's an export of the `@vercel/functions` npm package).
Hence the signed self-invoke in `bot/selfinvoke.py`.

---

## Setup

### 1. Generate the secrets

```bash
python3 -c "import secrets; print('WEBHOOK_SECRET :', secrets.token_urlsafe(32))"
python3 -c "import secrets; print('INTERNAL_SECRET:', secrets.token_urlsafe(32))"
python3 -c "import hashlib,secrets; t=secrets.token_urlsafe(32); \
print('SERVICE_TOKEN  :', t); print('  -> hash for VoyageCalc:', hashlib.sha256(t.encode()).hexdigest())"
```

### 2. Tell VoyageCalc about the bot

In `backend/.env` on the VPS, then `sudo systemctl restart voyagecalc`:

```
VOYAGECALC_SERVICE_TOKEN_HASHES=<the sha256 from above>
TELEGRAM_BOT_USERNAME=tropisHQ_bot
```

Until that's set, `/api/bot/*` returns **503** — the surface is closed by
default, so deploying it before the bot exists opens nothing.

### 3. Deploy

Set every variable from `.env.example` in the Vercel project, then deploy.

Put the bot on a **custom domain** (`tgbot.tropishq.com`). Vercel's Deployment
Protection covers generated `*.vercel.app` production URLs, and Telegram would
get a 401 on every delivery.

### 4. Prove the self-invoke survives

The whole webhook design rests on one platform behaviour: a self-invoked
function outliving the caller that abandoned its connection. Verify it before
it matters.

```bash
curl https://tgbot.tropishq.com/api/spike
# wait ~130s, then look for "spike_survived" in the Vercel runtime logs
```

If it never appears, swap `bot/selfinvoke.py` for a durable queue (Upstash
QStash publishes over HTTP and gives retries and dedupe for free). Nothing else
in the design changes.

### 5. Point Telegram at it

```bash
python scripts/set_webhook.py          # set the webhook + register commands
python scripts/set_webhook.py --info   # what Telegram currently thinks
```

### 6. Pair a chat

tropishq.com → Settings → Telegram → Connect, then tap the `t.me` link. Codes
are single-use and last 10 minutes.

---

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest            # no token or network needed

python scripts/set_webhook.py --delete   # Telegram allows one at a time
python scripts/dev_poll.py               # long polling, no tunnel, no Vercel
```

`scripts/dev_poll.py` is also **the VPS escape hatch, exercised**. If Vercel
ever stops suiting the job, a systemd unit runs that file: it calls the same
`handle_update()` the serverless worker does. Everything in `bot/` is free of
Vercel imports specifically so that port stays a deployment change rather than
a rewrite — and developing locally keeps it honest.

---

## Commands

| | |
|---|---|
| *(any message)* | ask the current agent |
| `/agents` | pick which of your agents to use |
| `/agent <name>` | pick by name |
| `/new` | fresh conversation with the current agent |
| `/status` | account, agent, conversation length |
| `/unlink` | disconnect this chat (two-step) |
| `/help` | answered with no network call |

---

## Things that will bite you

**Use a separate `@..._dev_bot` for Preview and Development.** `setWebhook` is
global to a bot, so any branch push could otherwise take over the production
webhook. `bot/config.py` refuses to start a non-production deploy holding a
token that ends in `TELEGRAM_PROD_BOT_TOKEN_SUFFIX`.

**Hobby is non-commercial-use only** and keeps runtime logs for one hour, which
is unusable for debugging a bot.

**Library management is web-only.** Tags, favourites, columns and charts are
hidden from the bot's tool profile — they mutate browser state or render in the
web UI, so from a chat client they'd either do nothing or produce a turn where
the user receives nothing at all.

**Nothing sensitive is logged.** No message text, agent output, rendered
templates, tool arguments, pairing codes or tokens. Telegram already stores
chat content; Vercel's logs would be a second copy of commercially sensitive
fixture data in a third-party system, and that one is avoidable.
