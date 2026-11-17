"""The loopback bridge must fail visibly if Windows cannot start it."""
import unittest
from unittest.mock import Mock, patch

from app.bridge import BrowserBridge


class BridgeStartupTests(unittest.TestCase):
    def test_startup_timeout_is_a_visible_fail_closed_error(self):
        bridge = BrowserBridge('test-secret')
        bridge._ready = Mock()
        bridge._ready.wait.return_value = False
        fake_thread = Mock()
        with patch('app.bridge.threading.Thread', return_value=fake_thread):
            bridge.start()
        self.assertEqual(bridge.error, 'Browser bridge did not start within 5 seconds')
        self.assertFalse(bridge.connected)
        fake_thread.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
