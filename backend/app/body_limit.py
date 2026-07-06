"""Глобальный потолок размера тела запроса (ревью C-2).

Чистый ASGI-слой: отклоняет запрос 413-м по заголовку Content-Length до
чтения тела. Клиенты без Content-Length (chunked) проходят дальше — для
тяжёлых маршрутов ингеста есть вторая линия по фактическим байтам
(routes/chunks.py:_enforce_upload_cap); внешний слой — request_body в Caddy.
"""
import json


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name != b"content-length":
                    continue
                try:
                    length = int(value)
                except ValueError:
                    break  # мусорный заголовок — пусть падает глубже по стеку
                if length > self.max_bytes:
                    body = json.dumps({"detail": "Request body too large"}).encode()
                    await send({
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
                break
        await self.app(scope, receive, send)
