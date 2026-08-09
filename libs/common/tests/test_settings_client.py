import httpx
import pytest

from sakura_common.settings_client import SettingsClient, SettingsWriteError


def make_client(handler):
    return SettingsClient(base_url="http://settings:8005", transport=httpx.MockTransport(handler))


def test_get_returns_revealed_value():
    def handler(request):
        assert request.url.path == "/api/settings/llm.api_key"
        assert request.url.params["reveal"] == "true"
        return httpx.Response(200, json={"key": "llm.api_key", "value": "sk-123", "is_secret": True})

    assert make_client(handler).get("llm.api_key") == "sk-123"


def test_get_missing_key_returns_default():
    client = make_client(lambda request: httpx.Response(404, json={"detail": "not found"}))
    assert client.get("nope", default="fallback") == "fallback"


def test_get_unreachable_returns_default():
    def handler(request):
        raise httpx.ConnectError("down")

    assert make_client(handler).get("any", default=None) is None


def test_set_sends_put():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"ok": True})

    make_client(handler).set("a.b", "v", is_secret=True)
    assert seen == {"method": "PUT", "path": "/api/settings/a.b"}


def test_set_failure_raises():
    client = make_client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(SettingsWriteError, match="500"):
        client.set("a", "b")
