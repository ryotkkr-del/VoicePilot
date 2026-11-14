import unittest
from app.agent import validate_action, parse_json_text


class TestValidation(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_action({"action": "browser_scroll", "args": {"direction":"down"}})[0], "browser_scroll")

    def test_no_arbitrary_commands(self):
        for name in ("shell", "python_exec", "files_delete", "browser_eval"):
            with self.assertRaises(ValueError):
                validate_action({"action": name, "args": {"command":"hello"}})

    def test_no_javascript_urls(self):
        for url in ("javascript:alert(1)", "file:///C:/Windows", "https://user:pass@example.com/"):
            with self.assertRaises(ValueError):
                validate_action({"action": "browser_open", "args": {"url":url}})

    def test_no_extra_args(self):
        name, args, _ = validate_action({"action": "windows_launch", "args": {"app":"notepad", "cmd":"whoami"}})
        self.assertEqual(args, {"app":"notepad"})

    def test_json_fence(self):
        self.assertEqual(parse_json_text('```json\n{"action":"done","args":{}}\n```')["action"], "done")


if __name__ == '__main__':
    unittest.main()