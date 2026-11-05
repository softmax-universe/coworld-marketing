"""Exercise real native client HTTP for known and model-overridden sampling contracts."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from marketing.judge import JudgeConfig, ModelClient


def test_native_sampling_contract_through_real_http(monkeypatch):
    received = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            received.append((body, self.headers.get("X-Coworld-Player-Slot")))
            reply = json.dumps({"choices": [{"message": {"content": "synthetic HTTP fixture only"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    monkeypatch.setenv("COWORLD_LLM_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    calls = []
    try:
        client = ModelClient(JudgeConfig(), on_call=calls.append)
        for model, temperature, expected in [
            ("anthropic/claude-opus-5.5", 0.7, False),
            ("anthropic/claude-opus-5.5", None, False),
            ("anthropic/claude-sonnet-4.6", 0.7, True),
            ("synthetic-other-model", 0.2, True),
        ]:
            client.model = model
            assert client.complete(
                [{"role": "user", "content": "synthetic only"}], slot=1, temperature=temperature
            ) == ("synthetic HTTP fixture only")
            body, slot = received[-1]
            assert body["model"] == model and body["max_tokens"] == client.cfg.max_tokens
            assert slot == "1" and ("temperature" in body) == expected
            if expected:
                assert body["temperature"] == temperature
            assert calls[-1]["request"] == body and calls[-1]["response_complete"]
            assert calls[-1]["response_reader_joined"] and calls[-1]["http_status"] == 200
        assert len(received) == len(calls) == 4 and client.supports_temperature
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
