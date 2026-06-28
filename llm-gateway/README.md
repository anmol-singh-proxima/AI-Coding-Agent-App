# LLM Gateway

A local, single-user OpenAI-compatible gateway that routes `/v1/chat/completions`
requests across multiple free LLM providers with automatic failover and rate-limit
tracking. Point Cline (or any OpenAI-compatible client) at `http://localhost:8080/v1`
— the gateway handles provider selection, failover, and quota management transparently.

```
Cline / Roo  ──►  Gateway (localhost:8080)  ──►  OpenRouter  (priority 1)
                                             ──►  Groq        (priority 2)
                                             ──►  Gemini      (priority 3)
                                             ──►  Ollama      (priority 99, local)
```

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Add your API keys
cp .env.example .env
# Edit .env and paste your keys:
#   OPENROUTER_API_KEY=sk-or-...
#   GROQ_API_KEY=gsk_...
#   GEMINI_API_KEY=AIza...

# 4. Start the gateway
uvicorn app.main:app --port 8080 --reload
```

The gateway is now running at `http://localhost:8080`.

## Connecting Cline

1. Open Cline settings and choose **OpenAI Compatible** as the provider.
2. Set **Base URL** to `http://localhost:8080/v1`
3. Set **API Key** to any non-empty string (the gateway ignores it).
4. Set **Model** to any name from `/v1/models`, or leave it as a placeholder —
   the gateway always routes by priority regardless of the model field.

## Endpoints

| Endpoint | Description |
|---|---|
| `POST /v1/chat/completions` | Main chat endpoint, OpenAI-compatible |
| `GET /v1/models` | Lists configured sources for Cline's model dropdown |
| `GET /status` | Per-source counters, cooldowns, and availability |
| `GET /docs` | Interactive Swagger UI |

### `/status` example

```json
{
  "sources": [
    {
      "name": "openrouter-qwen3coder",
      "model": "qwen/qwen3-coder:free",
      "priority": 1,
      "enabled": true,
      "rpm": 20,
      "rpd": 50,
      "available": true,
      "minute_count": 3,
      "day_count": 12,
      "in_cooldown": false,
      "cooldown_remaining_seconds": 0.0
    }
  ]
}
```

## Failover behaviour

The gateway tries sources in priority order and skips a source when:

- **RPM/RPD cap reached** — local counters show the source is over its configured limit.
- **HTTP 429 received** — upstream is rate-limiting; source goes into cooldown for
  the duration specified in the `Retry-After` header (default: 60 s).
- **HTTP 5xx / timeout / connection error** — upstream is unavailable; try the next.
- **HTTP 401 / 403** — API key issue specific to this source; try the next.

The gateway does **not** fail over on HTTP 400 / 422 — these indicate a malformed
request that would fail on every source.

Once a streaming response has started (HTTP 200 received), failover is no longer
possible. If the upstream dies mid-stream, the client receives a truncated response.

## Adding a source

Add an entry to `config.yaml`:

```yaml
sources:
  - name: my-new-source
    base_url: https://api.example.com/v1
    model: some-model-id
    api_key_env: MY_NEW_SOURCE_API_KEY   # name of the env var holding the key
    rpm: 60          # requests per minute (null = unlimited)
    rpd: 1000        # requests per day   (null = unlimited)
    priority: 5      # lower = tried earlier
    enabled: true
```

Add the key to `.env`:

```
MY_NEW_SOURCE_API_KEY=sk-...
```

Restart the gateway — the new source is active immediately.

## Configuration reference

| Field | Type | Description |
|---|---|---|
| `name` | string | Unique label used in logs and `/status` |
| `base_url` | string | OpenAI-compatible base URL |
| `model` | string | Upstream model ID |
| `api_key_env` | string | Env var name holding the API key (empty for keyless sources like Ollama) |
| `rpm` | int \| null | Requests-per-minute cap |
| `rpd` | int \| null | Requests-per-day cap |
| `priority` | int | Lower value = tried first |
| `enabled` | bool | Toggle without deleting the entry |

## Local Ollama tuning (memory & keep-alive)

The local fallback runs through Ollama. On a memory-constrained machine (e.g. a
24GB Mac), a large model plus its KV cache can consume all RAM and freeze the UI,
and by default Ollama keeps a model resident for 5 minutes after each request.
Both are controlled by environment variables on the Ollama **server**.

All tunable values live in one editable file, [`ollama.env`](ollama.env):

| Variable | Purpose |
|---|---|
| `OLLAMA_KEEP_ALIVE` | How long a model stays in RAM after its last request (`0`, `30s`, `1m`, `5m`…). Shorter frees memory sooner. |
| `OLLAMA_CONTEXT_LENGTH` | Default context window. The biggest lever on RAM — the KV cache grows linearly with it. |
| `OLLAMA_FLASH_ATTENTION` | `1` = more memory-efficient KV cache. Required for KV-cache quantization. |
| `OLLAMA_KV_CACHE_TYPE` | `q8_0` ≈ half the KV-cache memory at negligible quality cost (needs flash attention). |
| `OLLAMA_MAX_LOADED_MODELS` | Cap on models resident at once. `1` prevents two big models stacking. |

To change a setting, edit `ollama.env` then re-apply it:

```bash
./apply-ollama-env.sh
```

The script registers the values with `launchctl setenv` and **fully restarts**
Ollama.app (both the GUI and the `ollama serve` subprocess) so they take effect.
Verify with `ollama ps` after a request — the `CONTEXT` and `UNTIL` columns
should match your settings.

> **Reboot note:** `launchctl setenv` values persist until you log out or reboot.
> After a reboot, re-run `./apply-ollama-env.sh` (or add it to your Login Items)
> to re-apply them.

**Model choice matters too.** `qwen2.5-coder:14b` (~9GB, coding-specialised) is a
much better fit for a 24GB machine than a 24B model like `devstral` (~14GB), which
is what causes the memory pressure and freezing. The gateway's local source is set
to `qwen2.5-coder:14b` in [`config.yaml`](config.yaml).

## Running tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Security notes

- API keys live only in `.env` which is gitignored — never commit it.
- The gateway binds to localhost only. Do not expose it to the network; it has
  no authentication by design.
- This is a personal, single-user tool. For multi-user access, auth, or remote
  use, consider [LiteLLM](https://github.com/BerriAI/litellm) instead.
