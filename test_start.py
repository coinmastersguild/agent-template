import unittest
from start import cron_plan, expand_servers


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


if __name__ == "__main__":
    unittest.main()
