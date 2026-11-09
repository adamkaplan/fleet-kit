#!/usr/bin/env python3
"""A scripted OpenAI-compatible provider for the permissions proof: no model is involved. Every chat request is answered with the NEXT shell
tool call from a JSON list of commands; when the list is used up it answers "done". Each call it serves is marked in the shared log
(`MOCK <n>`), so the spy plugin's evaluations and the shims' executions that follow can be attributed to it.
usage: mock_provider.py PORT COMMANDS.json LOG"""
import http.server
import json
import sys
import time

PORT, COMMANDS, LOG = int(sys.argv[1]), json.load(open(sys.argv[2])), sys.argv[3]
SERVED = {"n": 0}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "mock"}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        tools = [t["function"]["name"] for t in body.get("tools", [])]
        done = sum(1 for m in body.get("messages", []) if m.get("role") == "tool")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def send(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()
        base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "mock"}
        if "shell" in tools and done < len(COMMANDS) and SERVED["n"] == done:
            SERVED["n"] += 1
            with open(LOG, "a") as log:
                log.write("MOCK %d\n" % done)
            call = {"index": 0, "id": "call_%d" % done, "type": "function",
                    "function": {"name": "shell", "arguments": json.dumps({"command": COMMANDS[done], "description": "proof"})}}
            send(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]))
            send(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]))
        else:
            send(dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]))
            send(dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
