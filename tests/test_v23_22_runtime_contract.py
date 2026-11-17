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

    def test_user_scope_python_311_is_a_safe_launcher_fallback(self):
        fallback = r'%LocalAppData%\Programs\Python\Python311\python.exe'
        for name in ('setup_and_run.bat', 'try_voicepilot.bat'):
            with self.subTest(name=name):
                content = (ROOT / name).read_text(encoding='utf-8')
                self.assertIn('py -3.11 -c "import sys" >nul 2>&1', content)
                self.assertIn(fallback, content)
                self.assertIn('if not exist "' + fallback + '"', content)


if __name__ == '__main__':
    unittest.main()
