#!/bin/sh
# Refuses committed plaintext secrets: every value in a tracked .env file must be
# dotenvx ciphertext, and private key files must never be tracked. Reads the git
# index, so the pre-commit hook checks exactly what is about to be committed.
set -eu
status=0
for f in $(git ls-files -- '.env' '.env.*' '**/.env' '**/.env.*'); do
  case "$f" in
    *.example) continue ;;
    *.keys) echo "$f: private keys must never be committed (git rm --cached $f)"; status=1; continue ;;
  esac
  plain=$(git show ":$f" | grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
    | grep -vE '^[[:space:]]*(export[[:space:]]+)?DOTENV_PUBLIC_KEY[A-Za-z0-9_]*=' \
    | grep -vE '=[[:space:]]*"?encrypted:' | grep -vE '=[[:space:]]*("")?[[:space:]]*$' | cut -d= -f1 || true)
  if [ -n "$plain" ]; then
    echo "$f has unencrypted values. Re-set each with: make secret NAME=<name>"
    echo "$plain" | sed 's/^/  /'
    status=1
  fi
done
exit $status
