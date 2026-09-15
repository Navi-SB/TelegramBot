"""Every user-visible WhatsApp string, in one file.

Same contract as the Telegram strings module (the core reads them through
ch.S), rewritten in WhatsApp markup and WhatsApp pairing language: there is no
/start deep link — the user sends a prefilled "LINK <code>" message instead.

NOT_LINKED is internet-facing: the bot's number is publicly messageable, so
that string is constant for every unlinked chat, reveals nothing, and must
never hint at whether an account exists.
"""

NOT_LINKED = (
    "👋 This is the *Tropis* assistant.\n\n"
    "This chat isn't connected to a Tropis account.\n\n"
    "To connect:\n"
    "1. Open tropishq.com/profile and sign in\n"
    "2. Find *WhatsApp* → Connect WhatsApp\n"
    "3. Tap the link and send the prefilled message without editing it\n\n"
    "_I can't create an account from here._"
)

LINK_FAILED = (
    "That code is invalid or has expired — codes last 10 minutes and work "
    "once.\n\n"
    "Generate a fresh one from tropishq.com/profile → WhatsApp, and send the "
    "prefilled message without editing it."
)

HELP = (
    "*Tropis assistant*\n\n"
    "Just send a message to talk to your agent.\n\n"
    "*agents* — pick which of your agents to talk to\n"
    "*switch to <name>* — go straight to an agent by name\n"
    "*short* — turn a pasted vessel description into a broker short description\n"
    "*new* — start a fresh conversation with the current agent\n"
    "*status* — what I'm connected to\n"
    "*unlink* — disconnect this chat\n"
    "*help* — this message\n\n"
    "Plain words work too: “list my agents”, “new conversation”.\n\n"
    "_Library management — tags, favourites and columns — is web-app only._"
)

GROUPS_UNSUPPORTED = (
    "I only work in a direct chat. In a group I can't tell whose account to "
    "use, and I'd rather not guess with a write-capable agent."
)

TEXT_ONLY = (
    "I can only read text right now. Paste vessel descriptions or fixture "
    "lines as text."
)

PLATFORM_DOWN = (
    "🔌 I can't reach Tropis right now. *Your message wasn't processed* — "
    "try again in a minute."
)

TURN_TIMEOUT = (
    "⏱ That one took too long and I had to stop. Try a narrower question, or "
    "*new* for a fresh conversation. Long jobs work better in the web app."
)

UNEXPECTED = "❌ Something went wrong on my side. Try again, or send *new*."

UNLINK_CONFIRM = (
    "Disconnect this chat from your Tropis account?\n\n"
    "_Your data is untouched. You can reconnect any time._"
)

UNLINKED = (
    "Disconnected. Your Tropis data is untouched — reconnect any time from "
    "tropishq.com/profile → WhatsApp."
)

UNLINK_CANCELLED = "Left connected."

STALE_MENU = "This menu is out of date — send *agents* again."

NO_AGENTS = (
    "You haven't built any agents yet. Create one at "
    "tropishq.com/agent-studio, or just message me and I'll answer in "
    "plain text."
)

# The way back to the menu, said wherever a choice is confirmed (the Telegram
# module says why). The bare word the parser accepts, as there is no menu here.
SWITCH_HINT = "Send *agents* any time to switch."

FULL_TEXT_SELECTED = (
    "📄 Now answering in *plain text* — no agent formatting.\n" + SWITCH_HINT
)

PICK_AGENT = "Which agent should I use?"

PICK_WHICH = "Which one?"

PICK_FORMAT = "Which short-description format?"

TOOLS_UNAVAILABLE = (
    "Short descriptions aren't available here yet — the platform needs an "
    "update first."
)

NO_ANSWER = "_(no answer)_"

APPROVED = "✅ Change approved."

REJECTED = "❌ Change rejected."

ALREADY_RESOLVED = "Already resolved."


def linked(name: str, email: str) -> str:
    return (
        f"🔗 Connected to *{name}* ({email}).\n"
        "Send *agents* any time to switch, or just say *switch to <name>*."
    )


def agent_selected(name: str, turns: int, rotated: bool) -> str:
    if rotated or turns == 0:
        head = f"● Now talking to *{name}* — starting a fresh conversation."
    else:
        head = f"● Now talking to *{name}* — resuming your {turns}-message conversation."
    return f"{head}\n{SWITCH_HINT}"


def new_thread(name: str | None) -> str:
    who = f"*{name}*" if name else "plain text"
    return (
        f"🆕 Fresh conversation with {who}. The previous one is still in the web app.\n"
        + SWITCH_HINT
    )


def status(account: str, agent: str | None, turns: int) -> str:
    return (
        f"*Account:* {account}\n"
        f"*Agent:* {agent or 'plain text (no agent)'}\n"
        f"*Conversation:* {turns} message(s)"
    )


def agent_error(error_class: str) -> str:
    # The exception CLASS only — messages can carry paths, SQL or key material.
    return f"❌ The agent hit an error (`{error_class}`). Try rephrasing, or send *new*."


def tool_selected(name: str) -> str:
    return (
        f"📝 *{name}* — paste a full vessel description or fixture recap and "
        "I'll return the compact broker line, ready to forward.\n"
        "Send *agents* to go back to your agents."
    )


def no_agent_match(query: str) -> str:
    return f"No agent matches *{query}*. Send *agents* to see the list."


def confirm_failed(detail: str) -> str:
    # The 409 detail is a server-authored sentence about the user's own data
    # ("Vessel 'X' not found.") — safe to show, and actionable.
    tail = f"\n`{detail[:200]}`" if detail else ""
    return (
        "⚠️ That change couldn't be applied — the data may have changed "
        "underneath it. It's still pending; review it in the web app." + tail
    )
