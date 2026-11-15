"""Per-process resource bounds, not authentication or a distributed rate limiter."""
import asyncio
import time
from collections import deque
from threading import Lock

from starlette.responses import JSONResponse


class RequestLimits:
    def __init__(self, app, max_body=512 * 1024, concurrency=2, per_minute=120):
        self.app = app
        self.max_body = max_body
        self.concurrency = concurrency
        self.per_minute = per_minute
        self.active = 0
        self.starts = deque()
        self.lock = Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] == "OPTIONS" or scope["path"] not in ("/simulate", "/dildoro/boss"):
            return await self.app(scope, receive, send)

        async def reject(code, message):
            await JSONResponse({"detail": message}, status_code=code,
                               headers={"Retry-After": "5"} if code == 429 else {})(scope, receive, send)

        # Reserve before receiving a body: slow clients cannot create unbounded buffers.
        with self.lock:
            now = time.monotonic()
            while self.starts and self.starts[0] <= now - 60:
                self.starts.popleft()
            busy = self.active >= self.concurrency or len(self.starts) >= self.per_minute
            if not busy:
                self.active += 1
                self.starts.append(now)
        if busy:
            return await reject(429, "계산 요청이 많습니다. 잠시 후 다시 시도하세요.")
        try:
            for name, value in scope.get("headers", []):
                if name.lower() == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        return await reject(400, "잘못된 요청 길이")
                    if length < 0 or length > self.max_body:
                        return await reject(413, "요청 크기는 최대 512 KiB입니다.")
            body = bytearray()

            async def collect():
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return "disconnect"
                    body.extend(message.get("body", b""))
                    if len(body) > self.max_body:
                        return "large"
                    if not message.get("more_body", False):
                        return "ok"
            try:
                outcome = await asyncio.wait_for(collect(), timeout=15)
            except asyncio.TimeoutError:
                return await reject(408, "요청 전송 시간 초과")
            if outcome == "disconnect":
                return
            if outcome == "large":
                return await reject(413, "요청 크기는 최대 512 KiB입니다.")
            delivered = False

            async def replay():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await self.app(scope, replay, send)
        finally:
            with self.lock:
                self.active -= 1
