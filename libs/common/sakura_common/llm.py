"""Provider-agnostic LLM client.

One interface, two wire formats:

- ``anthropic``: the Anthropic Messages API.
- ``openai``: any OpenAI-compatible chat-completions endpoint — OpenAI itself,
  or a local model behind Ollama / LM Studio (set ``base_url``).

The provider, key, model, and base URL are read from the settings service at
call time (see :func:`config_from_settings`), so switching providers in the UI
takes effect immediately. Every caller must treat :class:`LLMNotConfigured` as
a normal, expected state: AI features are optional everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

ANTHROPIC_DEFAULT_URL = "https://api.anthropic.com"
OPENAI_DEFAULT_URL = "https://api.openai.com"

# Settings-service keys holding the provider configuration.
KEY_PROVIDER = "llm.provider"
KEY_API_KEY = "llm.api_key"
KEY_MODEL = "llm.model"
KEY_BASE_URL = "llm.base_url"


class LLMError(Exception):
    """The provider call failed (network, auth, bad response...)."""


class LLMNotConfigured(LLMError):
    """No usable LLM configuration exists; AI features should say so, not break."""


@dataclass
class LLMConfig:
    provider: str  # "anthropic" | "openai"
    api_key: str
    model: str
    base_url: str | None = None

    def validate(self) -> None:
        if not self.provider:
            raise LLMNotConfigured("no LLM provider configured — set one on the Settings page")
        if self.provider not in ("anthropic", "openai"):
            raise LLMNotConfigured(f"unknown LLM provider {self.provider!r}")
        if not self.model:
            raise LLMNotConfigured("no LLM model configured")
        # Local OpenAI-compatible servers (Ollama, LM Studio) accept any key,
        # but a hosted provider without a key is a misconfiguration.
        if not self.api_key and self.base_url is None:
            raise LLMNotConfigured("no LLM API key configured")


def config_from_settings(settings_client) -> LLMConfig:
    """Build an LLMConfig from the settings service.

    ``settings_client`` is a sakura_common.settings_client.SettingsClient (or
    anything with a compatible ``get``). Raises LLMNotConfigured when the
    provider or model is unset.
    """
    provider = settings_client.get(KEY_PROVIDER)
    model = settings_client.get(KEY_MODEL)
    if not provider or not model:
        raise LLMNotConfigured("LLM provider is not configured in Settings")
    config = LLMConfig(
        provider=provider,
        api_key=settings_client.get(KEY_API_KEY) or "",
        model=model,
        base_url=settings_client.get(KEY_BASE_URL) or None,
    )
    config.validate()
    return config


class LLMClient:
    """Synchronous completion client. SSR pages block on it deliberately —
    there is no client-side polling in an IE6 world."""

    def __init__(self, config: LLMConfig, timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        config.validate()
        self.config = config
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        images: list[tuple[str, str]] | None = None,
        max_tokens: int = 2048,
    ) -> str:
        """Run one completion and return the text reply.

        ``images`` is a list of (media_type, base64_data) attached to the user
        message — used for receipt photos.
        """
        if self.config.provider == "anthropic":
            return self._complete_anthropic(prompt, system, images or [], max_tokens)
        return self._complete_openai(prompt, system, images or [], max_tokens)

    def _post(self, url: str, headers: dict, payload: dict) -> dict:
        try:
            response = self._http.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        if response.status_code != 200:
            raise LLMError(f"LLM provider returned {response.status_code}: {response.text[:500]}")
        return response.json()

    def _complete_anthropic(self, prompt, system, images, max_tokens) -> str:
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
            for media, data in images
        ]
        content.append({"type": "text", "text": prompt})
        payload: dict = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system
        base = (self.config.base_url or ANTHROPIC_DEFAULT_URL).rstrip("/")
        data = self._post(
            f"{base}/v1/messages",
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            },
            payload=payload,
        )
        try:
            return "".join(block["text"] for block in data["content"] if block["type"] == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected Anthropic response shape: {data}") from exc

    def _complete_openai(self, prompt, system, images, max_tokens) -> str:
        user_content: list[dict] | str
        if images:
            user_content = [
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
                for media, data in images
            ]
            user_content.append({"type": "text", "text": prompt})
        else:
            user_content = prompt
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        base = (self.config.base_url or OPENAI_DEFAULT_URL).rstrip("/")
        data = self._post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            payload={"model": self.config.model, "max_tokens": max_tokens, "messages": messages},
        )
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected OpenAI-compatible response shape: {data}") from exc
