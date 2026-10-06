import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import envfile
import supervisor
from fixture import ENV, KEY
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


if __name__ == "__main__":
    unittest.main()
