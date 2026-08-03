"""Environment, validated once at import so a misconfigured deploy fails loudly
on its first invocation rather than misbehaving quietly."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    webhook_secret: str
    internal_secret: str
    self_url: str
    api_base: str
    service_token: str
    admin_chat_id: str | None
    max_duration: int
    vercel_env: str
    vercel_bypass: str | None

    @property
    def deadline_seconds(self) -> float:
        """Stop with a message the user can act on, rather than being cut off
        mid-turn by the platform's own timeout."""
        return max(10.0, self.max_duration - 25)


def _load_dotenv() -> None:
    """Read a local .env, for scripts and dev_poll.

    On Vercel this file does not exist and real environment variables are
    injected — hence setdefault, so a stale local .env can never shadow
    production. Hand-rolled rather than python-dotenv purely to keep the
    deployed bundle at ONE dependency, which is what makes cold start fast
    enough for a sub-second webhook ack.
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        where = (
            "the Vercel project's environment variables"
            if os.environ.get("VERCEL")
            else "your local .env (copy .env.example)"
        )
        raise ConfigError(f"{name} is not set. Check {where}.")
    return v


def load() -> Config:
    _load_dotenv()
    env = os.environ.get("VERCEL_ENV", "development")
    cfg = Config(
        bot_token=_require("TELEGRAM_BOT_TOKEN"),
        webhook_secret=_require("TELEGRAM_WEBHOOK_SECRET"),
        internal_secret=_require("BOT_INTERNAL_SECRET"),
        # Explicit, never derived from VERCEL_URL: that is the per-deployment
        # URL, which Deployment Protection covers and Telegram would 401 on.
        self_url=_require("BOT_SELF_URL").rstrip("/"),
        api_base=_require("VOYAGECALC_API_BASE").rstrip("/"),
        service_token=_require("VOYAGECALC_SERVICE_TOKEN"),
        admin_chat_id=os.environ.get("BOT_ADMIN_CHAT_ID") or None,
        max_duration=int(os.environ.get("BOT_MAX_DURATION", "290")),
        vercel_env=env,
        vercel_bypass=os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None,
    )

    # A Preview deploy silently stealing the production webhook is a very easy
    # mistake: every branch push creates one, and setWebhook is global to the
    # bot. Refuse rather than let a half-finished branch answer real users.
    prod_marker = os.environ.get("TELEGRAM_PROD_BOT_TOKEN_SUFFIX", "").strip()
    if env != "production" and prod_marker and cfg.bot_token.endswith(prod_marker):
        raise ConfigError(
            "This is a non-production deployment holding the PRODUCTION bot "
            "token. Give Preview and Development their own @..._dev_bot."
        )
    return cfg
