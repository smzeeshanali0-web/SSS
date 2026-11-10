"""Voice profile lifecycle with an explicit consent gate.

awaiting_consent --accept--> consented --clone--> ready      (revoke/delete -> provider voice deleted)
awaiting_consent --decline--> (profile + sample deleted)
Cloning and AI-voice calls are impossible unless an un-revoked consent record exists for the profile.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Optional

from . import config, providers
from .store import Store

CONSENT_VERSION = "v1"
CONSENT_TEXT = (
    "Voice cloning consent. By accepting you confirm that: (1) the voice in this recording is YOUR OWN voice, "
    "or you have the voice owner's explicit permission to clone it and use it on calls; (2) you will not use the cloned voice "
    "to impersonate a real person to deceive, defraud or harass anyone; (3) you will tell the person you call that the voice is "
    "AI-generated where required by law or by the provider's terms; (4) you may revoke this consent at any time, which deletes the voice. "
    "Recordings of singers, celebrities or other people WITHOUT their permission are not allowed."
)


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class VoiceService:
    def __init__(self, store: Store):
        self.store = store

    def public(self, p: dict) -> dict:
        return {k: v for k, v in p.items() if k not in ("sample_path", "provider_voice_id")}

    def create_profile(self, *, name: str, owner_name: str, owner_relation: str, filename: str, data: bytes) -> dict:
        name, owner_name = name.strip(), owner_name.strip()
        if not name or not owner_name:
            raise ServiceError(422, "Profile name and voice owner name are required.")
        if owner_relation not in ("self", "other_with_permission"):
            raise ServiceError(422, "owner_relation must be 'self' or 'other_with_permission'.")
        ext = Path(filename or "").suffix.lower()
        if ext not in config.ALLOWED_EXT:
            raise ServiceError(415, f"Supported formats: {', '.join(sorted(config.ALLOWED_EXT))}")
        if not data:
            raise ServiceError(422, "Empty file.")
        if len(data) > config.MAX_UPLOAD_MB() * 1024 * 1024:
            raise ServiceError(413, f"File exceeds {config.MAX_UPLOAD_MB()} MB.")
        dest = config.data_dir() / "samples" / f"{uuid.uuid4()}{ext}"
        dest.write_bytes(data)
        p = self.store.add_profile(name=name, owner_name=owner_name, owner_relation=owner_relation, sample_path=str(dest),
                                   sample_sha256=hashlib.sha256(data).hexdigest(), status="awaiting_consent")
        self.store.audit("profile_created", p["id"], f"owner={owner_name} relation={owner_relation}")
        return {**self.public(p), "consent_text": CONSENT_TEXT, "consent_version": CONSENT_VERSION}

    def _get(self, pid: str) -> dict:
        p = self.store.profile(pid)
        if not p:
            raise ServiceError(404, "Voice profile not found.")
        return p

    def respond_consent(self, pid: str, *, accept: bool, disclosure_ack: bool, typed_name: str = "", permission_note: str = "", client_ip: str = "") -> dict:
        p = self._get(pid)
        if p["status"] != "awaiting_consent":
            raise ServiceError(409, f"Profile is '{p['status']}', not awaiting consent.")
        if not accept:
            self._purge_sample(p)
            self.store.audit("consent_declined", pid, "profile and sample deleted")
            self.store.delete_profile(pid)
            return {"status": "declined", "message": "Consent declined. Sample and profile deleted; nothing was cloned."}
        if not disclosure_ack:
            raise ServiceError(403, "You must acknowledge the AI-voice disclosure.")
        if typed_name.strip().casefold() != p["owner_name"].strip().casefold():
            raise ServiceError(403, "Type the voice owner's name exactly as entered on the profile to confirm consent.")
        if p["owner_relation"] != "self" and len(permission_note.strip()) < 10:
            raise ServiceError(403, "For someone else's voice, describe how/when they gave permission (min 10 characters).")
        cid = self.store.add_consent(profile_id=pid, text_version=CONSENT_VERSION, owner_name=p["owner_name"], owner_relation=p["owner_relation"],
                                     typed_name=typed_name.strip(), permission_note=permission_note.strip(), sample_sha256=p["sample_sha256"], client_ip=client_ip)
        self.store.update_profile(pid, status="consented")
        self.store.audit("consent_accepted", pid, f"consent={cid}")
        return {"status": "consented", "consent_id": cid, "message": "Consent recorded. You can now create the voice clone."}

    async def clone(self, pid: str, elevenlabs_key: str = "") -> dict:
        p = self._get(pid)
        if p["status"] not in ("consented", "failed") or not self.store.active_consent(pid):
            raise ServiceError(403, "Cloning requires an accepted, un-revoked consent for this profile.")
        try:
            provider = providers.get_provider(elevenlabs_key)
        except providers.ProviderNotConfigured as e:
            raise ServiceError(501, str(e))
        self.store.update_profile(pid, status="cloning", error=None)
        try:
            vid = await provider.create_voice(p["name"], Path(p["sample_path"]))
        except Exception as e:
            self.store.update_profile(pid, status="failed", error=str(e)[:300])
            self.store.audit("clone_failed", pid, str(e)[:300])
            raise ServiceError(502, f"Clone failed: {e}")
        self.store.update_profile(pid, status="ready", provider=provider.name, provider_voice_id=vid)
        self.store.audit("clone_created", pid, f"provider={provider.name}")
        import os
        if os.getenv("KEEP_SAMPLES") != "1":
            self._purge_sample(p)
            self.store.update_profile(pid, sample_path=None)
        return self.public(self._get(pid))

    async def preview(self, pid: str, pcm: bytes, elevenlabs_key: str = "") -> bytes:
        """Pre-call check: convert a short recording (PCM16 16 kHz mono) to the cloned voice. Same consent gate as calls."""
        self.usable_voice(pid)
        if len(pcm) < 16000 * 2:
            raise ServiceError(422, "Record at least 1 second of speech.")
        if len(pcm) > 16000 * 2 * 20:
            raise ServiceError(413, "Test recording must be 20 seconds or shorter.")
        try:
            provider = providers.get_provider(elevenlabs_key)
        except providers.ProviderNotConfigured as e:
            raise ServiceError(501, str(e))
        try:
            out = await provider.convert(self.store.profile(pid)["provider_voice_id"], pcm[: len(pcm) // 2 * 2])
        except Exception as e:
            raise ServiceError(502, f"Voice conversion failed: {e}")
        self.store.audit("preview", pid, f"{len(pcm)} bytes")
        return out[: len(out) // 2 * 2]

    def usable_voice(self, pid: str) -> tuple[dict, str]:
        """Return (profile, provider_voice_id) only if cloned AND consent is active. Used by call setup."""
        p = self._get(pid)
        if p["status"] != "ready" or not p["provider_voice_id"] or not self.store.active_consent(pid):
            raise ServiceError(403, "Voice profile is not ready or its consent is missing/revoked.")
        return p, p["provider_voice_id"]

    async def delete(self, pid: str, elevenlabs_key: str = "") -> dict:
        """Revoke consent, delete the provider voice and local data."""
        p = self._get(pid)
        self.store.revoke_consents(pid)
        warn = ""
        if p.get("provider_voice_id"):
            try:
                await providers.get_provider(elevenlabs_key).delete_voice(p["provider_voice_id"])
            except Exception as e:
                warn = f"Provider voice could not be deleted automatically: {e}"
        self._purge_sample(p)
        self.store.audit("profile_deleted", pid, warn or "ok")
        self.store.delete_profile(pid)
        return {"deleted": True, "warning": warn}

    def rename(self, pid: str, name: str) -> dict:
        self._get(pid)
        if not name.strip():
            raise ServiceError(422, "Name required.")
        self.store.update_profile(pid, name=name.strip())
        return self.public(self._get(pid))

    @staticmethod
    def _purge_sample(p: dict):
        if p.get("sample_path"):
            Path(p["sample_path"]).unlink(missing_ok=True)
