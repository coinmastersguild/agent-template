"""Trusted agent supervisor, baked into the runtime image at /opt/agent-runtime.

It runs as root inside the container and is never read from the user's checkout.
Only it can read the unlock key (/run/agent is a root-only tmpfs). It decrypts .env
with dotenvx running as `nobody`, then starts OpenHuman as the unprivileged `agent`
user with the decrypted values but never the key. Status for Pioneer goes to
/run/agent/status.json, which agent code can't write. Nothing here prints a secret.

  supervisor.py            supervise; SIGHUP re-reads the checkout and key, SIGTERM stops
  supervisor.py check-key  key on stdin; exit 0 decrypts .env, 3 wrong key, 4 nothing to decrypt
  supervisor.py core       internal: prepare the workspace as `agent`, then exec openhuman-core
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

AGENT_DIR = Path(os.environ.get("AGENT_DIR", "/agent"))  # the user's checkout: data, never code we run as root
RUN = Path(os.environ.get("AGENT_RUN_DIR", "/run/agent"))  # root-only tmpfs: unlock key and status
AGENT_UID = 1000
MANAGED = "repo:"  # prefix for cron jobs this supervisor owns; others are left alone
PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")  # ${NAME} or ${NAME:-default}
WRONG_KEY, NOTHING_TO_DECRYPT = 3, 4


def log(message):
    print(f"[supervisor] {message}", flush=True)


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


# ── unlocking (root) ────────────────────────────────────────────────────────────

class NothingToDecrypt(Exception):
    pass


def as_user(name):
    """Drop to `name` for a child process when running as root; tests run unprivileged."""
    return {"user": name, "group": name if name != "nobody" else "nogroup", "extra_groups": []} if os.geteuid() == 0 else {}


def decrypt_env(key):
    """The checkout's .env as plain values. Raises unless at least one encrypted value decrypts."""
    env_file = AGENT_DIR / ".env"
    try:
        text = env_file.read_text()
    except FileNotFoundError:
        raise NothingToDecrypt from None
    if "encrypted:" not in text:
        raise NothingToDecrypt
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("not an unlock key")
    # dotenvx parses user-supplied content, so it runs as `nobody`. An explicit empty keys
    # file means the key can only come from here, never a stray .env.keys in the checkout.
    result = subprocess.run(["dotenvx", "get", "-f", str(env_file), "-fk", "/dev/null", "--format", "json"],
                            env={"PATH": os.environ.get("PATH", ""), "HOME": "/tmp", "DOTENV_PRIVATE_KEY": key},
                            capture_output=True, text=True, timeout=60, **as_user("nobody"))
    if result.returncode:
        raise ValueError("the unlock key does not decrypt .env")
    values = {k: v for k, v in json.loads(result.stdout).items() if not k.startswith("DOTENV_PUBLIC_KEY")}
    if any(not usable(v) and v for v in values.values()):
        raise ValueError("some values did not decrypt")
    return values


def read_key():
    try:
        return (RUN / "unlock").read_text().strip()
    except FileNotFoundError:
        return os.environ.get("DOTENV_PRIVATE_KEY", "")  # local `make run` only


def unlock():
    """(decrypted values, lock state). Only a successful decryption counts as unlocked."""
    key = read_key()
    if not key:
        return {}, "locked"
    try:
        return decrypt_env(key), "unlocked"
    except NothingToDecrypt:
        return {}, "nothing_to_decrypt"
    except ValueError:
        return {}, "key_mismatch"


def child_env(values):
    """Runtime values (Pioneer's MODEL_*, paths) win over the repo's; the key never passes."""
    env = {**values, **os.environ, "HOME": "/home/agent", "USER": "agent"}
    env.pop("DOTENV_PRIVATE_KEY", None)
    return env


def write_status(**fields):
    """Pioneer reads this as root; agent processes can't write /run/agent."""
    try:
        tmp = RUN / ".status.tmp"
        tmp.write_text(json.dumps({**fields, "at": int(time.time())}))
        os.replace(tmp, RUN / "status.json")
    except OSError as error:
        log(f"status unavailable ({error.strerror})")


# ── workspace (runs as `agent`) ─────────────────────────────────────────────────

def on_tmpfs(path, mounts="/proc/self/mounts"):
    """True only when `path` resolves onto a tmpfs mount (longest matching mount point wins)."""
    real, best = os.path.realpath(path), ("", "")
    with open(mounts) as table:
        for line in table:
            point, kind = line.split()[1].replace("\\040", " "), line.split()[2]
            if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best[0]):
                best = (point, kind)
    return best[1] == "tmpfs"


def keep_tool_secrets_in_memory(workspace, memory=Path("/dev/shm"), is_tmpfs=on_tmpfs):
    """OpenHuman stores tool-server env values in plaintext under mcp_clients/. That store is
    rebuilt from mcp.json every boot, so it must live on tmpfs. Refuses to continue otherwise."""
    store, target = workspace / "mcp_clients", memory / "openhuman-mcp-clients"
    if not memory.is_dir() or not is_tmpfs(memory):
        raise RuntimeError(f"{memory} is not tmpfs; refusing to write tool secrets to disk")
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise RuntimeError(f"{target} is not a plain directory; refusing to use it")
    target.mkdir(mode=0o700, exist_ok=True)  # tmpfs is empty again after every container restart
    if store.is_symlink() and os.readlink(store) == str(target):
        pass
    elif store.is_symlink() or store.is_file():
        store.unlink()
    elif store.exists():
        shutil.rmtree(store)
    if not store.is_symlink():
        store.symlink_to(target)
    if os.path.realpath(store) != os.path.realpath(target) or not is_tmpfs(store):
        raise RuntimeError("tool secret store is not on tmpfs; refusing to start")


def core_mode():
    workspace = Path(os.environ["OPENHUMAN_WORKSPACE"]) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for path in (AGENT_DIR / "agent").glob("*.md"):
        shutil.copy(path, workspace / path.name)  # the repo wins for files it ships; agent notes are kept
    keep_tool_secrets_in_memory(workspace)
    os.execvp("openhuman-core", ["openhuman-core", "run", "--host", os.environ["OPENHUMAN_CORE_HOST"],
                                 "--port", os.environ["OPENHUMAN_CORE_PORT"]])


# ── configuration over RPC (root) ───────────────────────────────────────────────

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

    servers, missing = expand_servers(json.loads((AGENT_DIR / "agent" / "mcp.json").read_text())["mcpServers"], env)
    core.call("mcp_clients_config_set", {"mcpServers": servers})
    for name, names in missing.items():
        log(f"tool server {name} disabled until unlocked: needs {', '.join(names)}")
    log(f"tool servers: {', '.join(servers) or 'none'}")

    remove, add = cron_plan(result_list(core.call("cron_list"), "jobs"),
                            json.loads((AGENT_DIR / "agent" / "cron.json").read_text()))
    for job_id in remove:
        core.call("cron_remove", {"job_id": job_id})
    for job in add:
        core.call("cron_add", job)
    log(f"schedules: {', '.join(j['name'] for j in add) or 'none'}")


# ── supervision (root) ──────────────────────────────────────────────────────────

def run_once(core):
    values, lock = unlock()
    log(f"lock state: {lock}")
    write_status(state="starting", lock=lock)
    child = subprocess.Popen([sys.executable, __file__, "core"], env=child_env(values), **as_user("agent"))
    for _ in range(120):
        if core.healthy() or child.poll() is not None:
            break
        time.sleep(0.5)
    if child.poll() is not None:
        write_status(state="failed", lock=lock)
        return child
    try:
        apply(core, child_env(values))
        write_status(state="running", lock=lock)
        log("agent is running")
    except Exception as error:  # keep the core up so the owner can inspect it from Studio
        write_status(state="configuration_failed", lock=lock)
        log(f"configuration failed: {error}")
    return child


def check_key():
    try:
        decrypt_env(sys.stdin.read().strip())
        return 0
    except NothingToDecrypt:
        return NOTHING_TO_DECRYPT
    except ValueError:
        return WRONG_KEY


def main():
    if sys.argv[1:] == ["check-key"]:
        sys.exit(check_key())
    if sys.argv[1:] == ["core"]:
        core_mode()
    os.environ.setdefault("OPENHUMAN_WORKSPACE", "/data")
    os.environ.setdefault("OPENHUMAN_CORE_HOST", "127.0.0.1")
    os.environ.setdefault("OPENHUMAN_CORE_PORT", "7788")
    token = os.environ.setdefault("OPENHUMAN_CORE_TOKEN", secrets.token_hex(32))
    core = Core(int(os.environ["OPENHUMAN_CORE_PORT"]), token)
    state = {"child": None, "reload": False, "stop": False}

    def on_signal(signum, _frame):
        state["reload" if signum == signal.SIGHUP else "stop"] = True
        if state["child"]:
            state["child"].terminate()

    signal.signal(signal.SIGHUP, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    while True:
        state["child"] = run_once(core)
        code = state["child"].wait()
        if state["reload"] and not state["stop"]:
            state["reload"] = False
            log("reloading")
            continue
        write_status(state="stopped", lock="locked")
        sys.exit(code)


if __name__ == "__main__":
    main()
