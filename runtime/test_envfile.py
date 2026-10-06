import unittest

import envfile
from fixture import ENV, KEY


class Parse(unittest.TestCase):
    def test_matches_dotenv_including_quotes_comments_and_newlines(self):
        text = "A=1\nexport B='two'\nC = \"x\\ny\" # note\nD: four\n# E=5\nF=\nG=`back`\r\nH=\"multi\nline\"\n"
        self.assertEqual(envfile.parse(text), [("A", "1"), ("B", "two"), ("C", "x\ny"), ("D", "four"), ("F", ""),
                                               ("G", "back"), ("H", "multi\nline")])


class Decrypt(unittest.TestCase):
    def test_values_decrypt_literally_with_no_expansion_or_substitution(self):
        values = {name: envfile.decrypt(KEY, value) for name, value in envfile.parse(ENV) if value.startswith("encrypted:")}
        self.assertEqual(values, {"LEAK": "${DOTENV_PRIVATE_KEY}", "SUB": "$(echo substituted)", "PLAIN": "hello world",
                                  "MULTI": "line1\nline2", "QUOTES": 'a "quoted" # not a comment'})

    def test_wrong_key_and_tampering_fail(self):
        value = dict(envfile.parse(ENV))["PLAIN"]
        with self.assertRaises(ValueError):
            envfile.decrypt("c" * 64, value)
        body = value[len("encrypted:"):]
        flipped = body[:-6] + ("A" if body[-6] != "A" else "B") + body[-5:]
        with self.assertRaises(Exception):
            envfile.decrypt(KEY, "encrypted:" + flipped)
        with self.assertRaises(ValueError):
            envfile.decrypt(KEY, "plain")


if __name__ == "__main__":
    unittest.main()
