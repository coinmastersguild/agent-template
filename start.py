"""Boots openhuman-core and applies this repository's agent/ folder to it.

The same entry point runs on your machine (`make run`) and on Pioneer. Inputs come
from the environment; secrets arrive already decrypted by `dotenvx run`. Nothing
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
        core.call("cron_remove", {"id": job_id})
    for job in add:
        core.call("cron_add", job)
    log(f"schedules: {', '.join(j['name'] for j in add) or 'none'}")


def main():
    data = Path(os.environ.setdefault("OPENHUMAN_WORKSPACE", str(ROOT / ".data")))
    token = os.environ.setdefault("OPENHUMAN_CORE_TOKEN", secrets.token_hex(32))
    port = int(os.environ.get("OPENHUMAN_CORE_PORT", "7788"))
    host = os.environ.get("OPENHUMAN_CORE_HOST", "127.0.0.1")

    # Persona files: the repo wins for files it ships; notes the agent wrote itself are kept.
    workspace = data / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for path in AGENT.glob("*.md"):
        shutil.copy(path, workspace / path.name)

    child = subprocess.Popen(["openhuman-core", "run", "--host", host, "--port", str(port)])
    signal.signal(signal.SIGTERM, lambda *_: child.terminate())
    core = Core(port, token)
    for _ in range(120):
        if core.healthy() or child.poll() is not None:
            break
        time.sleep(0.5)
    if child.poll() is not None:
        sys.exit(f"[start] openhuman-core exited with {child.returncode}")
    try:
        apply(core, os.environ)
        log("agent is running")
    except Exception as error:  # keep the core up so the owner can inspect it from Studio
        log(f"configuration failed: {error}")
    sys.exit(child.wait())


if __name__ == "__main__":
    main()
