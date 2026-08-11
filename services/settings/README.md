# settings-service (:8005)

The platform's key-value configuration store. Anything the user can change at
runtime lives here and is edited through the web UI's Settings pages — no
config files, no restarts. Other services read values per-call via
`sakura_common.settings_client.SettingsClient`.

## API

| Endpoint | Purpose |
| -------- | ------- |
| `GET /api/settings` | List all settings (secret values masked as `********`) |
| `GET /api/settings/{key}?reveal=true` | Read one value; `reveal` returns the real value (internal network only) |
| `PUT /api/settings/{key}` | Upsert `{value, is_secret}`. Submitting the mask for an existing secret keeps the stored value |
| `DELETE /api/settings/{key}` | Remove a key |
| `POST /api/llm/test` | Round-trip test of an LLM config; body fields override stored values without saving |
| `GET /api/export` / `POST /api/import` | Backup / restore (export contains real secret values — treat backups as sensitive) |
| `GET /health` | Liveness |

Live OpenAPI docs: `/docs` on the service port.

## Well-known keys

| Key | Used by | Meaning |
| --- | ------- | ------- |
| `llm.provider` | receipts, stocks | `anthropic` or `openai` (OpenAI-compatible, incl. Ollama/LM Studio) |
| `llm.api_key` | receipts, stocks | Provider API key (secret). May be empty for local endpoints |
| `llm.model` | receipts, stocks | Model name to request |
| `llm.base_url` | receipts, stocks | Optional endpoint override (e.g. `http://ollama:11434`) |
| `stocks.ai_instructions` | stocks | The user's trading philosophy, included in AI analysis prompts |
| `stocks.fetch_hour_utc` | stocks | Hour (UTC) of the daily price fetch, default `10` |
| `ui.password_hash` | web-ui | PBKDF2 hash of the single-user login password |
| `monthly.skip.<yyyy-mm>.<service>.<account_id>` | web-ui | Marks an account skipped on the Monthly Updates page |

## Notes

- The mask sentinel is `********`; a stored non-secret value that is literally
  eight asterisks is left alone (only secrets get the keep-existing behavior).
- This service must stay dependency-light and boring: everything else depends
  on it.
