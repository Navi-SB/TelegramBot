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
    # WhatsApp (Meta Cloud API) — all optional so a Telegram-only deployment
    # never fails to start; the WhatsApp endpoints check for what they need.
    wa_access_token: str | None = None
    wa_phone_number_id: str | None = None
    wa_verify_token: str | None = None
    wa_app_secret: str | None = None
    wa_waba_id: str | None = None
    wa_graph_version: str = "v26.0"
    # Outbound pushes from VoyageCalc (a workflow on a schedule). Its OWN
    # secret, never internal_secret: that one is Vercel signing a call to
    # itself and lives on one machine, while this has to live on the VPS too
    # and authorises a different thing. Comma-separated for rotation. Unset =
    # /api/push answers 503 and nothing can be pushed.
    push_secret: str | None = None

    @property
    def deadline_seconds(self) -> float:
        """Stop with a message the user can act on, rather than being cut off
        mid-turn by the platform's own timeout."""
        return max(10.0, self.max_duration - 25)

    @property
    def whatsapp_ready(self) -> bool:
        return bool(self.wa_access_token and self.wa_phone_number_id
                    and self.wa_app_secret and self.wa_verify_token)


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
        wa_access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN") or None,
        wa_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or None,
        wa_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN") or None,
        wa_app_secret=os.environ.get("WHATSAPP_APP_SECRET") or None,
        wa_waba_id=os.environ.get("WHATSAPP_WABA_ID") or None,
        wa_graph_version=os.environ.get("WHATSAPP_GRAPH_VERSION", "v26.0"),
        push_secret=os.environ.get("BOT_PUSH_SECRET") or None,
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

    # Same failure mode, WhatsApp shape: webhook config is per-Meta-app, so a
    # preview can't steal it — but a preview SENDING from the production number
    # is just as bad. Refuse a non-production deploy holding the prod number.
    wa_prod = os.environ.get("WHATSAPP_PROD_PHONE_NUMBER_ID", "").strip()
    if env != "production" and wa_prod and cfg.wa_phone_number_id == wa_prod:
        raise ConfigError(
            "This is a non-production deployment holding the PRODUCTION "
            "WhatsApp number. Give Preview and Development the test number."
        )
    return cfg
