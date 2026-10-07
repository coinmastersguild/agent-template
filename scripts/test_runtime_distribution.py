"""The local template must stay on our verified fork and one tested architecture."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RuntimeDistribution(unittest.TestCase):
    def test_openhuman_download_is_only_our_pinned_public_fork(self):
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertNotIn('tinyhumansai/openhuman/releases', dockerfile)
        self.assertRegex(dockerfile, r'https://github\.com/coinmastersguild/openhuman/releases/download/')
        self.assertRegex(dockerfile, r'ARG OPENHUMAN_RELEASE=pioneer-local-v\d+\.\d+\.\d+')
        self.assertRegex(dockerfile, r'ARG OPENHUMAN_SHA256=[0-9a-f]{64}(?:\n|$)')
        self.assertIn('sha256sum -c -', dockerfile)

    def test_local_openai_compatible_runtime_admits_native_tools(self):
        # The generic core deliberately retains its configurable upstream default.
        # The template must opt into OpenAI native schemas: its old Python default
        # strips those schemas, leaving a configured agent unable to use tools.
        dockerfile = (ROOT / 'Dockerfile').read_text()
        self.assertRegex(dockerfile, r'ENV[^\n]*OPENHUMAN_TOOL_DISPATCHER=native(?: |\n|$)')

    def test_every_make_docker_call_selects_the_tested_platform(self):
        with tempfile.TemporaryDirectory(prefix='template-platform-') as directory:
            path = Path(directory)
            shutil.copy(ROOT / 'Makefile', path / 'Makefile')
            (path / 'scripts').mkdir()
            (path / 'scripts/test_check_env.py').write_text('import unittest\nclass Stub(unittest.TestCase):\n def test_fixture(self): pass\n')
            (path / 'scripts/test_runtime_distribution.py').write_text('import unittest\nclass Stub(unittest.TestCase):\n def test_fixture(self): pass\n')
            (path / 'scripts/check_env.py').write_text('')
            stub = path / 'docker'
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALL_LOG"\n')
            stub.chmod(0o755)
            calls = path / 'calls'
            env = {**os.environ, 'PATH': str(path) + os.pathsep + os.environ['PATH'], 'DOCKER_CALL_LOG': str(calls)}
            for target in [('image',), ('check',), ('run',), ('secret', 'NAME=DUMMY')]:
                result = subprocess.run(['make', *target], cwd=path, env=env, input='dummy-test-value\n',
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            commands = calls.read_text().splitlines()
            self.assertGreaterEqual(len(commands), 7)
            for command in commands:
                self.assertRegex(command, r'^(?:build|run) --platform linux/amd64(?: |$)', command)


if __name__ == '__main__':
    unittest.main()
