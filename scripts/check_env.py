"""Refuses committed plaintext secrets.

Every value in a tracked env file must be dotenvx ciphertext and private key files
must never be tracked. Lines are parsed with runtime/envfile.py, a port of dotenv's
parser, so anything dotenv would load is checked. Reads the git index (what is about to be
committed) and fails closed if any file can't be read.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runtime"))
from envfile import parse  # noqa: E402  (the same dotenv parser the runtime uses)


def is_env_file(path):
    name = path.rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def plaintext_keys(text):
    return [name for name, value in parse(text)
            if not (name.startswith("DOTENV_PUBLIC_KEY") or value == "" or value.startswith("encrypted:"))]


def problems(paths, read):
    out = []
    for path in paths:
        if not is_env_file(path) or path.endswith(".example"):
            continue
        if path.endswith(".keys"):
            out.append(f"{path}: private keys must never be committed (git rm --cached '{path}')")
            continue
        try:
            text = read(path)
        except Exception as error:  # fail closed: an unreadable env file is not a clean one
            out.append(f"{path}: could not be read ({error})")
            continue
        out += [f"{path}: {key} is not encrypted (re-set it with: make secret NAME={key})" for key in plaintext_keys(text)]
    return out


def index_paths():
    listing = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [p for p in listing.decode().split("\0") if p]


def read_index(path):
    return subprocess.run(["git", "show", f":{path}"], check=True, capture_output=True).stdout.decode()


if __name__ == "__main__":
    found = problems(index_paths(), read_index)
    print("\n".join(found) or "env files: all values encrypted")
    sys.exit(1 if found else 0)
