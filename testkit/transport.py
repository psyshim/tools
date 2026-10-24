"""One request interface, two implementations, so the same conformance scenarios run against the mock and the real service."""
import json

from protocol import error_envelope

class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

class MockTransport:
    """In-process: calls the executable mock directly."""
    name = "mock"

    def __init__(self, service):
        self.service = service

    def request(self, method, path, headers=None, json_body=None, raw=None):
        body = json_body
        if raw is not None:
            if len(raw) > 65536:
                return Response(413, error_envelope("PAYLOAD_TOO_LARGE", "request body too large"))
            try:
                body = json.loads(raw)
            except ValueError:
                return Response(400, error_envelope("BAD_REQUEST", "body is not valid JSON"))
        status, payload = self.service.handle(method, path, headers, body)
        return Response(status, payload)

class HttpTransport:
    """Wraps an httpx/Starlette-style client (for example fastapi.testclient.TestClient)."""
    name = "real"

    def __init__(self, client, prefix=""):
        self.client, self.prefix = client, prefix

    def request(self, method, path, headers=None, json_body=None, raw=None):
        kwargs = {"headers": dict(headers or {})}
        if raw is not None:
            kwargs["content"] = raw
            kwargs["headers"].setdefault("content-type", "application/json")
        elif json_body is not None:
            kwargs["json"] = json_body
        response = self.client.request(method, self.prefix + path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"_non_json": response.text[:200]}
        return Response(response.status_code, body)
