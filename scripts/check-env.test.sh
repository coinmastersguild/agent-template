#!/bin/sh
# Self-check for check-env.sh in a throwaway repository.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cd "$tmp"; git init -q
expect() { if sh "$here/check-env.sh" >/dev/null; then got=pass; else got=fail; fi
  [ "$got" = "$1" ] || { echo "FAIL: $2 (expected $1, got $got)"; exit 1; }; }
printf 'DOTENV_PUBLIC_KEY="03ab"\nTOKEN="encrypted:BGx"\nEMPTY=\n# PLAIN=comment\n' > .env; git add .env
expect pass "encrypted values, empty values and comments"
printf 'TOKEN=hunter2\n' >> .env; git add .env
expect fail "plaintext value"
git rm -q --cached .env; printf 'DOTENV_PRIVATE_KEY="ab"\n' > .env.keys; git add -f .env.keys
expect fail "tracked private key file"
git rm -q --cached .env.keys; printf 'TOKEN=example\n' > .env.example; git add .env.example
expect pass "example files are documentation"
echo "check-env: all cases pass"
