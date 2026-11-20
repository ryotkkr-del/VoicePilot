"""Connection UX must give a safe, actionable pairing next step."""
import unittest

from app.main import PAIRING_GUIDANCE, chrome_connection_copy


class ChromeConnectionGuidanceTests(unittest.TestCase):
    def test_unpaired_state_explains_the_next_steps(self):
        title, guidance, color = chrome_connection_copy(connected=False, bridge_error=False)
        self.assertEqual(title, "○ Chrome未接続")
        self.assertIn("接続コードをコピー", guidance)
        self.assertIn("chrome://extensions", guidance)
        self.assertEqual(color, "#64748B")

    def test_connected_state_does_not_repeat_pairing_secret_guidance(self):
        title, guidance, color = chrome_connection_copy(connected=True, bridge_error=False)
        self.assertEqual(title, "● Chrome接続中")
        self.assertNotIn("接続コード", guidance)
        self.assertEqual(color, "#15803D")

    def test_bridge_error_is_actionable_without_exposing_raw_error(self):
        title, guidance, color = chrome_connection_copy(connected=False, bridge_error=True)
        self.assertEqual(title, "● Chrome接続エラー")
        self.assertIn("起動し直してください", guidance)
        self.assertEqual(color, "#B91C1C")
        self.assertNotIn("Exception", guidance)

    def test_guidance_is_short_enough_for_the_compact_panel(self):
        self.assertLessEqual(len(PAIRING_GUIDANCE), 140)


if __name__ == "__main__":
    unittest.main()
