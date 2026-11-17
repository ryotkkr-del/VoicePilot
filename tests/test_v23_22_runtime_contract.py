"""The supported Python runtime is a safety and reproducibility boundary."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PythonRuntimeContractTests(unittest.TestCase):
    def test_all_user_entrypoints_fail_closed_outside_python_311(self):
        required = 'sys.version_info[:2] == (3, 11)'
        for name in ('setup_and_run.bat', 'try_voicepilot.bat', 'run_tests.bat'):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding='utf-8')
                self.assertIn(required, content)
                self.assertIn('exit /b 1', content)

    def test_entrypoints_do_not_delete_an_existing_environment(self):
        for name in ('setup_and_run.bat', 'try_voicepilot.bat', 'run_tests.bat'):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding='utf-8').lower()
                self.assertNotIn('rmdir /s', content)
                self.assertNotIn('remove-item', content)


if __name__ == '__main__':
    unittest.main()
