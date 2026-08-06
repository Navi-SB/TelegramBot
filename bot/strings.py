"""Every user-visible string, in one file.

NOT_LINKED is internet-facing: Telegram bots are publicly discoverable by
username, so anyone can find this bot and message it. That string is constant
for every unlinked chat, reveals nothing, and must never hint at whether an
account exists.
"""

NOT_LINKED = (
    "👋 This is the <b>Tropis</b> assistant.\n\n"
    "This chat isn't connected to a Tropis account.\n\n"
    "To connect:\n"
    "1. Open <a href=\"https://tropishq.com/profile\">tropishq.com/profile</a> and sign in\n"
    "2. Find <b>Telegram</b> → Connect Telegram\n"
    "3. Tap the link it gives you\n\n"
    "<i>I can't create an account from here.</i>"
)

LINK_FAILED = (
    "That link is invalid or has expired — they last 10 minutes and work once.\n\n"
    "Generate a fresh one from tropishq.com/profile → Telegram."
)

HELP = (
    "<b>Tropis assistant</b>\n\n"
    "Just send a message to talk to your agent.\n\n"
    "/agents — pick which of your agents to talk to\n"
    "/new — start a fresh conversation with the current agent\n"
    "/status — what I'm connected to\n"
    "/unlink — disconnect this chat\n"
    "/help — this message\n\n"
    "<i>Library management — tags, favourites and columns — is web-app only.</i>\n"
    "<i>Telegram chats are not end-to-end encrypted.</i>"
)

GROUPS_UNSUPPORTED = (
    "I only work in a direct chat. In a group I can't tell whose account to "
    "use, and I'd rather not guess with a write-capable agent."
)

TEXT_ONLY = (
    "I can only read text right now. Paste vessel descriptions or fixture "
    "lines as text."
)

THINKING = "⏳ Thinking…"

PLATFORM_DOWN = (
    "🔌 I can't reach Tropis right now. <b>Your message wasn't processed</b> — "
    "try again in a minute."
)

TURN_TIMEOUT = (
    "⏱ That one took too long and I had to stop. Try a narrower question, or "
    "/new for a fresh conversation. Long jobs work better in the web app."
)

UNEXPECTED = "❌ Something went wrong on my side. Try again, or /new."

BUSY = "⏳ I'm still working on your previous message — resend once I've replied."

CLIENT_ACTION_UNSUPPORTED = (
    "That's a library action — tags, favourites and columns live in the web app."
)

UNLINK_CONFIRM = (
    "Disconnect this chat from your Tropis account?\n\n"
    "<i>Your data is untouched. You can reconnect any time.</i>"
)

UNLINKED = (
    "Disconnected. Your Tropis data is untouched — reconnect any time from "
    "tropishq.com/profile → Telegram."
)

UNLINK_CANCELLED = "Left connected."

STALE_MENU = "This menu is out of date — send /agents again."

PICK_AGENT = "Which agent should I use?"

PICK_WHICH = "Which one?"

NO_ANSWER = "<i>(no answer)</i>"

APPROVED = "✅ Change approved."

REJECTED = "❌ Change rejected."

ALREADY_RESOLVED = "Already resolved."

NO_AGENTS = (
    "You haven't built any agents yet. Create one at "
    "<a href=\"https://tropishq.com/agent-studio\">Agent Studio</a>, "
    "or just message me and I'll answer in plain text."
)

FULL_TEXT_SELECTED = "📄 Now answering in <b>plain text</b> — no agent formatting."


def linked(name: str, email: str) -> str:
    return f"🔗 Connected to <b>{name}</b> ({email})."


def agent_selected(name: str, turns: int, rotated: bool) -> str:
    if rotated or turns == 0:
        return f"● Now talking to <b>{name}</b> — starting a fresh conversation."
    return f"● Now talking to <b>{name}</b> — resuming your {turns}-message conversation."


def new_thread(name: str | None) -> str:
    who = f"<b>{name}</b>" if name else "plain text"
    return f"🆕 Fresh conversation with {who}. The previous one is still in the web app."


def status(account: str, agent: str | None, turns: int) -> str:
    return (
        f"<b>Account:</b> {account}\n"
        f"<b>Agent:</b> {agent or 'plain text (no agent)'}\n"
        f"<b>Conversation:</b> {turns} message(s)"
    )


def agent_error(error_class: str) -> str:
    # The exception CLASS only — messages can carry paths, SQL or key material.
    return f"❌ The agent hit an error (<code>{error_class}</code>). Try rephrasing, or /new."


def no_agent_match(query: str) -> str:
    import html as _h

    return f"No agent matches <b>{_h.escape(query, quote=False)}</b>. Try /agents."


def confirm_failed(detail: str) -> str:
    # The 409 detail is a server-authored sentence about the user's own data
    # ("Vessel 'X' not found.") — safe to show, and actionable.
    import html as _h

    d = _h.escape(detail[:200], quote=False) if detail else ""
    tail = f"\n<code>{d}</code>" if d else ""
    return (
        "⚠️ That change couldn't be applied — the data may have changed "
        "underneath it. It's still pending; review it in the web app." + tail
    )
