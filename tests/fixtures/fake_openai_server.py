"""Deterministic stand-in OpenAI-compatible HTTP server for Phase 6C tests.

Listens on loopback only. Scenarios are selected by a keyword in the user
message content of the chat-completions JSON body — no outbound network.
"""
from __future__ import annotations

import json
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _chat_response(content: str, model: str = "fake-model") -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/1.0"
    protocol_version = "HTTP/1.0"
    _DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _safe_write(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            return True
        except self._DISCONNECT_ERRORS:
            return False

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except self._DISCONNECT_ERRORS:
            pass

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.end_headers()
            self._safe_write(b'{"error":{"message":"not found"}}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self._safe_write(b"not json")
            return
        messages = payload.get("messages") or []
        prompt = ""
        if messages and isinstance(messages[0], dict):
            prompt = str(messages[0].get("content") or "")

        auth = self.headers.get("Authorization", "")
        if "MISSING_AUTH" in prompt:
            self.send_response(401)
            self.end_headers()
            self._safe_write(json.dumps({"error": {"message": "invalid api key"}}).encode())
            return
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            self._safe_write(json.dumps({"error": {"message": "missing bearer token"}}).encode())
            return

        if "RATE_LIMIT" in prompt:
            self.send_response(429)
            self.end_headers()
            self._safe_write(json.dumps({"error": {"message": "rate limit exceeded"}}).encode())
            return
        if "SERVER_ERROR" in prompt:
            self.send_response(500)
            self.end_headers()
            self._safe_write(json.dumps({"error": {"message": "internal server error"}}).encode())
            return
        if "MALFORMED_BODY" in prompt:
            self.send_response(200)
            self.end_headers()
            self._safe_write(b"{not valid completion json")
            return
        if "SLOW_PAST_ATTEMPT_CAP" in prompt:
            # Deliberately > the per-attempt urlopen timeout that used to be
            # hardcoded to 5s in OpenAiCompatibleDirectRuntime
            # (direct_api_runtime.py), but comfortably within a 7s provider
            # deadline — see the regression test this feeds: a single
            # attempt must now be allowed the full remaining deadline
            # instead of being aborted at a flat 5s ceiling.
            time.sleep(5.1)
            body = json.dumps(_chat_response("slow but valid response")).encode()
            self.send_response(200)
            self.end_headers()
            self._safe_write(body)
            return
        if "SLOW" in prompt:
            time.sleep(3)
            body = json.dumps(_chat_response("slow but done")).encode()
            self.send_response(200)
            self.end_headers()
            self._safe_write(body)
            return
        if "SECRET_LEAK" in prompt:
            body = json.dumps(_chat_response("token sk-testsecret1234567890 leaked")).encode()
            self.send_response(200)
            self.end_headers()
            self._safe_write(body)
            return

        body = json.dumps(_chat_response(f"answer for: {prompt}")).encode()
        self.send_response(200)
        self.end_headers()
        self._safe_write(body)


class _QuietTLSServer(ThreadingHTTPServer):
    """A client that rejects our (deliberately untrusted) cert aborts the
    handshake; that's the scenario under test, not a server bug, so don't
    dump a traceback to stderr for it."""

    def handle_error(self, request, client_address) -> None:
        if issubclass(sys.exc_info()[0], (ssl.SSLError, OSError)):
            return
        super().handle_error(request, client_address)


class FakeOpenAiServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        certfile: str | None = None,
        keyfile: str | None = None,
    ):
        server_cls = _QuietTLSServer if certfile is not None else ThreadingHTTPServer
        self._httpd = server_cls((host, port), _Handler)
        self._scheme = "http"
        if certfile is not None:
            # Wrap the already-bound listening socket (standard idiom for
            # http.server + ssl): accept() then returns TLS-wrapped client
            # sockets, no per-connection wiring needed in the handler.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=certfile, keyfile=keyfile or certfile)
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
            self._scheme = "https"
        self.host = host
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-openai", daemon=True)

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self.host}:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()


def provider_document(base_url: str, name: str = "local", api_key_env: str = "TEST_OPENAI_API_KEY", **overrides) -> dict:
    entry = {
        "kind": "openai-compatible",
        "base_url": base_url,
        "api_key_env": api_key_env,
        "default_model": "fake-model",
        "request_timeout_seconds": 2,
        "allow_insecure_http": True,
        "capabilities": {"chat_completions": True},
    }
    entry.update(overrides)
    return {"providers": {name: entry}}


if __name__ == "__main__":
    server = FakeOpenAiServer()
    server.start()
    print(server.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
