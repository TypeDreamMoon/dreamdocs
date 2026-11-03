"""A translation API on 127.0.0.1 for the tests: chat completions, responses, messages and a model list.

It "translates" by prefixing 译 and keeps every placeholder, unless a scripted
fault says otherwise. Every request is recorded.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider:
    def __init__(self):
        self.requests: list[dict] = []
        self.faults: list[tuple] = []   # consumed one per POST, in order
        self.lock = threading.Lock()
        self.models: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"object": "list", "data": server.models})
                else:
                    self._send(404, {"error": "no such path"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with server.lock:
                    server.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    fault = server.faults.pop(0) if server.faults else ("ok",)
                if fault[0] == "status":
                    self._send(fault[1], {"error": {"message": f"scripted {fault[1]}"}}, fault[2] if len(fault) > 2 else None)
                    return
                api = "chat" if self.path.endswith("/chat/completions") else "responses" if self.path.endswith("/responses") else "messages"
                user = {"chat": lambda: body["messages"][1]["content"], "responses": lambda: body["input"],
                        "messages": lambda: body["messages"][0]["content"]}[api]()
                request = json.loads(user)
                answers = {}
                for slot in request["slots"]:
                    if "cells" in slot:
                        answers[slot["id"]] = [None if c is None else "译" + c for c in slot["cells"]]
                    else:
                        answers[slot["id"]] = "译" + slot["text"]
                if fault[0] == "drop":
                    answers = {k: re.sub(r"⟦1⟧", "", v, count=1) if isinstance(v, str) else v for k, v in answers.items()}
                if fault[0] == "omit":
                    answers.pop(next(iter(answers)))
                text = json.dumps({"slots": answers}, ensure_ascii=False)
                if fault[0] == "chatter":
                    text = "Here you go:\n```json\n" + text + "\n```\nAnything else?"
                truncated = fault[0] == "truncate"
                if api == "chat":
                    reply = {"choices": [{"message": {"role": "assistant", "content": text},
                                          "finish_reason": "length" if truncated else "stop"}],
                             "usage": {"prompt_tokens": len(user), "completion_tokens": len(text)}}
                elif api == "responses":
                    reply = {"status": "incomplete" if truncated else "completed",
                             "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                             "usage": {"input_tokens": len(user), "output_tokens": len(text)}}
                else:
                    reply = {"content": [{"type": "text", "text": text}],
                             "stop_reason": "max_tokens" if truncated else "end_turn",
                             "usage": {"input_tokens": len(user), "output_tokens": len(text)}}
                self._send(200, reply)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def sent_slots(self) -> list[dict]:
        out = []
        for r in self.requests:
            body = r["body"]
            user = body.get("input") or body["messages"][-1]["content"]
            out.extend(json.loads(user)["slots"])
        return out

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
