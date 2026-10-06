# OpenClaw agent template

Your agent, in your own GitHub repository. Edit it locally (with Claude Code or any
editor), keep its secrets encrypted in git, and run it on Pioneer from
[Pioneer Studio](https://studio.pioneers.dev) — or on your own machine.

## Start

1. **Use this template** → create a repository under your account (private is fine).
2. Clone it and install the secret guard:
   ```sh
   make setup
   ```
3. Shape your agent in [`workspace/`](workspace) — `SOUL.md`, `IDENTITY.md`,
   `AGENTS.md`, `TOOLS.md`. These are standard OpenClaw workspace files.
4. Add secrets (bot tokens, API keys). Each is encrypted into `.env`:
   ```sh
   make secret NAME=TELEGRAM_BOT_TOKEN
   ```
   Commit `.env`. **Never commit `.env.keys`** — it holds your private key and is
   git-ignored. Back it up in your password manager; without it the secrets in
   `.env` cannot be recovered.
5. Push. In Studio: **GitHub → connect this repository**, then **Unlock** and paste
   the key from `make key`.

After that, edit → commit → push → **Pull & restart** in Studio.

## How the pieces fit

| Where | Holds | Can |
| --- | --- | --- |
| This repo | Workspace files, `.env` ciphertext | Be public or private; nothing in it is readable without your key |
| Your machine | `.env.keys` (the unlock key) | Edit, encrypt, run locally |
| Pioneer runtime | A read-only GitHub grant for repos you chose; your unlock key **in memory only** | Pull and run. It can't push, and it forgets the key on restart, so you unlock again |

On every **Pull & restart** the Pioneer runtime fetches this repository, copies
`workspace/` into the agent's workspace (your committed files win; notes and
memory the agent wrote itself are kept), and starts `dotenvx run -- openclaw
gateway` with your key in its environment. Decrypted values exist only inside your agent's process environment.
Pioneer operates the host; treat the runtime as isolated from other users, not
as hidden from the operator.

## Commands

| | |
| --- | --- |
| `make setup` | Install the pre-commit hook that rejects plaintext secrets |
| `make secret NAME=X` | Prompt for a value and encrypt it into `.env` |
| `make key` | Copy your unlock key to the clipboard for Studio |
| `make check` | Verify no committed `.env` value is plaintext (also runs in CI) |
| `make configure` / `make run` | Pick a local model provider once, then run the agent locally |

Requires `git`, `make`, Node.js (for `npx @dotenvx/dotenvx`) and, for local runs,
[OpenClaw](https://docs.openclaw.ai).

## Rotating the key

If `.env.keys` leaks: create a new key with `rm .env.keys .env && make secret ...`
for each secret, rotate every secret at its provider (the old ciphertext stays in
git history), push, and unlock again in Studio.

## License

MIT. Files in `workspace/` are adapted from OpenClaw's reference templates
(MIT, Copyright (c) 2025 Peter Steinberger); see [NOTICE](NOTICE).
