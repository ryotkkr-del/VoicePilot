import asyncio
import threading
import time
import unittest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from app.bridge import BrowserBridge


class TestBridge(unittest.TestCase):
    def test_authenticated_request_response_and_invalid_token(self):
        bridge = BrowserBridge('test-secret', port=18761)
        bridge.start()
        self.addCleanup(bridge.close)
        self.assertFalse(bridge.error, bridge.error)

        async def fake_extension():
            url = 'ws://127.0.0.1:18761/ws'
            async with connect(url, origin='chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') as ws:
                await ws.send('{"type":"hello","token":"wrong"}')
                with self.assertRaises(ConnectionClosed):
                    await ws.recv()
            async with connect(url, origin='chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa') as ws:
                await ws.send('{"type":"hello","token":"test-secret"}')
                ready = await ws.recv()
                self.assertIn('ready', ready)
                result = await ws.recv()
                self.assertIn('snapshot', result)
                import json
                data = json.loads(result)
                await ws.send(json.dumps({'type':'response','id':data['id'], 'ok':True,'result':{'title':'example'}}))

        thread = threading.Thread(target=lambda: asyncio.run(fake_extension()), daemon=True)
        thread.start()
        for _ in range(50):
            if bridge.connected:
                break
            time.sleep(.05)
        result = bridge.call('snapshot')
        self.assertEqual(result['title'], 'example')
        thread.join(timeout=3)

if __name__ == '__main__':
    unittest.main()
