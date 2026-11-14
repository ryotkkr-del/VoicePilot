"""Authenticated loopback-only WebSocket connection to the Chrome extension."""
from __future__ import annotations

import asyncio
import json
import threading
import re
import secrets
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any


class BrowserBridge:
    def __init__(self, token: str, port: int = 8765):
        self.token = token
        self.port = port
        self._loop = None
        self._thread = None
        self._socket = None
        self._pending: dict[str, asyncio.Future] = {}
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._shutdown = None
        self.error = ""

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._serve, daemon=True, name="voicepilot-browser-bridge")
        self._thread.start()
        self._ready.wait(5)

    def _serve(self) -> None:
        try:
            from websockets.asyncio.server import serve
            from websockets.exceptions import ConnectionClosed

            async def handler(ws):
                origin = ws.request.headers.get("Origin", "")
                # Requests from ordinary websites and other local processes are rejected.
                if not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                    await ws.close(code=1008, reason="Chrome extension only")
                    return
                try:
                    greeting = json.loads(await asyncio.wait_for(ws.recv(), 8))
                    if greeting.get("type") != "hello" or not isinstance(greeting.get("token"), str) or not secrets.compare_digest(greeting["token"], self.token):
                        await ws.close(code=1008, reason="Pairing required")
                        return
                except (asyncio.TimeoutError, ValueError, TypeError, ConnectionClosed):
                    await ws.close(code=1008, reason="Invalid handshake")
                    return

                # A reconnect invalidates EVERY in-flight command from the
                # previous socket. Never accept a stale receipt from another
                # connection as evidence of a successful click.
                if self._socket is not None:
                    previous = self._socket
                    self._socket = None
                    self._connected.clear()
                    for waiter in self._pending.values():
                        if not waiter.done():
                            waiter.set_exception(ConnectionError("Chromeが再接続しました。前の操作結果は未確認です"))
                    self._pending.clear()
                    await previous.close(code=1000, reason="Replaced")
                self._socket = ws
                self._connected.set()
                try:
                    await ws.send(json.dumps({"type": "ready"}))
                    async for raw in ws:
                        if raw == "ping":
                            await ws.send("pong")
                            continue
                        try:
                            data = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if data.get("type") == "response" and self._socket is ws:
                            waiter = self._pending.pop(data.get("id"), None)
                            if waiter and not waiter.done():
                                waiter.set_result(data)
                finally:
                    if self._socket is ws:
                        self._socket = None
                        self._connected.clear()
                        for waiter in self._pending.values():
                            if not waiter.done():
                                waiter.set_exception(ConnectionError("Chrome disconnected"))
                        self._pending.clear()

            async def forever():
                async with serve(handler, "127.0.0.1", self.port, max_size=2_000_000):
                    self._shutdown = asyncio.Event()
                    self._ready.set()
                    await self._shutdown.wait()

            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(forever())
        except Exception as exc:
            self.error = str(exc)
            self._ready.set()
        finally:
            self._connected.clear()
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()

    async def _send(self, action: str, args: dict) -> Any:
        if not self._connected.is_set() or self._socket is None:
            raise ConnectionError("Chrome拡張機能が未接続です。ペアリングコードを設定してください。")
        req_id = str(uuid.uuid4())
        waiter = asyncio.get_running_loop().create_future()
        self._pending[req_id] = waiter
        try:
            await self._socket.send(json.dumps({
                "type": "request", "id": req_id, "action": action, "args": args,
            }, ensure_ascii=False))
            response = await asyncio.wait_for(waiter, 12)
            if not response.get("ok"):
                raise RuntimeError(str(response.get("error", "Chrome操作に失敗しました")))
            return response.get("result", {})
        finally:
            self._pending.pop(req_id, None)

    def call(self, action: str, args: dict | None = None, timeout: float = 15) -> Any:
        if not self._loop or not self.connected:
            raise ConnectionError("Chrome拡張機能が未接続です。")
        task: Future = asyncio.run_coroutine_threadsafe(self._send(action, args or {}), self._loop)
        try:
            return task.result(timeout=timeout)
        except FutureTimeoutError as exc:
            # Stop retaining a pending request when Chrome never responds.
            # The remote side may have already applied the action: callers
            # must NOT assume that retrying it is harmless.
            task.cancel()
            raise TimeoutError("Chromeの応答がありません。操作済みの可能性があるため画面を確認してください") from exc

    def close(self) -> None:
        """Release the loopback port and outstanding extension requests."""
        if self._thread is None or not self._thread.is_alive():
            return
        if self._loop is None or self._shutdown is None or self._loop.is_closed():
            return

        async def shutdown():
            if self._socket is not None:
                await self._socket.close(code=1001, reason="VoicePilot closed")
            self._shutdown.set()

        try:
            task = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
            task.result(timeout=3)
            self._thread.join(timeout=3)
        except (RuntimeError, TimeoutError):
            # Tk shutdown must remain responsive even if Chrome is unresponsive.
            pass