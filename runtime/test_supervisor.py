import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import envfile
import supervisor
from fixture import ENV, KEY

REAL_POPEN = subprocess.Popen
from supervisor import cron_plan, expand_servers, keep_tool_secrets_in_memory, on_tmpfs


class ToolServers(unittest.TestCase):
    def test_secrets_fill_placeholders_and_missing_ones_disable_the_server(self):
        servers = {"x": {"command": "python3", "env": {"KEY": "${KEY}", "MODE": "live"}},
                   "y": {"url": "https://example", "headers": {"Authorization": "Bearer ${TOKEN}"}}}
        out, missing = expand_servers(servers, {"KEY": "k1", "TOKEN": "encrypted:abc"})
        self.assertEqual(out["x"]["env"], {"KEY": "k1", "MODE": "live"})
        self.assertNotIn("enabled", out["x"])
        self.assertFalse(out["y"]["enabled"])
        self.assertEqual(missing, {"y": ["TOKEN"]})
        self.assertEqual(servers["x"]["env"]["KEY"], "${KEY}")  # input untouched

    def test_defaults_fill_optional_values_without_disabling(self):
        out, missing = expand_servers({"x": {"command": "c", "env": {"DRY": "${DRY:-0}", "ON": "${ON:-0}"}}}, {"ON": "1"})
        self.assertEqual(out["x"]["env"], {"DRY": "0", "ON": "1"})
        self.assertEqual(missing, {})

    def test_cron_replaces_only_repo_jobs(self):
        existing = [{"id": "a", "name": "repo:old"}, {"id": "b", "name": "made in the app"}]
        remove, add = cron_plan(existing, [{"name": "post", "schedule": {"kind": "every", "every_ms": 1}, "prompt": "p"}])
        self.assertEqual(remove, ["a"])
        self.assertEqual(add[0]["name"], "repo:post")


class SecretStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.disk, self.memory = Path(self.dir.name, "disk"), Path(self.dir.name, "memory")
        self.disk.mkdir(); self.memory.mkdir()
        self.tmpfs = lambda path: os.path.realpath(path).startswith(os.path.realpath(self.memory))

    def tearDown(self):
        self.dir.cleanup()

    def keep(self):
        keep_tool_secrets_in_memory(self.disk, self.memory, self.tmpfs)
        return self.disk / "mcp_clients"

    def test_old_disk_copy_is_replaced_by_a_link_into_memory(self):
        (self.disk / "mcp_clients").mkdir()
        (self.disk / "mcp_clients" / "mcp_clients.db").write_text("X_API_KEY=plain")
        store = self.keep()
        self.assertEqual(os.readlink(store), str(self.memory / "openhuman-mcp-clients"))
        self.assertEqual(list((self.memory / "openhuman-mcp-clients").iterdir()), [])

    def test_a_planted_link_to_persistent_storage_is_replaced(self):
        persistent = self.disk / "persistent"
        persistent.mkdir()
        (self.disk / "mcp_clients").symlink_to(persistent)
        store = self.keep()
        (store / "mcp_clients.db").write_text("dummy-secret")
        self.assertEqual(list(persistent.iterdir()), [])

    def test_restart_with_empty_tmpfs_recreates_the_target(self):
        self.keep()
        (self.memory / "openhuman-mcp-clients").rmdir()
        self.assertTrue(self.keep().resolve().is_dir())

    def test_no_tmpfs_refuses_to_start(self):
        with self.assertRaises(RuntimeError):
            keep_tool_secrets_in_memory(self.disk, self.memory, lambda path: False)
        with self.assertRaises(RuntimeError):
            keep_tool_secrets_in_memory(self.disk, Path(self.dir.name, "missing"), self.tmpfs)

    def test_a_planted_target_link_is_refused(self):
        (self.memory / "openhuman-mcp-clients").symlink_to(self.disk)
        with self.assertRaises(RuntimeError):
            self.keep()

    def test_mount_table_longest_prefix_decides(self):
        table = Path(self.dir.name, "mounts")
        table.write_text("overlay / overlay rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\n/dev/vda /data ext4 rw 0 0\n")
        self.assertTrue(on_tmpfs("/dev/shm/x", str(table)))
        self.assertFalse(on_tmpfs("/data/workspace", str(table)))
        self.assertFalse(on_tmpfs("/dev/shmx", str(table)))


class Unlocking(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name, ".env")
        self.patches = [mock.patch.object(supervisor, "AGENT_DIR", Path(self.dir.name)),
                        mock.patch.object(supervisor, "RUN", Path(self.dir.name, "run"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def check(self, key):
        with mock.patch("sys.stdin", io.StringIO(key)):
            return supervisor.check_key()

    def test_check_key_needs_a_real_encrypted_assignment(self):
        self.assertEqual(self.check(KEY), supervisor.NOTHING_TO_DECRYPT)  # no .env
        self.env.write_text("PLAIN=\n")
        self.assertEqual(self.check(KEY), supervisor.NOTHING_TO_DECRYPT)
        self.env.write_text('# "encrypted:abc" in a comment\nTOKEN=plaintext\n')  # review repro
        self.assertEqual(self.check("a" * 64), supervisor.NOTHING_TO_DECRYPT)

    def test_malformed_wrong_and_partial_keys_are_wrong(self):
        self.env.write_text(ENV)
        self.assertEqual(self.check("not-a-key"), supervisor.WRONG_KEY)
        self.assertEqual(self.check("c" * 64), supervisor.WRONG_KEY)
        other = dict(envfile.parse(ENV))["PLAIN"]
        self.env.write_text(ENV + 'FOREIGN="encrypted:' + "B" * 140 + '"\n')  # one value the key can't open
        self.assertEqual(self.check(KEY), supervisor.WRONG_KEY)
        self.assertTrue(other)

    def test_expansion_payloads_stay_literal_and_the_key_never_reaches_the_agent(self):
        self.env.write_text(ENV + "PLAINTEXT_SETTING=ignored\n")
        self.assertEqual(self.check(KEY), 0)
        with mock.patch.dict(os.environ, {"DOTENV_PRIVATE_KEY": KEY, "PLAIN": "runtime wins"}):
            values, lock = supervisor.unlock()
            env = supervisor.child_env(values)
        self.assertEqual(lock, "unlocked")
        self.assertEqual((env["LEAK"], env["SUB"], env["PLAIN"]), ("${DOTENV_PRIVATE_KEY}", "$(echo substituted)", "runtime wins"))
        self.assertNotIn("PLAINTEXT_SETTING", values)
        self.assertNotIn("DOTENV_PRIVATE_KEY", env)
        self.assertFalse(any(KEY in v for v in env.values()))

    def test_a_value_containing_the_key_is_refused(self):
        with mock.patch.object(supervisor.envfile, "decrypt", return_value="prefix-" + KEY):
            self.env.write_text('A="encrypted:x"\n')
            self.assertEqual(self.check(KEY), supervisor.WRONG_KEY)


class Status(unittest.TestCase):
    def test_lock_is_reported_only_while_running_and_tied_to_the_reload(self):
        with tempfile.TemporaryDirectory() as run, mock.patch.object(supervisor, "RUN", Path(run)):
            Path(run, "reload").write_text("req-42\n")
            for state in ("starting", "configuration_failed", "failed"):
                supervisor.write_status(state, supervisor.reload_id(), "unlocked")
                status = json.loads(Path(run, "status.json").read_text())
                self.assertEqual((status["lock"], status["reload_id"]), (None, "req-42"))
            supervisor.write_status("running", supervisor.reload_id(), "unlocked")
            self.assertEqual(json.loads(Path(run, "status.json").read_text())["lock"], "unlocked")
            Path(run, "reload").write_text("bad id with spaces")
            self.assertIsNone(supervisor.reload_id())


class Lifecycle(unittest.TestCase):
    """A reload or stop at any point of startup must end the child, within a bound."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = supervisor.new_state()
        self.on_signal = supervisor.handler(self.state)
        self.children = []
        self.patches = [mock.patch.object(supervisor, "RUN", Path(self.dir.name)),
                        mock.patch.object(supervisor, "AGENT_DIR", Path(self.dir.name)),
                        mock.patch.object(supervisor, "unlock", side_effect=self.unlock),
                        mock.patch.object(supervisor.subprocess, "Popen", side_effect=self.spawn),
                        mock.patch.object(supervisor, "STOP_GRACE", 1.0)]
        for p in self.patches:
            p.start()
        self.ignore_term = False
        self.before_spawn = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.dir.cleanup()

    def unlock(self):
        if self.before_spawn:
            self.on_signal(self.before_spawn, None)
        return {}, "locked"

    def spawn(self, *_args, **_kwargs):
        code = "import signal, time\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if self.ignore_term else "") + "time.sleep(60)"
        child = REAL_POPEN([sys.executable, "-c", code])
        self.children.append(child)
        time.sleep(0.2)  # let the child install its handlers
        return child

    def core(self, healthy=True, on_health=None):
        core = mock.Mock()
        def check():
            if on_health:
                on_health()
            return healthy
        core.healthy.side_effect = check
        return core

    def status(self):
        path = Path(self.dir.name, "status.json")
        return json.loads(path.read_text())["state"] if path.exists() else None

    def finish(self, core):
        started = time.monotonic()
        supervisor.run_once(core, self.state)
        supervisor.wait_child(self.state)
        return time.monotonic() - started

    def test_sighup_during_startup_ends_the_child(self):
        core = self.core(healthy=False, on_health=lambda: self.on_signal(signal.SIGHUP, None))
        elapsed = self.finish(core)
        self.assertIsNotNone(self.children[0].poll())
        self.assertTrue(self.state["reload"])
        self.assertEqual(self.status(), "starting")  # never claims running or failed for this attempt
        self.assertLess(elapsed, 5)

    def test_sigterm_during_configuration_ends_the_child(self):
        def apply(_core, _env):
            self.on_signal(signal.SIGTERM, None)
            raise ConnectionRefusedError("core went away")
        with mock.patch.object(supervisor, "apply", side_effect=apply):
            elapsed = self.finish(self.core())
        self.assertIsNotNone(self.children[0].poll())
        self.assertTrue(self.state["stop"])
        self.assertEqual(self.status(), "starting")  # not reported as configuration_failed
        self.assertLess(elapsed, 5)

    def test_sighup_during_configuration_that_still_succeeds_is_not_reported_running(self):
        with mock.patch.object(supervisor, "apply", side_effect=lambda *_: self.on_signal(signal.SIGHUP, None)):
            self.finish(self.core())
        self.assertIsNotNone(self.children[0].poll())
        self.assertEqual(self.status(), "starting")

    def test_signal_before_the_child_exists_still_ends_it(self):
        self.before_spawn = signal.SIGTERM
        self.finish(self.core())
        self.assertIsNotNone(self.children[0].poll())

    def test_a_core_ignoring_sigterm_is_killed_after_the_grace_period(self):
        self.ignore_term = True
        core = self.core(healthy=False, on_health=lambda: self.on_signal(signal.SIGTERM, None))
        elapsed = self.finish(core)
        self.assertEqual(self.children[0].returncode, -signal.SIGKILL)
        self.assertLess(elapsed, 5)

    def test_signal_while_running_ends_the_child_within_the_bound(self):
        self.ignore_term = True
        with mock.patch.object(supervisor, "apply"):
            supervisor.run_once(self.core(), self.state)
        self.assertEqual(self.status(), "running")
        threading.Timer(0.3, self.on_signal, (signal.SIGTERM, None)).start()
        started = time.monotonic()
        supervisor.wait_child(self.state)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(self.children[0].poll())


if __name__ == "__main__":
    unittest.main()
