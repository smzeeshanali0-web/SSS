import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("VOICE_PROVIDER", "test")
    monkeypatch.setenv("ALLOW_TEST_PROVIDER", "1")
    import app.main as m
    m._store = None
    return TestClient(m.app)


H = {"X-API-Key": "k"}


def test_health_and_auth(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/v1/voice-profiles").status_code == 401
    assert client.get("/v1/voice-profiles", headers=H).status_code == 200


def test_consent_flow(client):
    r = client.post("/v1/voice-profiles", headers=H, files={"file": ("a.mp3", b"ID3xx")}, data={"name": "n", "owner_name": "Sonya", "owner_relation": "self"})
    pid = r.json()["id"]
    assert client.post(f"/v1/voice-profiles/{pid}/clone", headers=H).status_code == 403
    assert client.post(f"/v1/voice-profiles/{pid}/consent", headers=H, json={"accept": True, "disclosure_ack": True, "typed_name": "Sonya"}).json()["status"] == "consented"
    assert client.post(f"/v1/voice-profiles/{pid}/clone", headers=H).json()["status"] == "ready"
    assert client.delete(f"/v1/voice-profiles/{pid}", headers=H).json()["deleted"]
