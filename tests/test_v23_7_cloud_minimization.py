"""P0-3: cloud input minimization without changing local safety observations."""
from __future__ import annotations

import copy
import threading
import unittest
from unittest.mock import Mock

from app.agent import VoiceAgent
from app.compact import compact_for_model


SECRET = "FAKE_PRIVATE_PREVIOUS_INSTRUCTION_API_KEY_123"


def observation():
    return {
        "recent_instructions": ["昨日の質問 " + SECRET],
        "recent_files": ["C:/Users/test/Documents/" + SECRET + ".txt"],
        "chrome_connected": True,
        "browser": {
            "title": "Current public page",
            "url": "https://example.org/",
            "document_ref": "42:token",
            "tabs": [
                {"title": "Current public page", "id": 42},
                {"title": "Private email subject " + SECRET, "id": 77},
            ],
            "elements": [{"id": "search", "label": "検索", "in_view": True}],
        },
        "windows": {
            "window_ref": "101",
            "active_title": "Public Browser",
            "windows": [{"title": "Secret background file " + SECRET}],
            "controls": [{"id": "0", "name": "検索", "type": "Edit"}],
        },
        "task_memory": {
            "recent_steps": [{"target": "Sensitive previous site " + SECRET}],
            "last_target": {"label": SECRET},
            "resolved_reference": {"source": "browser", "id": "search", "label": "検索"},
        },
    }


class CompactCloudMinimizationTests(unittest.TestCase):
    def test_cloud_excludes_old_instructions_file_paths_and_background_titles(self):
        original = observation()
        snapshot = copy.deepcopy(original)
        result = compact_for_model(original, "現在のページを下へスクロールして", cloud=True)
        self.assertNotIn(SECRET, str(result))
        self.assertNotIn("recent_instructions", result)
        self.assertNotIn("recent_files", result)
        self.assertNotIn("tabs", result["browser"])
        self.assertNotIn("windows", result["windows"])
        self.assertNotIn("recent_steps", result["task_memory"])
        self.assertNotIn("last_target", result["task_memory"])
        self.assertEqual(result["task_memory"]["resolved_reference"]["id"], "search")
        self.assertEqual(original, snapshot, "Safety validation must retain full local context")

    def test_local_ollama_still_retains_context_for_followups(self):
        result = compact_for_model(observation(), "同じことをやって", cloud=False)
        self.assertIn(SECRET, str(result))
        self.assertIn("recent_instructions", result)
        self.assertIn("tabs", result["browser"])
        self.assertIn("windows", result["windows"])

    def test_explicit_tab_and_window_switch_can_use_current_names(self):
        browser = compact_for_model(observation(), "別のタブへ切り替えて", cloud=True)
        self.assertIn("tabs", browser["browser"])
        self.assertNotIn("recent_instructions", browser)
        windows = compact_for_model(observation(), "ウィンドウを切り替えて", cloud=True)
        self.assertIn("windows", windows["windows"])
        self.assertNotIn("recent_files", windows)

    def test_cloud_agent_passes_only_filtered_context_to_planner(self):
        planner = Mock(provider="gemini")
        planner.next_action.return_value = {
            "action": "done", "args": {}, "message": "Cannot proceed"
        }
        agent = VoiceAgent(
            Mock(connected=False), Mock(), planner, Mock(), Mock(return_value=False),
            True, threading.Event()
        )
        agent._context = Mock(return_value=observation())
        outcome = agent.run("この画面を確認してください")
        self.assertEqual(outcome.status, "incomplete")
        model_context = planner.next_action.call_args.args[1]
        self.assertNotIn(SECRET, str(model_context))
        self.assertIn("browser", model_context)


if __name__ == "__main__":
    unittest.main()
