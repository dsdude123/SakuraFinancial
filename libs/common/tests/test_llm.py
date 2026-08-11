import json

import httpx
import pytest

from sakura_common.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    LLMNotConfigured,
    config_from_settings,
)


def make_client(provider, handler, **config_kwargs):
    config = LLMConfig(provider=provider, api_key="test-key", model="test-model", **config_kwargs)
    return LLMClient(config, transport=httpx.MockTransport(handler))


class TestAnthropic:
    def test_request_shape_and_parse(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})

        client = make_client("anthropic", handler)
        reply = client.complete("hi", system="be brief")

        assert reply == "hello"
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
        assert seen["headers"]["x-api-key"] == "test-key"
        assert seen["body"]["system"] == "be brief"
        assert seen["body"]["messages"][0]["content"][-1] == {"type": "text", "text": "hi"}

    def test_images_become_base64_blocks(self):
        def handler(request):
            body = json.loads(request.content)
            block = body["messages"][0]["content"][0]
            assert block["type"] == "image"
            assert block["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
            return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

        make_client("anthropic", handler).complete("what is this", images=[("image/png", "AAAA")])

    def test_non_200_raises(self):
        client = make_client("anthropic", lambda request: httpx.Response(401, text="bad key"))
        with pytest.raises(LLMError, match="401"):
            client.complete("hi")


class TestOpenAICompatible:
    def test_request_shape_and_parse(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "world"}}]})

        client = make_client("openai", handler)
        assert client.complete("hi", system="sys") == "world"
        assert seen["url"] == "https://api.openai.com/v1/chat/completions"
        assert seen["auth"] == "Bearer test-key"
        assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}

    def test_base_url_override_for_local_models(self):
        def handler(request):
            assert str(request.url) == "http://ollama:11434/v1/chat/completions"
            return httpx.Response(200, json={"choices": [{"message": {"content": "local"}}]})

        client = make_client("openai", handler, base_url="http://ollama:11434")
        assert client.complete("hi") == "local"


class TestConfig:
    def test_unknown_provider_rejected(self):
        with pytest.raises(LLMNotConfigured):
            LLMConfig(provider="bard", api_key="k", model="m").validate()

    def test_hosted_provider_requires_key(self):
        with pytest.raises(LLMNotConfigured):
            LLMConfig(provider="openai", api_key="", model="m").validate()

    def test_local_base_url_allows_empty_key(self):
        LLMConfig(provider="openai", api_key="", model="m", base_url="http://ollama:11434").validate()

    def test_config_from_settings_unconfigured(self):
        class Empty:
            def get(self, key, default=None):
                return default

        with pytest.raises(LLMNotConfigured):
            config_from_settings(Empty())

    def test_config_from_settings_reads_keys(self):
        values = {
            "llm.provider": "anthropic",
            "llm.api_key": "k",
            "llm.model": "claude-x",
        }

        class Fake:
            def get(self, key, default=None):
                return values.get(key, default)

        config = config_from_settings(Fake())
        assert config.provider == "anthropic"
        assert config.model == "claude-x"
