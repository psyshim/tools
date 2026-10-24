"""Exercise hostile JSON at actual HTTP boundaries, not only parser helpers."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from protocol.json_boundary import loads_object, read_limited_body
from coding_service.protocol_router import make_protocol_router_for
from coding_service.device_client import DeviceClient, DeviceUnavailable
from device_agent.server import create_app
from code_attach.router import make_router

BAD_JSON = [b"", b"{", b"{}{}", b"[]", b"null", b"\xff",
            b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}',
            b'{"x":1,"x":2}', b'{"x":{"y":1,"y":2}}',
            b'{"x":"\\ud800"}', b'{"\\ud800":1}',
            b'{"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}']


class BoundaryTests(unittest.TestCase):
    def test_actual_service_and_device_refuse_before_dispatch(self):
        engine, service = Mock(), Mock()
        app = FastAPI()
        app.include_router(make_protocol_router_for(engine, lambda _: "user", lambda *_: "workspace"))
        cfg = SimpleNamespace(allow_remote_clients=False, token_sha256=hashlib.sha256(b"token").hexdigest())
        device = create_app(cfg, service, trust_test_client=True)
        with TestClient(app) as a, TestClient(device) as b:
            for raw in BAD_JSON:
                with self.subTest(raw=raw[:40]):
                    self.assertEqual(a.post("/tasks", content=raw, headers={"x-protocol-version": "1.0"}).status_code, 400)
                    self.assertEqual(b.post("/v1/sessions", content=raw, headers={"authorization": "Bearer token"}).status_code, 400)
        engine.start.assert_not_called()
        service.open_session.assert_not_called()

    def test_prototype_ui_api_rejects_the_same_hostile_json(self):
        model = Mock()
        app = FastAPI()
        app.include_router(make_router(model, lambda _: "user", lambda *_: None, prototype=True))
        with TestClient(app) as client:
            for raw in BAD_JSON:
                with self.subTest(raw=raw[:40]):
                    self.assertEqual(client.post("/tasks", content=raw).status_code, 400)
        model.assert_not_called()

    def test_device_answers_are_strict_too(self):
        for raw in BAD_JSON:
            with self.subTest(raw=raw[:40]):
                response = Mock()
                response.status = 200
                response.read.return_value = raw
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                with patch("urllib.request.urlopen", return_value=response):
                    with self.assertRaises(DeviceUnavailable):
                        DeviceClient("http://localhost", "token").info()

    def test_duplicate_configuration_grants_are_refused(self):
        from device_agent.config import load_config as device_load, ConfigError as DeviceConfigError
        from coding_service.config import load_config as service_load, ConfigError as ServiceConfigError
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(b'{"allow_mutation":false,"allow_mutation":true}')
            for load, error in ((device_load, DeviceConfigError), (service_load, ServiceConfigError)):
                with self.assertRaises(error):
                    load(path)

    def test_brackets_quotes_unicode_and_valid_depth_are_preserved(self):
        value = {"text": '[{\\"日本語"}]', "nested": [{"a": 1.25}]}
        self.assertEqual(loads_object(json.dumps(value).encode()), value)
        with self.assertRaises(ValueError):
            loads_object('{"x":"日本語"}', max_bytes=12)


class ChunkedBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_stops_at_limit_without_consuming_rest(self):
        seen = []
        async def stream():
            for chunk in (b"123", b"456", b"never read"):
                seen.append(chunk)
                yield chunk
        request = SimpleNamespace(headers={}, stream=stream)
        with self.assertRaises(OverflowError):
            await read_limited_body(request, 5)
        self.assertEqual(seen, [b"123", b"456"])

    async def test_invalid_or_huge_content_length_is_rejected_before_read(self):
        for value, error in (("-1", ValueError), ("9" * 10000, ValueError), ("100", OverflowError)):
            request = SimpleNamespace(headers={"content-length": value}, stream=Mock())
            with self.assertRaises(error):
                await read_limited_body(request, 5)
            request.stream.assert_not_called()
