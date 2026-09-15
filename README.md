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
python3 -c "import secrets; print('PUSH_SECRET    :', secrets.token_urlsafe(32))"
```

Each of these is ONE value for the whole deployment — a password between two
machines, not anything per user. You generate them once at setup and never
touch them again except to rotate.

`PUSH_SECRET` is only needed if workflows send on a schedule (NAV-77). It is
deliberately NOT `INTERNAL_SECRET`: that one is Vercel signing a call to
itself and lives on this machine alone, while this one also lives on the
VoyageCalc VPS and authorises a different thing — "speak as the bot to a chat".
Different power, different key, different blast radius if one leaks.

### 2. Tell VoyageCalc about the bot

In `backend/.env` on the VPS, then `sudo systemctl restart voyagecalc`:

```
VOYAGECALC_SERVICE_TOKEN_HASHES=<the sha256 from above>
TELEGRAM_BOT_USERNAME=tropisHQ_bot

# Only for scheduled workflows (NAV-77). Same value as BOT_PUSH_SECRET here.
BOT_PUSH_SECRET=<the PUSH_SECRET from above>
BOT_PUSH_URL=https://tgbot.tropishq.com/api/push
VOYAGECALC_SCHEDULER=1
```

Both token variables accept a **comma-separated list**, so either can be
rotated without downtime: add the new value on both sides, deploy, then drop
the old one.

WHO a pushed message goes to is not decided by this secret. That comes from
`channel_links` — the row written when that particular broker redeemed a
pairing code in their own chat. The secret says "this caller is our backend";
the link row says "this is that person's Telegram". `/api/push` re-checks the
link before it sends anything, which is why a leaked push secret still cannot
message an arbitrary chat id.

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
| *(a PDF or Word file)* | read it; the caption, if any, says what to do with it |
| `/agents` | pick which of your agents to use |
| `/agent <name>` | pick by name (exact first, then partial) |
| `/short` | pick a short-description format, then paste vessel descriptions |
| `/new` | fresh conversation with the current agent |
| `/status` | account, whose agents, agent, conversation length |
| `/unlink` | disconnect this chat (two-step) |
| `/help` | answered with no network call |

Plain words work too, in every mode (an agent, Full text, `/short`, a ⚡
workflow): "switch to PMX Short", "list my agents", "new conversation".
VoyageCalc recognises those inside `/api/bot/turn` before any AI runs, so
they are not billed as turns and the bridge needs no parser for them. When
a request doesn't settle on one agent, the turn result carries
`"menu": "agents"` and the bridge opens the agent menu under the reply.

The menu header, `/status` and a failed `/agent <name>` name the account
whose agents they show when the platform sends that label (`"account"`).
A company seat sees the company's shared agents and a personal account only
its own, so an agent missing from the menu is usually under the other one.
Pairing and `/status` show that label in place of a company login's email,
which is a placeholder under the reserved `.invalid` domain.

When the chat's agent was deleted in the web app (or its ⚡ workflow switched
off), `GET /api/bot/agents` returns no `active_preset_id` and sets `"active_gone"` to
`"agent"`, `"format"` or `"workflow"`. `/new` and `/status` then move the chat
to plain text, and their reply says why first.

Every command in `/help` has to be registered in `scripts/set_webhook.py`
(a test holds them together); rerun that script after changing the list.

---

## Files (PDF and Word)

Send a PDF (text or scanned) or a Word `.docx` — a Q88, a recap — with or
without a caption. The caption is the instruction ("pmx format", "add these
fixtures") and is never read as a command, so `/new` under a file is about
the file.

The bridge **fetches and forwards; it never reads**. Only it holds the
Telegram and WhatsApp tokens, so it downloads the file and posts it to
`/api/bot/turn` next to the caption:

```json
{"channel": "telegram", "chat_id": "…", "content": "<caption or empty>",
 "attachment": {"filename": "…", "mime_type": "…", "data_b64": "<base64>"}}
```

VoyageCalc sniffs the bytes and decides what the file is. A legacy `.doc`,
a `.docm`, a spreadsheet or garbage comes back as an ordinary reply
explaining why (`stop_reason: attachment_rejected`), and the bridge delivers
it like any other answer. Text turns send no `attachment` key at all.

| | Telegram | WhatsApp |
|---|---|---|
| Arrives as | `document.file_id` | `document.id` (a media id) |
| Download | `getFile` → `api.telegram.org/file/bot<token>/<path>` | `GET /{media_id}` → a URL valid 5 min → GET with the Bearer token |
| Integrity | — | every sha256 Meta supplies must match (hex or base64) |
| While downloading | placeholder reads "📄 Reading the file…" | nothing extra (no edits, and sends cost money) |

The rules, all in `bot/core/`:

- **5 MB cap** (`attachments.MAX_BYTES`, 5 MiB — exactly VoyageCalc's
  `attachments.MAX_ATTACHMENT_BYTES`, which its web page uses too; a test on
  each side pins it). A file *declared* bigger is refused without
  downloading; the download itself streams and stops the moment it passes
  the cap, because declared sizes and `Content-Length` are hints.
  Telegram's cloud Bot API can't hand out files over 20 MB at all — that
  also reads as "too big".
- **Linked chats only.** The file is fetched after the link check, so an
  unlinked chat can never make the bot download anything. VoyageCalc answers
  that check as unlinked for a suspended account too, and if a chat is
  refused (403) by the time its turn arrives, it gets the pairing message
  rather than "something went wrong".
- **Inside the turn deadline.** The download gets at most half of it (and
  never more than 60 s); whatever it uses comes off the backend call.
- **Every failure has its own message**: too large, couldn't download
  (retry), couldn't pass it on (a 413 from a proxy in front of the backend —
  never the file's size, since nothing over the cap is sent), and — for an
  old backend that 422s a file turn — "can't read files here yet". Photos,
  voice notes, stickers, GIFs and the rest still get the text-only nudge,
  now naming what *does* work.
- **Deploy VoyageCalc first, and check its proxy's body limit.** An old
  backend ignores the unknown `attachment` key: a caption-less file 422s (and
  says "not yet"), but a captioned one would be answered as if only the
  caption had been sent. A 5 MiB file is ~7 MB of JSON, so any body limit in
  front of VoyageCalc must allow that. Its Caddy sets none today;
  VoyageCalc's `deploy/nav-81-sent-files.md` shows how to check, and the
  `8MB` to use if one is added.

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
fixture data in a third-party system, and that one is avoidable. Files add
three more never-logs: the file name (it often names an owner, charterer or
vessel), the caption, and the download URL — Telegram's has the bot token in
its path, which is also why httpx's own request logging is held at WARNING.
Refused media is logged as `media_refused` with its kind and declared MIME
type only; a failed download as `attachment_failed` with a short reason code.

## WhatsApp (Meta Cloud API)

The same core now serves WhatsApp: `bot/core/dispatch.py` holds every flow
once, `bot/telegram/` and `bot/whatsapp/` adapt it per channel, and one Vercel
project answers both webhooks. The backend was channel-agnostic from day one —
`channel='whatsapp'` rides the same `/api/bot/*` surface and tables.

What differs from Telegram, by design:

| | Telegram | WhatsApp |
|---|---|---|
| Pairing | `t.me/...?start=<code>` deep link | `wa.me/<number>?text=LINK <code>` prefill — the user must press Send |
| Commands | `/commands` menu | bare words (`agents`, `short`, `new`, `status`, `unlink`, `help`); `/forms` still accepted, so `/agent <name>` works; multi-word text is never swallowed, `agent <name>` included — "switch to <name>" is the plain-words way |
| Progress | "⏳ Thinking…" edited into the answer | read receipt + typing indicator, then buffered sends (no edit API exists) |
| Menus | inline keyboards, 8/page | list message, 10-row ceiling: 7 agents + Full text + nav |
| Confirm cards | ≤3500 chars under the keyboard | ≤950 chars (interactive body caps at 1024) |
| Blocked | inbound `my_chat_member` | error 131026 on a send → `mark_blocked`, once |
| Dedupe key | `update_id` | per-message `wamid` (one POST can batch several) |
| Files | `getFile` + tokenised download URL | media id → fresh 5-minute URL → Bearer download, sha256-checked |
| Dev loop | `scripts/dev_poll.py` | webhook-only — use the dashboard test number + a tunnel (no polling exists) |

Because the bot only ever replies inside the 24-hour service window, no paid
message templates are needed for any flow. (Meta ends free service messages on
2026-10-01 — per-message rates land by 2026-09-01; that changes cost, not code.)

### WhatsApp setup, in order

1. **Start Business Verification immediately** (business.facebook.com — takes
   3–10 days and gates only production; everything below works on the free
   test number meanwhile).
2. developers.facebook.com → create a **Business** app → add the WhatsApp
   product. Note the test **phone number id** and **WABA id**; add up to 5
   SMS-verified recipient numbers.
3. Business Settings → System User (admin) → assign the app + WABA → generate
   a **permanent token** with `whatsapp_business_messaging` and
   `whatsapp_business_management` → `WHATSAPP_ACCESS_TOKEN`. App Settings →
   Basic → App Secret → `WHATSAPP_APP_SECRET`. Mint `WHATSAPP_VERIFY_TOKEN`
   yourself (`secrets.token_urlsafe(32)`).
4. Set the `WHATSAPP_*` vars in Vercel (see `.env.example`) and deploy; check
   `/api/health`.
5. Dashboard → WhatsApp → Configuration → Webhook:
   URL `https://tgbot.tropishq.com/api/whatsapp`, your verify token →
   Verify and save → subscribe to the **`messages`** field only. Then
   `python scripts/wa_subscribe.py`.
6. Smoke: text the bot number from a verified recipient phone, or
   `python scripts/wa_send_test.py <number>` after messaging it first.
7. Backend: set `WHATSAPP_BOT_NUMBER` on the VPS so `mint_link_code` can build
   the `wa.me` link, and add the frontend Connect WhatsApp card (VoyageCalc
   repo — see NAV-36).
8. Production, after verification passes: dedicated SIM never used on consumer
   WhatsApp → register the number (6-digit PIN) → display name "Tropis" →
   swap `WHATSAPP_PHONE_NUMBER_ID` + set `WHATSAPP_PROD_PHONE_NUMBER_ID` so
   previews refuse the prod number. Keep the test number + a second dev Meta
   app pointed at a tunnel as the dev loop.

### WhatsApp things that will bite you

- **Statuses ride the same webhook field.** Three receipts arrive per outbound
  message and cannot be unsubscribed — `api/whatsapp.py` filters them before
  they wake a worker. Don't "fix" that filter away.
- **Meta re-delivers for up to 36h** and batches messages: the worker claims
  each wamid separately, so a redelivered batch replays only what didn't
  finish.
- **The typing indicator lasts ~25s**; a 30–90s turn goes visually quiet after
  that. Known trade-off — there is no edit API to stream into.
- **`hub.challenge` must echo as plain text**, not JSON — that's why
  `Resp(text=...)` exists in `bot/asgi.py`.
- **Business-specific assistants are allowed** under Meta's 2026 AI policy;
  general-purpose ones are not. Keep the display name and copy "Tropis
  assistant", never "chat with an AI".
