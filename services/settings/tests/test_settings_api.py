from sakura_common.settings_client import MASK


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_put_get_roundtrip(client):
    response = client.put("/api/settings/stocks.fetch_hour_utc", json={"value": "10"})
    assert response.status_code == 200
    assert client.get("/api/settings/stocks.fetch_hour_utc").json()["value"] == "10"


def test_missing_key_404(client):
    assert client.get("/api/settings/nope").status_code == 404


def test_secret_masked_unless_revealed(client):
    client.put("/api/settings/llm.api_key", json={"value": "sk-secret", "is_secret": True})

    assert client.get("/api/settings/llm.api_key").json()["value"] == MASK
    listed = {row["key"]: row for row in client.get("/api/settings").json()}
    assert listed["llm.api_key"]["value"] == MASK
    revealed = client.get("/api/settings/llm.api_key", params={"reveal": "true"}).json()
    assert revealed["value"] == "sk-secret"


def test_resubmitting_mask_keeps_existing_secret(client):
    client.put("/api/settings/llm.api_key", json={"value": "sk-secret", "is_secret": True})
    # A settings form that redisplays the masked value will PUT the mask back.
    client.put("/api/settings/llm.api_key", json={"value": MASK, "is_secret": True})

    revealed = client.get("/api/settings/llm.api_key", params={"reveal": "true"}).json()
    assert revealed["value"] == "sk-secret"


def test_non_secret_can_store_literal_asterisks(client):
    client.put("/api/settings/weird", json={"value": MASK, "is_secret": False})
    assert client.get("/api/settings/weird").json()["value"] == MASK


def test_delete(client):
    client.put("/api/settings/tmp", json={"value": "x"})
    client.delete("/api/settings/tmp")
    assert client.get("/api/settings/tmp").status_code == 404


def test_export_contains_real_secret_values(client):
    client.put("/api/settings/llm.api_key", json={"value": "sk-secret", "is_secret": True})
    exported = client.get("/api/export").json()
    entry = next(row for row in exported["settings"] if row["key"] == "llm.api_key")
    assert entry["value"] == "sk-secret"


def test_import_replaces_everything(client):
    client.put("/api/settings/old", json={"value": "gone"})
    response = client.post(
        "/api/import",
        json={"settings": [{"key": "new", "value": "v", "is_secret": False}]},
    )
    assert response.json() == {"imported": 1}
    assert client.get("/api/settings/old").status_code == 404
    assert client.get("/api/settings/new").json()["value"] == "v"


def test_import_rejects_bad_shape(client):
    assert client.post("/api/import", json={"nope": []}).status_code == 422
    assert client.post("/api/import", json={"settings": [{"value": "no key"}]}).status_code == 422


class TestReset:
    def test_reset_clears_everything_by_default(self, client):
        client.put("/api/settings/llm.api_key", json={"value": "sk-x", "is_secret": True})
        client.put("/api/settings/llm.model", json={"value": "gpt-4"})
        assert client.post("/api/reset", json={}).json()["deleted"]["settings"] == 2
        assert client.get("/api/settings").json() == []

    def test_kept_keys_survive(self, client):
        """The web UI passes its password hash so a factory reset doesn't lock
        the user out of the machine they just reset."""
        client.put("/api/settings/ui.password_hash", json={"value": "hash", "is_secret": True})
        client.put("/api/settings/llm.api_key", json={"value": "sk-x", "is_secret": True})
        result = client.post("/api/reset", json={"keep": ["ui.password_hash"]}).json()
        assert result["deleted"]["settings"] == 1
        assert [row["key"] for row in client.get("/api/settings").json()] == ["ui.password_hash"]
