import unittest
from check_env import plaintext_keys, problems


class CheckEnvTest(unittest.TestCase):
    def test_only_ciphertext_empty_and_public_keys_pass(self):
        text = 'DOTENV_PUBLIC_KEY="03ab"\nA="encrypted:BG"\nB=\n# C=comment\nexport D=encrypted:x\n'
        self.assertEqual(plaintext_keys(text), [])

    def test_every_plaintext_shape_dotenv_loads_is_caught(self):
        for line in ['TOKEN = "review-placeholder"', "TOKEN=hunter2", "TOKEN: hunter2", "export TOKEN='x'",
                     "  TOKEN=`x`", 'TOKEN="multi\nline"', "TOKEN=x # comment", "TOKEN=x\r\n"]:
            self.assertEqual(plaintext_keys(line), ["TOKEN"], line)

    def test_paths_with_spaces_keys_files_and_read_errors_fail(self):
        def read(path):
            if path == "broken/.env":
                raise OSError("unreadable")
            return "TOKEN=plain"
        found = problems(["dir with spaces/.env", ".env.keys", "broken/.env", ".env.example", "README.md"], read)
        self.assertEqual(len(found), 3)
        self.assertIn("dir with spaces/.env: TOKEN is not encrypted", found[0])


if __name__ == "__main__":
    unittest.main()
