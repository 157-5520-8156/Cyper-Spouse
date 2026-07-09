from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from companion_daemon.config import get_settings
from companion_daemon.models import CompanionReply, IncomingMessage, ProactiveDecision
from companion_daemon.qq_official import (
    QQ_CALLBACK_VALIDATION_OP,
    ack_response,
    incoming_message_from_payload,
    validation_response,
    verify_callback_signature,
)
from companion_daemon.runtime import build_companion_engine


app = FastAPI(title="Girl Agent Companion Daemon")
engine = build_companion_engine()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages", response_model=CompanionReply)
async def post_message(message: IncomingMessage) -> CompanionReply:
    if not message.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    return await engine.handle_message(message)


@app.post("/proactive/{canonical_user_id}", response_model=ProactiveDecision)
async def proactive(canonical_user_id: str) -> ProactiveDecision:
    return await engine.proactive_tick(canonical_user_id)


@app.post("/qq/webhook")
async def qq_webhook(
    request: Request,
    x_signature_ed25519: str | None = Header(default=None),
    x_signature_timestamp: str | None = Header(default=None),
) -> JSONResponse:
    settings = get_settings()
    raw_body = await request.body()
    payload = await request.json()

    if settings.qq_verify_signatures and settings.qq_bot_secret:
        if not x_signature_ed25519 or not x_signature_timestamp:
            raise HTTPException(status_code=401, detail="missing QQ signature headers")
        if not verify_callback_signature(
            settings.qq_bot_secret,
            x_signature_timestamp,
            raw_body,
            x_signature_ed25519,
        ):
            raise HTTPException(status_code=401, detail="invalid QQ callback signature")

    if payload.get("op") == QQ_CALLBACK_VALIDATION_OP:
        if not settings.qq_bot_secret:
            raise HTTPException(status_code=500, detail="QQ_BOT_SECRET is required")
        return JSONResponse(validation_response(settings.qq_bot_secret, payload))

    incoming = incoming_message_from_payload(payload)
    if incoming:
        await engine.handle_message(incoming)

    return JSONResponse(ack_response())


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("companion_daemon.app:app", host=settings.host, port=settings.port, reload=True)
