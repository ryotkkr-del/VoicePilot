import base64
import unittest
from unittest.mock import patch

from app.planner import Planner
from app.actions import validate_action


class TestProviderPayloads(unittest.TestCase):
    @patch('app.planner.post_json')
    def test_ollama_vision_is_only_sent_when_present(self, post):
        post.return_value = {'response': '{"action":"done","args":{},"message":"ok"}'}
        planner = Planner('ollama')
        planner.next_action('こんにちは', {}, [], screenshot=b'test-jpeg')
        args = post.call_args.args
        self.assertEqual(args[1]['images'], [base64.b64encode(b'test-jpeg').decode()])
        self.assertEqual(post.call_args.kwargs['timeout'], 95)
        planner.next_action('こんにちは', {}, [])
        self.assertNotIn('images', post.call_args.args[1])

    @patch('app.planner.post_json')
    def test_gemini_image_uses_inline_data(self, post):
        post.return_value = {'candidates': [{'content': {'parts': [
            {'text': '{"action":"done","args":{},"message":"ok"}'}
        ]}}]}
        planner = Planner('gemini', gemini_key='not-a-real-key')
        result = planner.next_action('画像を見て', {}, [], screenshot=b'test-jpeg')
        self.assertEqual(result['action'], 'done')
        payload = post.call_args.args[1]
        parts = payload['contents'][0]['parts']
        self.assertEqual(parts[-1]['inline_data']['mime_type'], 'image/jpeg')
        self.assertEqual(parts[-1]['inline_data']['data'], base64.b64encode(b'test-jpeg').decode())

    def test_local_ip_restrictions(self):
        for url in ('http://[::1]:1234/', 'http://127.0.0.2/',
                    'http://192.168.1.1/', 'http://router.lan/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_action({'action':'browser_open','args':{'url':url}})


if __name__ == '__main__':
    unittest.main()