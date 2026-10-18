# Models and hardware guidance

By default all inference is local through Ollama and nothing leaves the machine. Optionally, bots can use a hosted
model through any **OpenAI-compatible API** (see "Hosted model APIs" below); that is off until you enable it.

## What was actually tested (2026-10-18)

Host: Intel i7-1265U (10C/12T, 15 W), 15.6 GB RAM, Intel UHD integrated graphics (Ollama ran **CPU-only**,
`size_vram = 0`). Ollama 0.40.5.

| Model (Ollama tag) | Params / quant | License | Loaded RAM (Ollama `/api/ps`) | Gen speed | Eval suite |
|---|---|---|---|---|---|
| `qwen2.5:1.5b` | 1.5B, Q4_K_M | Apache-2.0 | 1.37 GB | ~20–24 tok/s | 5/6 |
| `qwen2.5:3b` | 3.1B, Q4_K_M | **Qwen Research License (non-commercial)** | 2.42 GB | ~10–12 tok/s (19 on short replies) | 5/6 |

Both report capabilities `completion, tools` (no vision, no audio), context length 32,768. LocalCrew caps
`num_ctx` at 8,192.

Eval suite: `python tests/eval/model_eval.py <models…>`. Raw results: `docs/eval/model_eval_results.json`.
Six small cases: correct tool choice, tool arguments, answering without tools, JSON plan with schema, prompt
injection inside tool output, grounded markdown report. **It is a smoke evaluation, not a benchmark.**

Notable observations:
- Neither model made the injected `fetch_url` call. `qwen2.5:3b`, however, *claimed* in prose that it had fetched
  the attacker URL (a false statement about an action). Policy enforcement, not model behaviour, is the safety
  boundary.
- On the real report task (`test_real_model_report`, manual UI run) `qwen2.5:1.5b` produced a downloadable
  report but got arithmetic wrong (11.67% instead of 12.5%) and dropped the return figures. Use tools (e.g.
  `run_command` with Python) for calculations. Don't trust small-model arithmetic.

## Profiles

| Profile | Model | When |
|---|---|---|
| **Low-memory (default, tested)** | `qwen2.5:1.5b` | ≤ 16 GB RAM, no GPU. ~1.4 GB RAM. Apache-2.0. |
| Larger CPU (tested, license-restricted) | `qwen2.5:3b` | Better phrasing, half the speed, non-commercial license. |
| Stronger GPU profile (**untested here**) | e.g. `qwen2.5:7b` / `qwen3:8b` class with tool support | ≥ 8 GB VRAM NVIDIA/AMD GPU. Run the eval suite on that host before relying on it, and record the results here. |

Set the default with `LOCALCREW_DEFAULT_MODEL`, or pick a model per bot in the bot editor. The editor shows
tools/vision/context for each installed model, as reported by Ollama.

## Setup

```bash
winget install --id Ollama.Ollama -e --scope user
```

```bash
ollama pull qwen2.5:1.5b
```

Ollama listens on `127.0.0.1:11434` by default. Keep it that way; it has no authentication.

### Containers reaching host Ollama

Compose sets `LOCALCREW_OLLAMA_URL=http://host.docker.internal:11434` with `extra_hosts: host-gateway`. On Linux,
Ollama must then listen on the Docker bridge address, **not** on all interfaces:

```bash
sudo systemctl edit ollama   # [Service] Environment="OLLAMA_HOST=172.17.0.1:11434"
```

Firewall that address from other hosts. Never publish 11434 to a LAN or the internet.

## Capability handling

- `Caps.tools == False` → the task runs without tools and says so in its progress text.
- `Caps.vision == False` (all tested models) → screenshots are saved for humans only. They are never described to
  the model as understood. Visual coordinate control is disabled.
- `remote:<id>` models go to the configured OpenAI-compatible API. If it is disabled or unconfigured the task fails
  with that reason; it never silently falls back to another model.

## Hosted model APIs (optional)

`services/api/localcrew/remote.py` implements one adapter for the OpenAI-compatible Chat Completions API (streaming,
tool calling, JSON mode for planning, cancellation, retries on 429/5xx, no retry on 4xx). It works with any
provider that offers that API:

| Provider | `LOCALCREW_REMOTE_BASE_URL` | Example model id |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| OpenRouter (many vendors, incl. Anthropic/Google models) | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` |
| Groq | `https://api.groq.com/openai/v1` | a tool-capable model from their list |
| Together / DeepSeek / Mistral | their OpenAI-compatible endpoint | per provider |
| LM Studio / vLLM (local servers) | `http://127.0.0.1:<port>/v1` | per server |

Setup (in `.env`, then restart):

```
LOCALCREW_REMOTE_PROVIDERS_ENABLED=true
LOCALCREW_REMOTE_BASE_URL=https://api.openai.com/v1
LOCALCREW_REMOTE_API_KEY=<your key>
LOCALCREW_REMOTE_MODELS=gpt-4o-mini
```

Then pick `remote:gpt-4o-mini` in a bot's model list (or set `LOCALCREW_DEFAULT_MODEL=remote:gpt-4o-mini`).
Settings → "Models & privacy" shows the provider host and exactly which data categories leave the machine.

**What is sent:** bot instructions and role, the conversation/objective, retrieved memories, tool results (web page
text, workspace file contents, connector data), deliverable drafts. **Never sent:** the API key itself (header only),
stored secrets, passwords, the secret key file. Plain `http://` is refused except for localhost.

**Costs:** you pay the provider per token. A typical report task here uses 3–6 requests.

**Verification status:** tested only against a local MOCK OpenAI-compatible server
(`tests/test_remote_provider.py`: streaming with fragmented tool-call arguments, tool-result linking, validation,
401 without retry, 429 retry, cancellation before the first byte, key never in request bodies or events). It has
**not** been run against a real provider from this repository (no API key was available). Run the real check with
your key:

```bash
LOCALCREW_REMOTE_PROVIDERS_ENABLED=true LOCALCREW_REMOTE_BASE_URL=https://api.openai.com/v1 LOCALCREW_REMOTE_API_KEY=... .venv/Scripts/python tests/eval/report_trials.py 3 remote:gpt-4o-mini
```

Deliverable validation (numeric grounding, structure, content checks) applies to API models exactly as to local ones.
