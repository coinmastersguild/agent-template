import unittest
import tempfile
from pathlib import Path
import os
from unittest import mock
import start
from start import cron_plan, expand_servers, keep_tool_secrets_in_memory


class StartTest(unittest.TestCase):
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


    def test_tool_secret_store_moves_to_memory_and_old_disk_copy_is_removed(self):
        with tempfile.TemporaryDirectory() as disk, tempfile.TemporaryDirectory() as memory:
            old = Path(disk) / "mcp_clients"
            old.mkdir()
            (old / "mcp_clients.db").write_text("X_API_KEY=plain")
            keep_tool_secrets_in_memory(Path(disk), Path(memory))
            self.assertTrue(old.is_symlink())
            self.assertEqual(list(Path(memory, "openhuman-mcp-clients").iterdir()), [])
            Path(memory, "openhuman-mcp-clients").rmdir()  # a container restart empties tmpfs
            keep_tool_secrets_in_memory(Path(disk), Path(memory))
            self.assertTrue(old.is_symlink() and old.resolve().is_dir())


    def test_runtime_values_win_and_the_key_never_reaches_openhuman(self):
        with mock.patch.dict(os.environ, {"DOTENV_PRIVATE_KEY": "k" * 64, "MODEL": "pioneer", "UNLOCK_KEY_FILE": "/nonexistent"}), \
             mock.patch.object(start, "decrypt_env", return_value={"MODEL": "mine", "X_API_KEY": "x"}):
            env, unlocked = start.runtime_env()
        self.assertTrue(unlocked)
        self.assertEqual((env["MODEL"], env["X_API_KEY"]), ("pioneer", "x"))
        self.assertNotIn("DOTENV_PRIVATE_KEY", env)

    def test_wrong_key_stays_locked(self):
        with mock.patch.object(start, "decrypt_env", side_effect=ValueError("bad")):
            self.assertEqual(start.runtime_env()[1], False)


if __name__ == "__main__":
    unittest.main()
