# Jarvis

A private, low-cost assistant base for a Mac: **Claude Code on your subscription** for short judgment calls, and
**local Ollama models** for everything heavy. Built from a setup that runs 24/7 on a Mac Studio (32 GB).

Three rules are enforced in code, not just documented:

1. **No pay-per-token APIs.** Model calls refuse to run if any provider API key is visible (`ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`, ...), because Claude Code would bill that key instead of your subscription. Claude calls also stop
   if the subscription's usage windows don't come back.
2. **Nothing private without consent.** Email, calendar, contacts, messages, files, photos, location... are denied until
   you grant them yourself at a terminal (`python3 -m jarvis consent grant calendar`). Scripts and AI sessions can check,
   never grant.
3. **Every install is asked for.** Ollama, Claude Code and each model show what they are (and their size) and wait for
   a `y`.

No third-party Python packages: standard library only.

## Setup (macOS, Apple Silicon)

```bash
git clone https://github.com/diegofalconcostilla/jarvis.git ~/jarvis && cd ~/jarvis
setup/install_hooks.sh            # blocks commits containing secrets or .env files
setup/01_check_guardrails.sh      # audit: no API keys in shell, launchd or profiles
setup/02_install_ollama.sh        # asks, then: Ollama as a memory-safe, localhost-only service
setup/03_pull_model.sh qwen3:30b-a3b   # shows size + free disk, asks, then installs ONE model
export JARVIS_MODEL=qwen3:30b-a3b # (add to ~/.zshrc if you like)
setup/04_setup_claude.sh          # asks, installs Claude Code, walks you through the SUBSCRIPTION login
python3 -m jarvis check --claude  # everything OK? (one tiny Claude call to prove it's the subscription)
python3 -m unittest discover tests
```

Then: `python3 -m jarvis ask "..."` (local model), add `--claude` to let Claude answer when within budget.

### What the Ollama service sets (and why)

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_HOST` | `127.0.0.1:11434` | nothing on your network can use or probe the models |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | two ~19 GB models don't fit in 32 GB; macOS would swap to the SSD |
| `OLLAMA_NUM_PARALLEL` | `1` | parallel requests multiply context memory |
| `OLLAMA_KEEP_ALIVE` | `5m` | unload when idle so memory and GPU rest |
| `OLLAMA_FLASH_ATTENTION` + `OLLAMA_KV_CACHE_TYPE=q8_0` | on | roughly halves context memory |

Model sizing on 32 GB: stay around 30B parameters at Q4 or smaller (~20 GB). A model that fits well:
`qwen3:30b-a3b` (writing), `qwen3-coder:30b` (code), `nomic-embed-text` (embeddings, 0.3 GB).

## Token optimization (how Claude use stays small)

- **Ollama first.** Drafting, summarizing, classifying, extracting, embeddings: all local, free.
- **Claude only judges.** Short calls where judgment matters (review a plan, pick the best of N, diagnose a failure).
- **Lean `claude -p` calls** (`jarvis/llm.py`): Claude Code's agent system prompt is replaced with one line, tools and
  MCP are disabled, and it runs from an empty folder. That cuts each call from ~20k input tokens to a few hundred.
- **Budget by real usage, not call counts.** Every call returns the subscription's 5-hour and 7-day utilization; Jarvis
  stops above 50% of the 5-hour window and never runs ahead of the week's pace (max 60%), leaving the rest for you.
  Tune with `JARVIS_FIVE_HOUR_MAX` / `JARVIS_SEVEN_DAY_MAX`.
- **Send diffs and summaries, not whole files**, and cache anything reused (plans, reviews).

## Layout

```
jarvis/guard.py     API-key detection, secret scanning
jarvis/consent.py   consent registry (~/.jarvis/consent.json, never in git)
jarvis/llm.py       ollama() / claude() / ask(), usage-window budget
jarvis/__main__.py  CLI: check, ask, consent, scan
setup/              consent-gated install scripts, Ollama launchd template, pre-commit hook
tests/              proof the guardrails work
CLAUDE.md           rules any Claude session working here must follow
SECURITY.md         threat model and what to do if something leaks
```
