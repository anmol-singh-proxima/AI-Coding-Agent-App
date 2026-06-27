# Local LLM Gateway — Implementation Spec

> A spec for building a personal, OpenAI-compatible gateway that routes requests
> across multiple free LLM sources and automatically fails over when one hits its
> rate limit. Designed to be implemented with Claude Code.

---

## 1. What we're building (and why)

A single local HTTP service — the **gateway** — that exposes an OpenAI-compatible
`/v1/chat/completions` endpoint. Tools like Cline point at this one endpoint. The
gateway holds an ordered list of upstream **sources** (OpenRouter, Groq, Gemini,
Ollama, etc.). For each request it tries sources in priority order; when a source
is rate-limited or failing, it transparently moves to the next one. The caller
(Cline) only ever sees one stable endpoint and never has to switch profiles.

**Why a gateway instead of switching Cline profiles by hand:**
- Failover is automatic — no manual intervention when a quota runs out.
- One config file holds all sources; adding a new provider is one entry.
- The same gateway can serve any tool (Cline, Roo, a future custom extension).
- It teaches the exact streaming + routing mechanics real agent tools use.

**Non-goals:** This is a personal, single-user, localhost tool. No auth, no
multi-tenancy, no production hardening, no UI. Keep it small.

---

## 2. Core design

```
                       ┌────────────────────────────────────────┐
   Cline / Roo  ─────► │  Gateway  (localhost:8080/v1)           │
   (OpenAI client)     │                                         │
                       │   1. pick first healthy source          │
                       │   2. forward request (stream or not)    │
                       │   3. on 429/5xx/quota → next source      │
                       │   4. track per-source RPM & RPD counters │
                       └───────┬──────────┬──────────┬───────────┘
                               ▼          ▼          ▼
                          OpenRouter    Groq      Ollama (local,
                          (priority 1) (prio 2)   unlimited, last)
```

Every upstream is itself OpenAI-compatible, so forwarding is mostly a base-URL +
API-key swap. The gateway's real work is **source selection**, **failover**, and
**rate-limit accounting**.

---

## 3. Tech stack

- **Language:** Python 3.11+
- **Web framework:** FastAPI
- **Server:** uvicorn
- **HTTP client:** httpx (async; supports streaming responses)
- **Config:** a `config.yaml` file + a `.env` file for secrets
- **Deps:** `fastapi`, `uvicorn`, `httpx`, `pyyaml`, `python-dotenv`

Rationale: FastAPI + httpx handle async streaming (Server-Sent Events) cleanly,
which is required because Cline streams responses token-by-token.

---

## 4. File structure

```
llm-gateway/
├── config.yaml            # ordered list of sources + their limits
├── .env                   # API keys (never commit)
├── .env.example           # template showing required keys
├── .gitignore             # must ignore .env
├── requirements.txt
├── README.md
└── app/
    ├── main.py            # FastAPI app, endpoints, request handling
    ├── config.py          # load + validate config.yaml and env vars
    ├── sources.py         # Source model + the SourceRegistry
    ├── limiter.py         # per-source rate-limit tracking
    ├── router.py          # failover selection logic
    └── proxy.py           # forwarding (streaming + non-streaming) to upstreams
```

---

## 5. Configuration

### 5.1 `config.yaml` schema

A `sources` list, in priority order (first = tried first). Each source:

| field        | type   | meaning                                                        |
|--------------|--------|----------------------------------------------------------------|
| `name`       | string | unique label, e.g. `openrouter-qwen`                           |
| `base_url`   | string | OpenAI-compatible base, e.g. `https://openrouter.ai/api/v1`    |
| `model`      | string | the upstream model id, e.g. `qwen/qwen3-coder:free`            |
| `api_key_env`| string | name of the env var holding the key (empty for local Ollama)   |
| `rpm`        | int    | requests-per-minute cap (null = unlimited)                     |
| `rpd`        | int    | requests-per-day cap (null = unlimited)                        |
| `priority`   | int    | lower = tried earlier                                          |
| `enabled`    | bool   | toggle without deleting                                        |

### 5.2 Example `config.yaml`

```yaml
sources:
  - name: openrouter-qwen3coder
    base_url: https://openrouter.ai/api/v1
    model: qwen/qwen3-coder:free
    api_key_env: OPENROUTER_API_KEY
    rpm: 20
    rpd: 50          # raise to 1000 if you ever buy $10 of credits
    priority: 1
    enabled: true

  - name: groq-llama
    base_url: https://api.groq.com/openai/v1
    model: llama-3.3-70b-versatile
    api_key_env: GROQ_API_KEY
    rpm: 30
    rpd: 1000
    priority: 2
    enabled: true

  - name: gemini-flash
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    model: gemini-2.5-flash
    api_key_env: GEMINI_API_KEY
    rpm: 10
    rpd: 1500
    priority: 3
    enabled: true

  - name: ollama-devstral        # local final fallback — never rate-limited
    base_url: http://localhost:11434/v1
    model: devstral
    api_key_env: ""
    rpm: null
    rpd: null
    priority: 99
    enabled: true
```

> **Verify each base_url and model id against the provider's current docs before
> relying on it** — provider endpoints, free model ids, and free-tier limits
> change often. The Ollama entry is the always-available safety net: it has no
> limits, so put it last so the gateway only reaches it when every API source is
> exhausted or offline.

### 5.3 `.env.example`

```
OPENROUTER_API_KEY=
GROQ_API_KEY=
GEMINI_API_KEY=
```

---

## 6. Components — what each module does

### 6.1 `config.py`
- Load `.env` (via `python-dotenv`).
- Parse `config.yaml`.
- For each source, resolve `api_key_env` → actual key from env. If the env var is
  named but missing/empty, mark the source `enabled=false` and log a warning
  (don't crash — a missing Gemini key shouldn't kill the gateway).
- Return a validated, priority-sorted list of `Source` objects.

### 6.2 `sources.py`
- A `Source` dataclass holding the config fields above plus runtime state.
- A `SourceRegistry` holding all sources, sorted by `priority`, with a method
  `available_sources()` that returns enabled sources that are **not currently
  rate-limited** (delegates the limit check to `limiter.py`).

### 6.3 `limiter.py` — rate-limit accounting
Per source, track:
- a **minute window**: count of requests in the current rolling 60s.
- a **day window**: count of requests since the last local-midnight reset.

Expose:
- `can_use(source) -> bool`: false if minute count ≥ `rpm` or day count ≥ `rpd`.
- `record(source)`: increment both counters (call on each attempt).
- `mark_rate_limited(source, retry_after)`: when an upstream returns HTTP 429,
  force this source unavailable until `retry_after` seconds pass (or a default
  cooldown, e.g. 60s, if no header is given). This handles the case where our
  local counters disagree with the provider's actual state.

Implementation note: an in-memory dict keyed by source name is fine — this is a
single-process local tool. No database needed. Use `time.monotonic()` for the
minute window and a stored date for the day window.

### 6.4 `router.py` — failover selection
- `select_chain()`: return the ordered list of sources to attempt for a request:
  enabled, currently under their limits, sorted by priority.
- The actual attempt loop lives in `main.py`/`proxy.py`, but the *ordering policy*
  lives here so it's easy to change later (e.g. add round-robin among equal
  priorities, or model-class matching).

### 6.5 `proxy.py` — forwarding
Two functions:
- `forward_nonstream(source, payload) -> Response`: POST the (rewritten) payload
  to `{source.base_url}/chat/completions` with the source's key, return the JSON.
- `forward_stream(source, payload) -> async generator`: same, but stream the SSE
  chunks straight back to the caller as they arrive.

**Payload rewriting:** the incoming request will name *some* model (whatever Cline
sends). Overwrite `payload["model"]` with `source.model` before forwarding, since
each source has its own model id. Pass everything else (`messages`, `temperature`,
`stream`, `tools`, etc.) through unchanged.

**Headers:** set `Authorization: Bearer {key}` when the source has a key. For
OpenRouter, optionally add `HTTP-Referer` and `X-Title` headers (it likes them but
they're not required).

### 6.6 `main.py` — the endpoint + attempt loop
- `POST /v1/chat/completions`: the OpenAI-compatible endpoint.
  1. Read the JSON body. Detect `stream: true/false`.
  2. `chain = router.select_chain()`. If empty → return 503 "all sources exhausted".
  3. For each `source` in `chain`:
     - `limiter.record(source)`
     - try to forward (stream or non-stream).
     - **Success** → return / stream the response, stop.
     - **HTTP 429** → `limiter.mark_rate_limited(source, retry_after)`, continue to next.
     - **HTTP 5xx / connection error / timeout** → log, continue to next.
     - **HTTP 4xx other than 429** (e.g. bad request) → this is *our* bug, not a
       quota issue: return the error to the caller, don't fail over (failing over
       would just repeat the same bad request).
  4. If the loop ends with no success → return 503 with a summary of what failed.
- `GET /v1/models`: return the list of configured source names as model entries
  (Cline calls this to populate its dropdown). Optional but nice.
- `GET /status`: a small JSON dump of each source's current minute/day counts and
  whether it's currently available. Useful for debugging which source served you.

**Streaming caveat:** once you start streaming bytes back to the client, you can no
longer fail over (the HTTP response has already begun). So for streaming requests,
do failover *before the first byte*: attempt the upstream connection, and only
start relaying once you've confirmed a 200 status and the first chunk arrives. If
the upstream 429s on connect, fail over normally; if it dies mid-stream, you can
only surface the error, not silently switch. Document this clearly.

---

## 7. The failover algorithm (pseudocode)

```
def handle_chat(request):
    payload = request.json()
    streaming = payload.get("stream", False)
    chain = router.select_chain()          # enabled + under-limit, by priority

    if not chain:
        return 503, "all sources rate-limited or disabled"

    errors = []
    for source in chain:
        limiter.record(source)
        try:
            if streaming:
                resp = open_upstream_stream(source, payload)   # may raise on 429
                return relay_stream(resp)                       # first byte committed
            else:
                resp = forward_nonstream(source, payload)
                return resp
        except RateLimited as e:
            limiter.mark_rate_limited(source, e.retry_after)
            errors.append((source.name, "429"))
            continue
        except (ServerError, ConnectionError, Timeout) as e:
            errors.append((source.name, str(e)))
            continue
        except BadRequest as e:
            return 400, e.body          # don't fail over on our own bad request

    return 503, {"message": "no source succeeded", "attempts": errors}
```

---

## 8. Connecting Cline to the gateway

After the gateway runs on `http://localhost:8080`:
1. In Cline settings, choose **OpenAI Compatible** as the provider.
2. Base URL: `http://localhost:8080/v1`
3. API key: any non-empty string (the gateway ignores it locally).
4. Model: any of the names returned by `/v1/models`, or just a placeholder — the
   gateway picks the real source itself.

Now every Cline request flows through the gateway and fails over automatically.

---

## 9. Build sequence (milestones for Claude Code)

Implement and test in this order — each milestone is independently runnable.

**M1 — Skeleton.** FastAPI app with a hardcoded single upstream (OpenRouter).
`POST /v1/chat/completions` forwards non-streaming requests and returns the JSON.
Test with `curl`. *Goal: prove forwarding works end to end.*

**M2 — Streaming.** Add SSE streaming passthrough for `stream: true`. Test that a
streamed `curl` request relays chunks. *Goal: Cline-compatible streaming.*

**M3 — Config-driven sources.** Move the upstream into `config.yaml` + `.env`,
load via `config.py`, support multiple sources sorted by priority. Still always
use source #1. *Goal: no hardcoded providers.*

**M4 — Failover.** Add the attempt loop: on 429/5xx, move to the next source.
Test by setting an invalid key on source #1 and confirming it falls to source #2,
and that local Ollama serves when all API sources are down. *Goal: automatic
failover works.*

**M5 — Rate-limit accounting.** Add `limiter.py` with RPM/RPD counters and the
429-cooldown. Add `/status`. Test by setting `rpd: 2` on a source and confirming
the 3rd request skips it. *Goal: pre-emptive limit handling.*

**M6 — Polish.** Add `/v1/models`, logging that prints which source served each
request, a clean README, and `.gitignore` for `.env`. *Goal: usable daily.*

---

## 10. Testing checklist

- [ ] Non-streaming `curl` to `/v1/chat/completions` returns a completion.
- [ ] Streaming `curl` (`-N`, `"stream": true`) relays chunks live.
- [ ] Invalid key on the top source → request still succeeds via the next source.
- [ ] All API sources disabled → local Ollama serves the request.
- [ ] Hitting a source's configured `rpd` → it's skipped until the day resets.
- [ ] A genuine bad request (e.g. malformed messages) returns 400, not a failover loop.
- [ ] Cline, pointed at `localhost:8080/v1`, completes a real file-editing task.
- [ ] `/status` shows accurate per-source counters after a few requests.

---

## 11. Run instructions (for the README)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then paste your keys into .env
uvicorn app.main:app --port 8080 --reload
```

Then point Cline at `http://localhost:8080/v1`.

---

## 12. Security notes

- API keys live only in `.env`; `.gitignore` must exclude it. Never hardcode keys.
- The gateway binds to localhost only — do **not** expose it to the network; it has
  no authentication by design.
- This is a personal tool. If you ever want auth, rate limits per caller, or remote
  access, that's a different (much larger) project — consider LiteLLM instead.

---

## 13. If you'd rather not build it: LiteLLM

Everything above is implemented and battle-tested in **LiteLLM Proxy**. A
`litellm config.yaml` with a `fallbacks` list does the same failover, and you run
`litellm --config config.yaml` to get a local OpenAI-compatible endpoint. Build
your own to learn the mechanics; reach for LiteLLM if you just want the result.
```
