"""Trusted agent supervisor, baked into the runtime image at /opt/agent-runtime.

It runs as root inside the container and is never read from the user's checkout.
Only it can read the unlock key (/run/agent is a root-only tmpfs). It decrypts each
encrypted .env value itself (envfile.py: no expansion, no command substitution, no
dotenvx), then starts OpenHuman as the unprivileged `agent` user with those values but
never the key. Status for Pioneer goes to /run/agent/status.json, which agent code
can't write. Nothing here prints a secret.

  supervisor.py            supervise; SIGHUP re-reads the checkout, key and reload id; SIGTERM stops
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
import threading
import time
import urllib.request
from pathlib import Path

import envfile

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
    return {"user": name, "group": name, "extra_groups": []} if os.geteuid() == 0 else {}


def decrypt_env(key):
    """Every encrypted assignment in the checkout's .env, decrypted literally.

    Raises NothingToDecrypt unless the file has at least one encrypted assignment, and
    ValueError unless the key opens all of them. Plaintext assignments are ignored."""
    try:
        pairs = envfile.parse((AGENT_DIR / ".env").read_text())
    except FileNotFoundError:
        raise NothingToDecrypt from None
    encrypted = {name: value for name, value in pairs if value.startswith("encrypted:")}
    if not encrypted:
        raise NothingToDecrypt
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("not an unlock key")
    try:
        values = {name: envfile.decrypt(key, value) for name, value in encrypted.items()}
    except Exception:
        raise ValueError("the unlock key does not decrypt .env") from None
    if any(key in value for value in values.values()):
        raise ValueError("a value contains the unlock key")
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


def reload_id():
    """Pioneer writes an id to /run/agent/reload before each SIGHUP and waits for status
    to echo it, so a result is always tied to the request that caused it."""
    try:
        value = (RUN / "reload").read_text().strip()
    except FileNotFoundError:
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else None


def write_status(state, reload, lock=None):
    """Pioneer reads this as root; agent processes can't write /run/agent. `lock` is
    reported only once the agent is running, so it never describes a failed start."""
    try:
        tmp = RUN / ".status.tmp"
        tmp.write_text(json.dumps({"state": state, "reload_id": reload, "lock": lock if state == "running" else None,
                                   "at": int(time.time())}))
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
    """One RPC client per start attempt. Cancelling it stops a configuration worker that
    outlived its attempt from ever reaching the next attempt's core on the same port."""

    def __init__(self, port, token):
        self.url, self.token, self.next_id, self.cancelled = f"http://127.0.0.1:{port}", token, 0, False

    def cancel(self):
        self.cancelled = True

    def healthy(self):
        try:
            return urllib.request.urlopen(self.url + "/health", timeout=2).status == 200
        except OSError:
            return False

    def call(self, method, params=None):
        if self.cancelled:
            raise RuntimeError("start attempt cancelled")
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

STOP_GRACE = float(os.environ.get("AGENT_STOP_GRACE_SECONDS", "20"))


def new_state():
    return {"child": None, "reload": False, "stop": False, "deadline": None}


def handler(state):
    """SIGHUP = reload, SIGTERM = stop. The shutdown deadline starts now, and a watchdog
    kills the core at that deadline no matter what the main thread is doing."""
    def on_signal(signum, _frame):
        state["reload" if signum == signal.SIGHUP else "stop"] = True
        if state["deadline"] is None:
            state["deadline"] = time.monotonic() + STOP_GRACE
        child = state["child"]
        if child and child.poll() is None:
            child.terminate()
            watchdog = threading.Timer(max(0.0, state["deadline"] - time.monotonic()),
                                       lambda: child.poll() is None and child.kill())
            watchdog.daemon = True
            watchdog.start()
    return on_signal


def interrupted(state):
    return state["reload"] or state["stop"]


def stop_child(state):
    """Terminate the core and kill it if it is still alive at the deadline, which started
    when the signal arrived (or now, if the stop comes from elsewhere)."""
    child = state["child"]
    if child is None or child.poll() is not None:
        return
    child.terminate()
    deadline = state["deadline"] or time.monotonic() + STOP_GRACE
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.poll() is None:
        child.kill()
        child.wait()


def wait_child(state):
    """Wait for the core to exit; once a signal is pending, no longer than its deadline."""
    child = state["child"]
    while child.poll() is None:
        if interrupted(state):
            stop_child(state)
        time.sleep(0.2)
    return child.returncode


def run_once(core, state):
    """Start the core and configure it. Configuration runs in a worker thread so a signal
    is noticed within 0.2 s even while an RPC is stalled; an interrupted attempt never
    reports running or configuration_failed."""
    reload = reload_id()
    values, lock = unlock()
    log(f"lock state: {lock}")
    write_status("starting", reload)
    state["child"] = child = subprocess.Popen([sys.executable, __file__, "core"], env=child_env(values), **as_user("agent"))
    for _ in range(120):
        if interrupted(state) or core.healthy() or child.poll() is not None:
            break
        time.sleep(0.5)
    if interrupted(state):
        core.cancel()
        return stop_child(state)
    if child.poll() is not None:
        return write_status("failed", reload)
    outcome = {}
    done = threading.Event()

    def configure():
        try:
            apply(core, child_env(values))
        except Exception as error:
            outcome["error"] = error
        finally:
            done.set()

    threading.Thread(target=configure, daemon=True).start()
    while not done.wait(0.2):
        if interrupted(state):
            break
    if interrupted(state):
        core.cancel()
        return stop_child(state)
    if "error" in outcome:  # keep the core up so the owner can inspect it from Studio
        log(f"configuration failed: {outcome['error']}")
        return write_status("configuration_failed", reload)
    write_status("running", reload, lock)
    log("agent is running")


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
    state = new_state()
    signal.signal(signal.SIGHUP, handler(state))
    signal.signal(signal.SIGTERM, handler(state))
    while True:
        run_once(Core(int(os.environ["OPENHUMAN_CORE_PORT"]), token), state)
        code = wait_child(state)
        state["child"] = None
        if state["reload"] and not state["stop"]:
            state["reload"], state["deadline"] = False, None
            log("reloading")
            continue
        write_status("stopped", reload_id())
        sys.exit(0 if state["stop"] else code)  # a requested stop is clean; otherwise report the core's exit


if __name__ == "__main__":
    main()
