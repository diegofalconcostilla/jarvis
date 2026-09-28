# Security and privacy policy

## What Jarvis protects against

| Risk | Control |
|---|---|
| Surprise API bills | `guard.assert_no_api_billing()` runs before every model call; `check` audits the shell, launchd and shell profiles; Claude calls strip API variables from the child environment and stop if no subscription usage windows are reported |
| Private data read without asking | `consent.require(scope)` before email, calendar, contacts, messages, files, photos, location, keychain, microphone, camera, payments. Only a person at an interactive terminal can grant; grants live outside the repo |
| Unwanted downloads / disk and memory blowups | every install (Ollama, Claude Code, each model) asks first and shows size and free disk |
| Local models reachable from the network | Ollama bound to `127.0.0.1` only; `check` fails if it listens on all interfaces |
| Secrets committed to a public repo | `.gitignore` for `.env`/keys/consent, pre-commit hook scans staged files for API keys, bot tokens, GitHub/AWS/Google keys and private key blocks |
| Supply chain | no third-party Python packages |

## Hardening checklist for the Mac (manual, needs your password)

- Firewall on and stealth mode on: System Settings -> Network -> Firewall (Options -> Enable stealth mode).
- FileVault on (System Settings -> Privacy & Security -> FileVault).
- Remote Login (SSH) off unless you need it; if on, key-only (`PasswordAuthentication no`).
- Install macOS updates.
- Secrets files `chmod 600`.

## If a secret leaks

1. Revoke it at the source first (Telegram @BotFather `/revoke`, GitHub token settings, provider console).
2. Remove it from the history (`git filter-repo`), then force-push, then rotate anything that reused it.
3. Tell the owner what leaked, where and for how long.

Report problems by opening an issue (without including the secret).
