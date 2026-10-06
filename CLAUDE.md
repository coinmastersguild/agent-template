# Editing this agent

This repository is an OpenClaw agent. `workspace/` is what the agent reads at
runtime; everything else is tooling.

- Agent behavior lives in `workspace/*.md`. Edit those to change what the agent does.
  `workspace/AGENTS.md` is instructions **for the agent**, not for you.
- Secrets: never write a plaintext value to `.env` and never read, print or copy
  `.env.keys`. Ask the user to run `make secret NAME=<NAME>` themselves.
- Run `make check` before committing. The pre-commit hook enforces the same rule.
- The deployed agent pulls this repository read-only. Changes take effect after the
  user pushes and presses **Pull & restart** in Pioneer Studio.
