"""v0.13 reduces false alarms without relaxing approvals for real risks."""
import threading
import unittest
from unittest.mock import Mock

from app.agent import VoiceAgent
from app.safety import RISK_WORDS


class SafetyPrecision(unittest.TestCase):
    def test_ok_is_a_word_not_a_substring_of_look(self):
        self.assertFalse(RISK_WORDS.search('Look at details'))
        self.assertFalse(RISK_WORDS.search('Book details'))
        self.assertTrue(RISK_WORDS.search('OK'))
        self.assertTrue(RISK_WORDS.search('Click OK'))
        self.assertTrue(RISK_WORDS.search('送信'))

    def test_regular_web_button_has_no_spurious_approval(self):
        agent = VoiceAgent(Mock(connected=False), Mock(), Mock(provider='ollama'),
                           lambda _: None, lambda _: True, False, threading.Event())
        for label in ('Look at details', 'Book details'):
            context = {'browser': {'elements': [{'id': 'b', 'type': 'button',
                                                'label': label, 'role': '',
                                                'in_form': False, 'in_dialog': False,
                                                'external_origin': ''}]}}
            self.assertFalse(agent._requires_confirmation('browser_click', {'id':'b'}, context))
        context['browser']['elements'][0]['label'] = 'OK'
        self.assertTrue(agent._requires_confirmation('browser_click', {'id':'b'}, context))

if __name__ == '__main__':
    unittest.main()