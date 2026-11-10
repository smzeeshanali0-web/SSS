"""HTTP/WebSocket API. Thin layer over service.py (consent/cloning) and session.py (calls)."""
from __future__ import annotations

import hmac
import json
import os
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from . import config, providers
from .service import CONSENT_TEXT, CONSENT_VERSION, ServiceError, VoiceService
from .session import CallSession
from .store import Store

app = FastAPI(title="AI Clone Dialer Backend", version="1.0.0",
              description="Consent-gated voice cloning + SIP call gateway. Set API_KEY and VOICE_PROVIDER (see .env.example).")
_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(config.data_dir() / "app.db")
    return _store


def service() -> VoiceService:
    return VoiceService(store())


def _key_ok(candidate: str) -> bool:
    expected = config.api_key()
    return bool(expected) and hmac.compare_digest(candidate.encode(), expected.encode())


def auth(authorization: str = Header(default=""), x_api_key: str = Header(default="")):
    if not config.api_key():
        raise HTTPException(503, "Server misconfigured: API_KEY is not set.")
    token = x_api_key or authorization.removeprefix("Bearer ").strip()
    if not _key_ok(token):
        raise HTTPException(401, "Invalid API key.")


def _err(e: ServiceError):
    raise HTTPException(e.status, e.message)


@app.get("/health")
def health():
    try:
        provider = providers.get_provider().name
    except providers.ProviderNotConfigured:
        provider = None
    return {"status": "ok", "service": "ai-clone-dialer-backend", "voice_provider": provider, "auth_configured": bool(config.api_key())}


@app.get("/v1/consent-text", dependencies=[Depends(auth)])
def consent_text():
    return {"version": CONSENT_VERSION, "text": CONSENT_TEXT}


@app.post("/v1/voice-profiles", dependencies=[Depends(auth)])
async def create_profile(file: UploadFile = File(...), name: str = Form(..., max_length=80), owner_name: str = Form(..., max_length=80),
                         owner_relation: str = Form("self")):
    """Step 1: upload the sample. Nothing is cloned yet -- profile waits for the owner's consent."""
    data = await file.read(config.MAX_UPLOAD_MB() * 1024 * 1024 + 1)
    try:
        return service().create_profile(name=name, owner_name=owner_name, owner_relation=owner_relation, filename=file.filename or "", data=data)
    except ServiceError as e:
        _err(e)


class ConsentBody(BaseModel):
    accept: bool
    disclosure_ack: bool = False
    typed_name: str = ""
    permission_note: str = ""


@app.post("/v1/voice-profiles/{pid}/consent", dependencies=[Depends(auth)])
def consent(pid: str, body: ConsentBody, request: Request):
    """Step 2: accept (or decline -> everything deleted)."""
    try:
        return service().respond_consent(pid, accept=body.accept, disclosure_ack=body.disclosure_ack, typed_name=body.typed_name,
                                         permission_note=body.permission_note, client_ip=request.client.host if request.client else "")
    except ServiceError as e:
        _err(e)


@app.post("/v1/voice-profiles/{pid}/clone", dependencies=[Depends(auth)])
async def clone(pid: str, x_elevenlabs_key: str = Header(default="")):
    """Step 3: create the clone at the provider. Refused unless consent was accepted."""
    try:
        return await service().clone(pid, x_elevenlabs_key)
    except ServiceError as e:
        _err(e)


@app.post("/v1/voice-profiles/{pid}/preview", dependencies=[Depends(auth)])
async def preview(pid: str, request: Request, x_elevenlabs_key: str = Header(default="")):
    """Pre-call voice check. Body = raw PCM16LE 16 kHz mono (<= 20 s). Returns the same format in the cloned voice."""
    pcm = await request.body()
    try:
        out = await service().preview(pid, pcm, x_elevenlabs_key)
    except ServiceError as e:
        _err(e)
    return Response(content=out, media_type="application/octet-stream")


@app.get("/v1/voice-profiles", dependencies=[Depends(auth)])
def list_profiles():
    svc = service()
    return {"items": [svc.public(p) for p in store().profiles()]}


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@app.patch("/v1/voice-profiles/{pid}", dependencies=[Depends(auth)])
def rename(pid: str, body: Rename):
    try:
        return service().rename(pid, body.name)
    except ServiceError as e:
        _err(e)


@app.delete("/v1/voice-profiles/{pid}", dependencies=[Depends(auth)])
async def delete(pid: str, x_elevenlabs_key: str = Header(default="")):
    """Revokes consent and deletes the voice (provider + local)."""
    try:
        return await service().delete(pid, x_elevenlabs_key)
    except ServiceError as e:
        _err(e)


@app.get("/v1/voice-profiles/{pid}/audit", dependencies=[Depends(auth)])
def audit(pid: str):
    return {"items": store().audit_log(pid)}


@app.get("/v1/calls", dependencies=[Depends(auth)])
def call_history(limit: int = 100):
    return {"items": store().calls(min(limit, 500))}


@app.websocket("/v1/calls/ws")
async def calls_ws(ws: WebSocket):
    """Protocol: first text frame {"type":"start","account":{...},"destination":"...","profile_id":null|"id","disclosure_ack":bool}.
    Then binary frames = mic PCM16LE 16 kHz mono (20 ms = 640 bytes). Server sends binary = remote audio, same format,
    and JSON {"type":"state"|"error"|"warning"|"muted"}. Client JSON: {"type":"dtmf","digit":"5"}, {"type":"mute","on":true}, {"type":"hangup"}."""
    if not _key_ok(ws.query_params.get("token", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    sess = CallSession(store(), service(), ws.send_json, ws.send_bytes)
    try:
        first = await ws.receive_json()
        if first.get("type") != "start":
            await ws.send_json({"type": "error", "message": "First message must be type=start."})
            return
        await sess.start(first)
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if m.get("bytes") is not None:
                await sess.on_bytes(m["bytes"])
            elif m.get("text"):
                await sess.on_json(json.loads(m["text"]))
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        await sess.close()
