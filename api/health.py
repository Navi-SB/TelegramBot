"""Deploy smoke test. Reports config validity WITHOUT echoing any secret."""
from bot.asgi import Resp, json_endpoint


async def handle(_req):
    try:
        from bot.config import load

        cfg = load()
        return Resp(200, {
            "ok": True,
            "env": cfg.vercel_env,
            "api_base": cfg.api_base,
            "self_url": cfg.self_url,
            "max_duration": cfg.max_duration,
            "whatsapp": cfg.whatsapp_ready,
        })
    except Exception as exc:
        # The message names which variable is missing, which is the whole
        # point of this endpoint. It never contains a value.
        return Resp(500, {"ok": False, "error": str(exc)})


app = json_endpoint(handle, methods=("GET",))
