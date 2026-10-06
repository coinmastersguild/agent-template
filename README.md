# Agent template

Your [OpenHuman](https://github.com/tinyhumansai/openhuman) agent in your own GitHub
repository. Describe it in plain files, keep its secrets encrypted in git, run it on
your machine, then run the identical container on Pioneer from
[Pioneer Studio](https://studio.pioneers.dev).

## Start

1. **Use this template** → create a repository under your account (private is fine).
2. Clone it, then `make setup` (builds the runtime image, installs the secret guard).
3. Describe your agent in [`agent/`](agent):

   | File | What it is |
   | --- | --- |
   | `SOUL.md`, `IDENTITY.md` | Personality and purpose. Any `agent/*.md` is copied into OpenHuman's workspace |
   | `mcp.json` | Tool servers (MCP). Use `${NAME}` for secrets, `${NAME:-default}` for optional settings |
   | `cron.json` | Scheduled jobs: `[{"name", "schedule", "prompt"}]` |
   | `tools/` | Your own tool code, if any |

4. Secrets (API keys, bot tokens) go in an encrypted `.env`:
   ```sh
   make secret NAME=X_API_KEY
   ```
   Commit `.env`. **Never commit `.env.keys`**: it holds your private key and is
   git-ignored. Back it up in your password manager.
5. Give it a model and run it locally, exactly as Pioneer will:
   ```sh
   make secret NAME=MODEL_BASE_URL   # any OpenAI-compatible endpoint
   make secret NAME=MODEL_API_KEY
   make secret NAME=MODEL
   make run
   ```
   Chat with it from the OpenHuman desktop app: choose a remote core, URL
   `http://127.0.0.1:7788/rpc`, token from `.data/core.token`.
6. Push. In Studio: **GitHub → connect this repository**, **Unlock** with the key
   from `make key`, then **Pull & restart** whenever you push.

On Pioneer the runtime supplies `MODEL_*` from your agent's prepaid budget; your own
values apply only if you set them.

## How it runs

`start.py` is the whole runtime contract. It decrypts `.env` with the unlock key,
copies `agent/*.md` into OpenHuman's workspace (your files win; notes the agent wrote
itself are kept), starts `openhuman-core`, signs in with OpenHuman's offline local
session (no TinyHumans account), turns analytics off, points inference at
`MODEL_BASE_URL`, installs `mcp.json` and replaces the schedules it owns with
`cron.json`. A tool server whose secret is missing is disabled rather than crashing
the agent, so a locked agent still boots.

- The unlock key comes from `/run/agent/unlock` (an in-memory file Pioneer writes) or,
  locally, `DOTENV_PRIVATE_KEY`. It is never passed to OpenHuman or your tools; they
  only see the decrypted values.
- `SIGHUP` re-reads the checkout and the key (Unlock, Pull & restart) without
  restarting the container. Restarting the container forgets the key.
- OpenHuman keeps tool-server secrets in a store that `start.py` places in
  `/dev/shm`, so decrypted secrets never land on disk.
- `python3 start.py check-key` verifies a key given on stdin (exit 0 or 3).

| Where | Holds | Can |
| --- | --- | --- |
| This repo | Agent files, `.env` ciphertext | Be public or private; secrets are unreadable without your key |
| Your machine | `.env.keys` (the unlock key) | Edit, encrypt, run locally |
| Pioneer runtime | A read-only GitHub grant for this one repository; your unlock key **in memory only** | Pull and run. It can't push, and it forgets the key on restart, so you unlock again |

Pioneer operates the host: the runtime is isolated from other users, not hidden from
the operator. Pioneer runs this image built from the upstream template at a pinned
version; your copy of `Dockerfile` is used only locally.

## Commands

| | |
| --- | --- |
| `make setup` | Build the runtime image and install the pre-commit secret guard |
| `make secret NAME=X` | Prompt for a value and encrypt it into `.env` |
| `make key` | Copy your unlock key to the clipboard for Studio |
| `make check` | Fail if any committed `.env` value is plaintext (also runs in CI) |
| `make run` | Run the agent locally in the Pioneer runtime image |

Requires Docker, git and make (and Python 3 for `make check`).

## Rotating the key

If `.env.keys` leaks: `rm .env.keys .env`, re-add each secret with `make secret`,
rotate every secret at its provider (old ciphertext stays in git history), push,
and unlock again in Studio.

## License

MIT. OpenHuman itself is GPL-3.0 and is downloaded, not included, by `Dockerfile`.
