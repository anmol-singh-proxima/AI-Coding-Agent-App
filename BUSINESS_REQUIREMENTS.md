# Business Requirements Document — AI Coding Agent App (Local LLM Gateway)

| | |
|---|---|
| **Document version** | 1.0 |
| **Date** | 2026-08-01 |
| **Status** | Baselined against the delivered implementation (Milestones M1–M6 complete) |
| **Author** | Reverse-engineered from the codebase |

---

## 1. Purpose of this document

This document states, in business terms, *what* the project must achieve and *why*.
It deliberately avoids implementation detail — the technical design lives in
[llm-gateway-spec.md](llm-gateway-spec.md) and the operating instructions in
[llm-gateway/README.md](llm-gateway/README.md).

---

## 2. Background and problem statement

AI coding assistants (Cline, Roo Code, and similar IDE extensions) need a large
language model behind them. Paid model subscriptions are expensive for individual
developers, and the free tiers offered by providers such as OpenRouter, Groq and
Google Gemini are individually too small to sustain a working day: each one caps
requests per minute and per day, and once a cap is hit the assistant simply stops
working.

Today the developer works around this **manually** — noticing the failure,
opening the assistant's settings, switching to a different provider profile,
re-entering a model name, and resuming. This is disruptive, happens several times
a day, and interrupts the flow of work at exactly the wrong moment.

**The business problem:** a solo developer cannot get uninterrupted AI coding
assistance from free model providers without constant manual intervention.

---

## 3. Business objectives

| # | Objective | Why it matters |
|---|---|---|
| BO-1 | Eliminate manual provider switching during a coding session | Removes the single biggest source of interruption |
| BO-2 | Keep AI assistance available even when every hosted provider is exhausted or offline | Work must not stop; a local model is the safety net |
| BO-3 | Keep the running cost at (or near) zero | The whole premise is free-tier and local capacity |
| BO-4 | Make adding, removing or reordering providers a configuration change, not a code change | New free tiers appear and disappear frequently |
| BO-5 | Make the system usable with existing tools without modifying them | The developer keeps using Cline/Roo as-is |
| BO-6 | Serve as a learning vehicle for how real agent tooling routes and streams model traffic | A stated secondary goal of the project |

---

## 4. Scope

### 4.1 In scope

* A single locally-run service (the **gateway**) that sits between the coding
  assistant and multiple model providers.
* Automatic selection of, and failover between, providers.
* Quota awareness, so a provider is skipped *before* it rejects the request.
* Visibility into which provider is serving requests and how much quota remains.
* Configuration of providers and their limits through an editable file.
* Tuning of the local model's memory footprint so it can run on a
  memory-constrained laptop without degrading the machine.
* A reusable **system prompt** that sets the quality, correctness and security
  standards the coding assistant must follow
  ([coding-agent-system-prompt.md](coding-agent-system-prompt.md)).

### 4.2 Out of scope

* Multi-user access, user accounts, authentication or authorisation.
* Remote or networked access — the service is localhost-only by design.
* A graphical user interface or dashboard.
* Billing, cost tracking, or paid-tier management.
* Production-grade concerns: high availability, horizontal scaling, persistence,
  audit logging.
* Building the coding assistant itself; the project integrates with existing ones.

---

## 5. Stakeholders and users

| Stakeholder | Interest |
|---|---|
| **Primary user** — the individual developer | Uninterrupted AI coding assistance at zero cost |
| **Consuming tools** — Cline, Roo Code, any OpenAI-compatible client | A single, stable endpoint that behaves like a standard model API |
| **Upstream providers** — OpenRouter, Groq, Google Gemini, local Ollama | Their published rate limits must be respected, not circumvented |

---

## 6. Business requirements

### 6.1 Continuity of service

| ID | Requirement | Priority |
|---|---|---|
| BR-1 | The system shall present **one stable endpoint** to the coding assistant, so the user never reconfigures their tool when the underlying provider changes. | Must |
| BR-2 | The system shall try providers in a **user-defined priority order**, so the cheapest/fastest/preferred provider is always used first. | Must |
| BR-3 | When a provider refuses or fails a request, the system shall **automatically move to the next provider** without user involvement and without failing the user's request. | Must |
| BR-4 | The system shall distinguish a **provider-side failure** (out of quota, unavailable, bad credentials) from a **request-side failure** (a malformed request). Only the former triggers a move to the next provider; the latter is reported back immediately rather than repeated across every provider. | Must |
| BR-5 | The system shall always include a **local, unlimited model** as the last resort, so assistance remains available when every hosted provider is exhausted or the machine is offline. | Must |
| BR-6 | When no provider can serve a request, the system shall return a **clear, actionable failure** naming what was attempted. | Must |

### 6.2 Quota management

| ID | Requirement | Priority |
|---|---|---|
| BR-7 | The system shall track, per provider, how much of its **per-minute and per-day allowance** has been consumed. | Must |
| BR-8 | The system shall **skip a provider before contacting it** once its configured allowance is used up, avoiding wasted round-trips and provider-side rejections. | Must |
| BR-9 | When a provider explicitly signals it is rate-limited, the system shall **rest that provider for the period the provider asks for** (or a sensible default) before trying it again. | Must |
| BR-10 | Per-minute allowances shall recover continuously; per-day allowances shall reset at the start of each local day. | Must |

### 6.3 Configurability

| ID | Requirement | Priority |
|---|---|---|
| BR-11 | Providers — their endpoint, model, order, and limits — shall be **defined in a single editable configuration file**; adding a provider requires no code change. | Must |
| BR-12 | A provider shall be **switchable on and off** without deleting its configuration. | Should |
| BR-13 | If a provider's credentials are missing, the system shall **disable only that provider and continue running**, rather than failing to start. | Must |

### 6.4 Compatibility

| ID | Requirement | Priority |
|---|---|---|
| BR-14 | The system shall be usable by any tool that already speaks the **industry-standard OpenAI chat-completions interface**, with configuration only (endpoint URL + placeholder key). | Must |
| BR-15 | The system shall support **live, token-by-token streaming** of responses, because coding assistants display output as it is generated. | Must |
| BR-16 | The system shall pass the assistant's request options through unchanged (including tool/function definitions), so no assistant capability is lost by routing through the gateway. | Must |
| BR-17 | The system shall publish the **list of configured providers** in the format assistants expect for their model-selection dropdown. | Should |

### 6.5 Transparency and operability

| ID | Requirement | Priority |
|---|---|---|
| BR-18 | The system shall expose a **status view** showing, per provider: whether it is currently usable, its consumed minute/day counts, and any active cooldown. | Should |
| BR-19 | The system shall log **which provider served each request** and why any provider was skipped, so behaviour is explainable after the fact. | Should |
| BR-20 | The system shall be startable and connectable by the user in **under five minutes** from a documented set of steps. | Should |

### 6.6 Local model resource management

| ID | Requirement | Priority |
|---|---|---|
| BR-21 | Running the local fallback model shall **not exhaust the machine's memory** or make the developer's computer unusable. | Must |
| BR-22 | Memory-related settings (how long a model stays loaded, context size, cache efficiency, how many models may be resident) shall be **collected in one place and applied by a single command**. | Should |
| BR-23 | The chosen local model shall be sized appropriately for the target machine (a 24 GB laptop), favouring a smaller coding-specialised model over a larger general one. | Should |

### 6.7 Security and privacy

| ID | Requirement | Priority |
|---|---|---|
| BR-24 | Provider credentials shall be stored **outside source control** and never appear in code, logs, or committed files. | Must |
| BR-25 | The service shall be reachable **only from the local machine**; it carries no authentication and must never be exposed to a network. | Must |
| BR-26 | Error messages returned to the caller shall not leak credentials or internal secrets. | Must |

### 6.8 Quality of AI-generated code (system prompt)

| ID | Requirement | Priority |
|---|---|---|
| BR-27 | The project shall provide a **reusable instruction set** for the coding assistant that mandates: understanding existing code before changing it, small scoped changes, clear and maintainable code, deliberate error handling, and secure-by-default practices (no hardcoded secrets, no injection risks, input validation, least privilege). | Must |
| BR-28 | That instruction set shall require the assistant to **surface assumptions, risks and uncertainty** rather than guessing — and to state when a requested approach has a real problem. | Must |

---

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Simplicity** — the system is a single-user personal tool. It must stay small; no database, no clustering, no speculative abstraction. |
| NFR-2 | **Latency overhead** — routing must add negligible delay relative to the model's own response time. |
| NFR-3 | **Streaming responsiveness** — output must reach the assistant as it is produced, with no buffering that delays visible text. |
| NFR-4 | **Tolerance of slow models** — a large local model may pause for seconds between tokens; this must not be mistaken for a failure. |
| NFR-5 | **Resilience at startup** — a missing key, a missing config file, or an unreachable provider must degrade the system, not stop it. |
| NFR-6 | **Testability** — core behaviours (routing order, failover decisions, quota accounting, streaming) must be covered by automated tests. The delivered system has ~120 tests across these areas. |
| NFR-7 | **Maintainability** — configuration-driven design so provider churn is absorbed without code edits. |

---

## 8. Key business rules

1. **Priority order is absolute** — the highest-priority *usable* provider always wins; there is no load balancing.
2. **Local before failure** — the local model is tried before the user is ever told "no provider available".
3. **The caller's requested model is advisory** — the gateway selects the real model by priority, so the assistant never needs to know which provider is live.
4. **A malformed request is never retried** across providers — it would fail identically everywhere and would consume quota needlessly.
5. **Committed streaming cannot be undone** — once a response has begun reaching the user, the system can no longer switch providers; an interruption at that point is surfaced honestly rather than hidden.
6. **Provider limits are respected, not evaded** — quota tracking exists to stay within published free-tier terms.

---

## 9. Assumptions

* A single user on a single machine; no concurrent load.
* Every provider exposes an OpenAI-compatible interface, so they are interchangeable by configuration.
* Free-tier limits, model identifiers and endpoints change frequently and must be verified by the user against provider documentation.
* The local model runtime (Ollama) is installed and running on the same machine.
* The target machine has approximately 24 GB of memory.

---

## 10. Constraints

* Runs on macOS on the developer's own laptop; localhost only.
* Free tiers only — no committed spend.
* State is held in memory only; restarting the service resets quota counters.
* No user interface beyond command-line startup and simple status/documentation endpoints.

---

## 11. Success criteria

The project is considered successful when:

1. A full coding session in Cline completes **without the user touching provider settings**, even after one or more free tiers are exhausted.
2. Turning off or breaking the top-priority provider produces **no visible failure** — the next provider serves the request.
3. With all hosted providers unavailable, the **local model completes a real file-editing task**.
4. A provider that has reached its configured daily allowance is **skipped without contacting it**.
5. A genuinely malformed request produces **one clear error**, not a cascade of retries.
6. The status view accurately reflects consumption after a series of requests.
7. Running the local model leaves the machine **responsive and usable**.
8. Adding a new provider takes **one configuration entry plus one credential**, and no code change.

---

## 12. Delivery milestones (as executed)

| Milestone | Business outcome delivered |
|---|---|
| M1 | Proof that a request can be routed through the gateway to a provider and answered |
| M2 | Live streaming output, making the gateway usable by real coding assistants |
| M3 | Providers moved into configuration — no hardcoded vendors |
| M4 | Automatic failover between providers |
| M5 | Quota tracking, cooldowns, and the status view |
| M6 | Provider listing, request-level logging, documentation — daily usability |
| Post-M6 | Local-model memory tuning so the fallback runs without degrading the machine |

---

## 13. Known limitations (accepted)

* Quota counters are lost on restart, which can allow a brief over-count against a provider's daily allowance.
* A failure that occurs *after* streaming has started cannot be recovered by switching providers; the user sees a truncated response.
* The gateway has no authentication and is unsafe to expose beyond localhost.
* Provider endpoints, free model identifiers and limits must be re-verified by the user over time.

---

## 14. Alternatives considered

An off-the-shelf proxy (**LiteLLM**) already provides equivalent routing and
fallback behaviour. It was consciously not adopted because BO-6 — learning the
mechanics of model routing and streaming first-hand — is an explicit goal of this
project. LiteLLM remains the recommended path for anyone who wants only the
outcome.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **Gateway** | The local service this project delivers; the single endpoint the coding assistant talks to |
| **Source / provider** | An upstream model service (OpenRouter, Groq, Gemini, local Ollama) |
| **Failover** | Automatically moving to the next provider when the current one cannot serve a request |
| **Streaming** | Delivering the model's answer progressively as it is generated |
| **RPM / RPD** | Requests per minute / requests per day — the allowance a provider grants |
| **Cooldown** | A rest period during which a provider is not contacted after it signalled a rate limit |
| **Coding assistant** | Cline, Roo Code, or a similar IDE extension that consumes the gateway |
