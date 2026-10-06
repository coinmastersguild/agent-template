# Editing this agent

This repository is an OpenHuman agent. `agent/` is what the agent is; everything
else is tooling. `start.py` applies `agent/` to OpenHuman at boot.

- Behavior: `agent/SOUL.md`, `agent/IDENTITY.md`. Tools: `agent/mcp.json` (+ code in
  `agent/tools/`). Schedules: `agent/cron.json`.
- Secrets: reference them as `${NAME}` in `mcp.json`. Never write a plaintext value to
  `.env` and never read, print or copy `.env.keys`. Ask the user to run
  `make secret NAME=<NAME>` themselves.
- Run `make check` before committing, and `make run` to try changes locally.
- The deployed agent pulls this repository read-only. Changes take effect after the
  user pushes and presses **Pull & restart** in Pioneer Studio.
