import sakura_common.llm
from sakura_common.settings_client import MASK


def test_llm_test_unconfigured_reports_error(client):
    response = client.post("/api/llm/test", json={})
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert "configured" in body["error"]


def test_llm_test_with_stored_config(client, monkeypatch):
    client.put("/api/settings/llm.provider", json={"value": "anthropic"})
    client.put("/api/settings/llm.model", json={"value": "claude-x"})
    client.put("/api/settings/llm.api_key", json={"value": "sk-1", "is_secret": True})

    monkeypatch.setattr(sakura_common.llm.LLMClient, "complete", lambda self, *a, **k: "OK")

    body = client.post("/api/llm/test", json={}).json()
    assert body == {"ok": True, "reply": "OK"}


def test_llm_test_override_without_saving(client, monkeypatch):
    captured = {}

    def fake_complete(self, *args, **kwargs):
        captured["config"] = self.config
        return "OK"

    monkeypatch.setattr(sakura_common.llm.LLMClient, "complete", fake_complete)

    body = client.post(
        "/api/llm/test",
        json={"provider": "openai", "api_key": "sk-new", "model": "gpt-test"},
    ).json()
    assert body["ok"] is True
    assert captured["config"].provider == "openai"
    assert captured["config"].api_key == "sk-new"
    # Nothing was persisted by a test call.
    assert client.get("/api/settings/llm.provider").status_code == 404


def test_llm_test_mask_falls_back_to_stored_key(client, monkeypatch):
    client.put("/api/settings/llm.provider", json={"value": "anthropic"})
    client.put("/api/settings/llm.model", json={"value": "claude-x"})
    client.put("/api/settings/llm.api_key", json={"value": "sk-stored", "is_secret": True})

    captured = {}

    def fake_complete(self, *args, **kwargs):
        captured["key"] = self.config.api_key
        return "OK"

    monkeypatch.setattr(sakura_common.llm.LLMClient, "complete", fake_complete)

    # The form re-submits the mask it displayed; the real stored key is used.
    body = client.post("/api/llm/test", json={"api_key": MASK}).json()
    assert body["ok"] is True
    assert captured["key"] == "sk-stored"
