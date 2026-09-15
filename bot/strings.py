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
    "Just send a message to talk to your agent.\n"
    "Send a PDF or Word file — a Q88, a recap — and add a caption to say what "
    "you want.\n\n"
    "/agents — pick which of your agents to talk to\n"
    "/agent &lt;name&gt; — switch straight to an agent by name\n"
    "/short — turn a pasted vessel description into a broker short description\n"
    "/new — start a fresh conversation with the current agent\n"
    "/status — what I'm connected to\n"
    "/unlink — disconnect this chat\n"
    "/help — this message\n\n"
    "Plain words work too: “switch to &lt;name&gt;”, “list my agents”, "
    "“new conversation”.\n\n"
    "<i>Library management — tags, favourites and columns — is web-app only.</i>\n"
    "<i>Telegram chats are not end-to-end encrypted.</i>"
)

GROUPS_UNSUPPORTED = (
    "I only work in a direct chat. In a group I can't tell whose account to "
    "use, and I'd rather not guess with a write-capable agent."
)

TEXT_ONLY = (
    "I can read text, PDF files and Word (.docx) files — not photos, voice "
    "notes or other media yet. Paste the text, or send the document itself "
    "as a file."
)

THINKING = "⏳ Thinking…"

READING_FILE = "📄 Reading the file…"

# The limit is bot/core/attachments.py's MAX_BYTES; a test keeps them in step.
FILE_TOO_LARGE = (
    "📄 That file is too big — I can read files up to <b>5 MB</b>. Try a "
    "smaller export, or paste the part you need as text."
)

FILE_UNAVAILABLE = (
    "📄 I couldn't download that file from Telegram. <b>It wasn't "
    "processed</b> — try sending it again."
)

# The platform's proxy refused the upload (a 413) although the file is within
# the cap: a deployment limit, not something a smaller file would fix.
FILE_NOT_DELIVERED = (
    "📄 I couldn't pass that file on to Tropis. <b>It wasn't processed</b> — "
    "try again later, or paste the text."
)

FILES_NOT_SUPPORTED_YET = (
    "📄 I can't read files here yet — the platform needs an update first. "
    "Paste the text for now."
)

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

PICK_FORMAT = "Which short-description format?"

TOOLS_UNAVAILABLE = (
    "Short descriptions aren't available here yet — the platform needs an "
    "update first."
)

NO_ANSWER = "<i>(no answer)</i>"

APPROVED = "✅ Change approved."

REJECTED = "❌ Change rejected."

ALREADY_RESOLVED = "Already resolved."

NO_AGENTS = (
    "You haven't built any agents yet. Create one at "
    "<a href=\"https://tropishq.com/agent-studio\">Agent Studio</a>, "
    "or just message me and I'll answer in plain text."
)

# The way back to the menu, said wherever a choice is confirmed. The menu shows
# up by itself only straight after linking, and a tester who wanted another
# agent days later never found /agents at all — they asked the AI instead.
SWITCH_HINT = "Switch agents any time with /agents."

FULL_TEXT_SELECTED = (
    "📄 Now answering in <b>plain text</b> — no agent formatting.\n" + SWITCH_HINT
)


def linked(name: str, account: str | None) -> str:
    import html as _h

    # The account is the email, or "Acme Shipping (ops1)" for a company login,
    # which is why it follows a dash rather than sitting in brackets.
    where = f" — {_h.escape(account, quote=False)}" if account else ""
    return (
        f"🔗 Connected to <b>{_h.escape(name, quote=False)}</b>{where}.\n"
        "Switch agents any time with /agents, or just say "
        "“switch to &lt;name&gt;”."
    )


def active_gone(kind: str, agents_of: str | None = None) -> str:
    """Said before /new's or /status's reply when the chat's agent, format or
    workflow can no longer be used, and the chat has just moved to plain text."""
    import html as _h

    if kind == "workflow":
        what = "The workflow this chat was using is switched off or was deleted"
    else:
        where = (f"<b>{_h.escape(agents_of, quote=False)}</b>" if agents_of
                 else "the account this chat is connected to")
        noun = "format" if kind == "format" else "agent"
        what = f"The {noun} this chat was using was deleted or isn't on {where}"
    return f"⚠️ {what}, so this chat now answers in plain text.\n"


def agent_selected(name: str, turns: int, rotated: bool) -> str:
    if rotated or turns == 0:
        head = f"● Now talking to <b>{name}</b> — starting a fresh conversation."
    else:
        head = f"● Now talking to <b>{name}</b> — resuming your {turns}-message conversation."
    return f"{head}\n{SWITCH_HINT}"


def new_thread(name: str | None) -> str:
    who = f"<b>{name}</b>" if name else "plain text"
    return (
        f"🆕 Fresh conversation with {who}. The previous one is still in the web app.\n"
        + SWITCH_HINT
    )


def pick_agent(agents_of: str | None = None) -> str:
    """The agent menu's header, naming whose agents these are when the
    platform says. Said up front, a menu missing the agent you want (because
    it lives under the other account) explains itself."""
    if not agents_of:
        return PICK_AGENT
    import html as _h

    return f"{PICK_AGENT}\n<i>Showing the agents for {_h.escape(agents_of, quote=False)}.</i>"


def status(account: str, agent: str | None, turns: int, agents_of: str | None = None) -> str:
    import html as _h

    # Left out when it would only repeat the account on the line above.
    scope = (
        f"<b>Agents from:</b> {_h.escape(agents_of, quote=False)}\n"
        if agents_of and agents_of != account else ""
    )
    return (
        f"<b>Account:</b> {_h.escape(account, quote=False)}\n"
        + scope
        + f"<b>Agent:</b> {agent or 'plain text (no agent)'}\n"
        f"<b>Conversation:</b> {turns} message(s)"
    )


def rate_limited(wait: str, *, daily: bool, file: bool) -> str:
    """The platform turned the turn away for sending too much (a 429). `wait`
    is how long, in words ("a minute", "about 3 hours")."""
    what = "that file wasn't read" if file else "that message wasn't processed"
    if daily:
        return (f"⏳ You've reached the daily limit of messages to me, so <b>{what}</b>. "
                f"Send it again in {wait}.")
    many = "files" if file else "messages"
    return (f"⏳ That's more {many} than I can take at once, so <b>{what}</b>. "
            f"Send it again in {wait}.")


def agent_error(error_class: str) -> str:
    # The exception CLASS only — messages can carry paths, SQL or key material.
    return f"❌ The agent hit an error (<code>{error_class}</code>). Try rephrasing, or /new."


def tool_selected(name: str) -> str:
    return (
        f"📝 <b>{name}</b> — paste a full vessel description or fixture recap "
        "and I'll return the compact broker line, ready to forward.\n"
        "Back to your agents with /agents."
    )


def no_agent_match(query: str, agents_of: str | None = None) -> str:
    import html as _h

    q = _h.escape(query, quote=False)
    if not agents_of:
        return f"No agent matches <b>{q}</b>. Try /agents."
    # Naming whose agents were searched is what makes "I'm linked to the wrong
    # account" visible from the chat. Only a linked chat ever gets here.
    return (
        f"No agent matches <b>{q}</b> in the agents for "
        f"<b>{_h.escape(agents_of, quote=False)}</b>. Try /agents."
    )


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
