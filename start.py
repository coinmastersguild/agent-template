"""Boots openhuman-core and applies this repository's agent/ folder to it.

The same entry point runs on your machine (`make run`) and on Pioneer. It decrypts
.env with the unlock key, supervises openhuman-core, and reloads on SIGHUP. Nothing
here prints a secret value.
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "agent"
MANAGED = "repo:"  # prefix for cron jobs this script owns; others are left alone
PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")  # ${NAME} or ${NAME:-default}


def log(message):
    print(f"[start] {message}", flush=True)


def usable(value):
    """Locked agents see dotenvx ciphertext instead of a value."""
    return bool(value) and not value.startswith("encrypted:")


def expand_servers(servers, env):
    """Fill ${VAR} placeholders in env/headers. A server missing a value is disabled, not crashed."""
    out, missing = {}, {}
    for name, server in servers.items():
        server = json.loads(json.dumps(server))
        absent = []
        for block in ("env", "headers"):
            for key, value in (server.get(block) or {}).items():
                for var, default in PLACEHOLDER.findall(value):
                    if not usable(env.get(var)) and not default:
                        absent.append(var)
                server[block][key] = PLACEHOLDER.sub(
                    lambda m: env[m.group(1)] if usable(env.get(m.group(1))) else (m.group(2) or ""), value)
        if absent:
            server["enabled"] = False
            missing[name] = sorted(set(absent))
        out[name] = server
    return out, missing


def cron_plan(existing, wanted):
    """Ids to remove and jobs to add so the repo's managed jobs match cron.json exactly."""
    remove = [job["id"] for job in existing if str(job.get("name", "")).startswith(MANAGED)]
    add = [{**job, "name": MANAGED + job["name"]} for job in wanted]
    return remove, add


def keep_tool_secrets_in_memory(workspace, memory=Path("/dev/shm")):
    """OpenHuman stores tool-server env values in plaintext under mcp_clients/. start.py
    rebuilds that store from mcp.json on every boot, so it lives on tmpfs and never on disk."""
    store, target = workspace / "mcp_clients", memory / "openhuman-mcp-clients"
    if not memory.is_dir():
        return
    target.mkdir(mode=0o700, exist_ok=True)  # tmpfs is empty again after every container restart
    if store.is_symlink():
        return
    if store.exists():
        shutil.rmtree(store)
    store.symlink_to(target)


class Core:
    def __init__(self, port, token):
        self.url, self.token, self.next_id = f"http://127.0.0.1:{port}", token, 0

    def healthy(self):
        try:
            return urllib.request.urlopen(self.url + "/health", timeout=2).status == 200
        except OSError:
            return False

    def call(self, method, params=None):
        self.next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": "openhuman." + method, "params": params or {}})
        request = urllib.request.Request(self.url + "/rpc", body.encode(), {
            "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        reply = json.load(urllib.request.urlopen(request, timeout=60))
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result")


def result_list(value, key):
    """RPC results wrap lists differently across versions; accept the list or {key: list}."""
    if isinstance(value, dict):
        value = value.get(key, value.get("result", []))
    return value if isinstance(value, list) else []


def apply(core, env):
    core.call("auth_set_credential", {"token": "agent.offline.local", "kind": "local",
                                      "user": {"id": "local", "name": "Agent"}})
    core.call("config_update_analytics_settings", {"enabled": False})
    if env.get("MODEL_BASE_URL") and env.get("MODEL_API_KEY") and env.get("MODEL"):
        core.call("config_update_model_settings", {"inference_url": env["MODEL_BASE_URL"],
                                                   "api_key": env["MODEL_API_KEY"], "default_model": env["MODEL"]})
        log(f"model {env['MODEL']} via {env['MODEL_BASE_URL']}")
    else:
        log("MODEL_BASE_URL / MODEL_API_KEY / MODEL not set: the agent can't think until they are")

    servers, missing = expand_servers(json.loads((AGENT / "mcp.json").read_text())["mcpServers"], env)
    core.call("mcp_clients_config_set", {"mcpServers": servers})
    for name, names in missing.items():
        log(f"tool server {name} disabled until unlocked: needs {', '.join(names)}")
    log(f"tool servers: {', '.join(servers) or 'none'}")

    remove, add = cron_plan(result_list(core.call("cron_list"), "jobs"), json.loads((AGENT / "cron.json").read_text()))
    for job_id in remove:
        core.call("cron_remove", {"job_id": job_id})
    for job in add:
        core.call("cron_add", job)
    log(f"schedules: {', '.join(j['name'] for j in add) or 'none'}")


def unlock_key():
    """Pioneer writes the key to an in-memory file; local runs may pass it in the environment."""
    path = Path(os.environ.get("UNLOCK_KEY_FILE", "/run/agent/unlock"))
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return os.environ.get("DOTENV_PRIVATE_KEY", "")
    except OSError as error:
        log(f"staying locked: can't read the unlock key ({error.strerror})")
        return ""


def decrypt_env(key):
    """The repo's .env as plain values; empty when locked. A wrong key raises instead of half-working."""
    if not key or not (ROOT / ".env").is_file():
        return {}
    # An explicit empty keys file: the key must come from Pioneer or the environment, never a
    # stray .env.keys next to the checkout.
    result = subprocess.run(["dotenvx", "get", "-f", str(ROOT / ".env"), "-fk", "/dev/null", "--format", "json"],
                            env={**os.environ, "DOTENV_PRIVATE_KEY": key}, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("the unlock key does not decrypt .env")
    return {k: v for k, v in json.loads(result.stdout).items() if not k.startswith("DOTENV_PUBLIC_KEY")}


def runtime_env():
    """Values the runtime sets (Pioneer's MODEL_*, paths) win over the repo's. The key itself
    never reaches OpenHuman, so the agent's tools can't read it."""
    try:
        values = decrypt_env(unlock_key())
    except ValueError as error:
        log(f"staying locked: {error}")
        values = {}
    env = {**values, **os.environ}
    env.pop("DOTENV_PRIVATE_KEY", None)
    return env, bool(values)


def run_once(core, host, port, workspace):
    """Start the core with the current checkout and key, apply agent/, wait for it to exit."""
    env, unlocked = runtime_env()
    for path in AGENT.glob("*.md"):
        shutil.copy(path, workspace / path.name)  # the repo wins for files it ships; agent notes are kept
    keep_tool_secrets_in_memory(workspace)
    child = subprocess.Popen(["openhuman-core", "run", "--host", host, "--port", str(port)], env=env)
    for _ in range(120):
        if core.healthy() or child.poll() is not None:
            break
        time.sleep(0.5)
    if child.poll() is None:
        try:
            apply(core, env)
            log(f"agent is running ({'unlocked' if unlocked else 'locked'})")
        except Exception as error:  # keep the core up so the owner can inspect it from Studio
            log(f"configuration failed: {error}")
    return child


def main():
    if sys.argv[1:] == ["check-key"]:  # Pioneer verifies a key (on stdin) before storing it
        try:
            decrypt_env(sys.stdin.read().strip())
            sys.exit(0)
        except ValueError:
            sys.exit(3)
    data = Path(os.environ.setdefault("OPENHUMAN_WORKSPACE", str(ROOT / ".data")))
    token = os.environ.setdefault("OPENHUMAN_CORE_TOKEN", secrets.token_hex(32))
    port = int(os.environ.get("OPENHUMAN_CORE_PORT", "7788"))
    host = os.environ.get("OPENHUMAN_CORE_HOST", "127.0.0.1")
    workspace = data / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = Core(port, token)
    state = {"child": None, "reload": False, "stop": False}

    def on_signal(signum, _frame):
        state["reload" if signum == signal.SIGHUP else "stop"] = True
        if state["child"]:
            state["child"].terminate()

    # SIGHUP = re-read the checkout and the unlock key (Unlock, Pull & restart). SIGTERM = stop.
    signal.signal(signal.SIGHUP, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    while True:
        state["child"] = run_once(core, host, port, workspace)
        code = state["child"].wait()
        if state["reload"] and not state["stop"]:
            state["reload"] = False
            log("reloading")
            continue
        sys.exit(code)


if __name__ == "__main__":
    main()
