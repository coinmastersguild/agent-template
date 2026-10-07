# The local fork runtime needs only Docker, git and make.
IMAGE := agent-runtime:local
# The reviewed public fork package currently targets amd64; Apple Silicon uses emulation.
PLATFORM ?= linux/amd64
DOCKER := docker run --platform $(PLATFORM) --rm -u "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$(CURDIR)":/agent
PORT ?= 7788

.PHONY: help setup image secret key check run
help: ## Show targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup: image ## Install the pre-commit hook that blocks plaintext secrets
	git config core.hooksPath .githooks
	@echo "Ready. Add secrets with: make secret NAME=<name>"

image:
	@docker build --platform $(PLATFORM) -q -t $(IMAGE) . >/dev/null

secret: image ## Encrypt one secret into .env (prompts; never in shell history)
	@test -n "$(NAME)" || { echo "usage: make secret NAME=X_API_KEY"; exit 1; }
	@printf '%s: ' "$(NAME)"; stty -echo 2>/dev/null; read -r VALUE; stty echo 2>/dev/null; echo; export VALUE; \
	  $(DOCKER) -e VALUE -e NAME=$(NAME) $(IMAGE) sh -c 'dotenvx set --no-native --no-1password --no-bitwarden "$$NAME" -- "$$VALUE" >/dev/null 2>&1' \
	  && echo "Encrypted $(NAME) into .env. Commit .env; never .env.keys."

key: ## Copy the unlock key for Pioneer Studio to the clipboard
	@key=$$(sed -n 's/^DOTENV_PRIVATE_KEY="\{0,1\}\([0-9a-f]\{64\}\)"\{0,1\}$$/\1/p' .env.keys 2>/dev/null); \
	  test -n "$$key" || { echo "No unlock key yet. Run: make secret NAME=<name>"; exit 1; }; \
	  if command -v pbcopy >/dev/null; then printf %s "$$key" | pbcopy; echo "Unlock key copied. Paste it into Studio → Unlock."; \
	  else echo "$$key"; fi

check: image ## Verify no plaintext secret is committed and the runtime works (also runs in CI)
	cd scripts && python3 -m unittest -q test_check_env test_runtime_distribution
	docker run --platform $(PLATFORM) --rm --network none -e PYTHONDONTWRITEBYTECODE=1 -v "$(CURDIR)":/agent:ro -w /agent/runtime $(IMAGE) \
	  python3 -m unittest -q test_envfile test_supervisor
	docker run --platform $(PLATFORM) --rm --init --network none --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL \
	  --security-opt no-new-privileges --tmpfs /run/agent:mode=0700 -v "$(CURDIR)":/source:ro \
	  --entrypoint python3 $(IMAGE) /source/scripts/smoke_local_runtime.py
	python3 scripts/check_env.py

run: image ## Run the local fork core with your MODEL_* settings
	@mkdir -p .data; test -s .data/core.token || openssl rand -hex 32 > .data/core.token
	@echo "Core: http://127.0.0.1:$(PORT)/rpc  token: .data/core.token  (OpenHuman app → remote core)"
	@DOTENV_PRIVATE_KEY=$$(sed -n 's/^DOTENV_PRIVATE_KEY="\{0,1\}\([0-9a-f]\{64\}\)"\{0,1\}$$/\1/p' .env.keys 2>/dev/null) \
	  OPENHUMAN_CORE_TOKEN=$$(cat .data/core.token) \
	  docker run --platform $(PLATFORM) --rm --init $$([ -t 0 ] && echo -it) -p 127.0.0.1:$(PORT):7788 --tmpfs /run/agent:mode=0700 \
	  -v "$(CURDIR)":/agent:ro -v "$(CURDIR)/.data":/data \
	  -e DOTENV_PRIVATE_KEY -e OPENHUMAN_CORE_TOKEN $(IMAGE)
