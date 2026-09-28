# Rules for any Claude session working on Jarvis

These are the owner's standing instructions. They override convenience.

## Never, without the owner's explicit consent in this conversation
- Enable or use anything billed per token: no API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...), no Console /
  API-billing login, no Bedrock/Vertex. Claude runs only on the owner's subscription.
- Install any model (Ollama included), app, or package. Propose it with its size and reason, wait for a yes.
- Read private data: email, calendar, contacts, messages, browser history, photos, location, keychain, or files outside
  this repo. In code, call `consent.require(scope)` first. Never grant consent yourself or edit `~/.jarvis/consent.json`.
- Commit or publish secrets. Keep the pre-commit hook installed; never use `--no-verify`.

## Token optimization
- Default to local Ollama models (`jarvis.llm.ollama`) for drafting, summarizing, extracting, classifying.
- Use Claude (`jarvis.llm.claude`) only for short judgment calls; keep prompts compact (summaries and diffs, not whole
  files); respect `usage_problem()` - if it returns a reason, don't call Claude, fall back or wait.
- One Ollama model in memory at a time on a 32 GB Mac; don't raise the GPU memory limit.

## Before finishing any change
- `python3 -m unittest discover tests` passes.
- `python3 -m jarvis check` has no new FAIL lines.
