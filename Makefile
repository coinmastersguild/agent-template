# Pinned so every agent decrypts with the same dotenvx the runtime uses.
DOTENVX := npx -y @dotenvx/dotenvx@2.33.0
LOCAL := OPENCLAW_STATE_DIR=$(CURDIR)/.openclaw OPENCLAW_CONFIG_PATH=$(CURDIR)/.openclaw/openclaw.json
PORT ?= 18790

.PHONY: setup secret key check run configure help
help: ## Show targets
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup: ## Install the pre-commit hook that blocks plaintext secrets
	git config core.hooksPath .githooks
	@echo "Hook installed. Add secrets with: make secret NAME=<name>"

secret: ## Encrypt one secret into .env (prompts; value never hits shell history)
	@test -n "$(NAME)" || { echo "usage: make secret NAME=TELEGRAM_BOT_TOKEN"; exit 1; }
	@printf '%s: ' "$(NAME)"; stty -echo 2>/dev/null; read -r value; stty echo 2>/dev/null; echo; \
	  $(DOTENVX) set --no-native --no-1password --no-bitwarden "$(NAME)" -- "$$value" >/dev/null && echo "Encrypted $(NAME) into .env. Commit .env; never .env.keys."

key: ## Copy the unlock key for Pioneer Studio to the clipboard
	@test -f .env.keys || { echo "No .env.keys yet. Run: make secret NAME=<name>"; exit 1; }
	@key=$$(sed -n 's/^DOTENV_PRIVATE_KEY="\{0,1\}\([0-9a-f]\{64\}\)"\{0,1\}$$/\1/p' .env.keys); \
	  test -n "$$key" || { echo ".env.keys has no DOTENV_PRIVATE_KEY"; exit 1; }; \
	  if command -v pbcopy >/dev/null; then printf %s "$$key" | pbcopy; echo "Unlock key copied. Paste it into Studio → Unlock."; \
	  else echo "$$key"; fi

check: ## Verify no plaintext secret is committed
	sh scripts/check-env.test.sh
	sh scripts/check-env.sh

configure: ## One-time: pick a model provider for local runs
	$(LOCAL) openclaw configure

run: ## Run this agent locally with decrypted secrets
	@mkdir -p .openclaw
	@$(LOCAL) openclaw config set agents.defaults.workspace $(CURDIR)/workspace >/dev/null
	$(LOCAL) $(DOTENVX) run -- openclaw gateway --port $(PORT) --allow-unconfigured
