"""Loopback-only deterministic model fixture for native product integration tests.

This does not test a real LLM. It exercises product HTTP/streaming, plugin hooks,
and PMT context delivery without reading credentials or calling a remote API.
Official protocols: developers.openai.com/api/reference/resources/responses/streaming-events
and platform.claude.com/docs/en/build-with-claude/streaming.
Only request counts and sentinel-presence booleans are persisted.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sentinel", required=True)
    parser.add_argument("--stats", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    stats = {"fixture": "deterministic_loopback_model", "real_llm": False, "requests": []}
    lock = threading.Lock()
    args.stats.parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def json_response(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.json_response({"object": "list", "data": [{"id": "pmt-native-fixture", "object": "model"}]})

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 8 * 1024 * 1024:
                self.send_error(413)
                return
            raw = self.rfile.read(size)
            try:
                request = json.loads(raw)
            except ValueError:
                self.send_error(400)
                return
            if "count_tokens" in self.path:
                self.json_response({"input_tokens": 1})
                return
            context_seen = args.sentinel in raw.decode("utf-8", errors="replace")
            with lock:
                stats["requests"].append({"route": self.path.split("?")[0], "context_seen": context_seen})
                args.stats.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
            text = "PMT_NATIVE_CONTEXT_OK" if context_seen else "PMT_NATIVE_CONTEXT_MISSING"
            message_id = "msg_" + uuid.uuid4().hex
            if "messages" in self.path:
                events = [
                    ("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message",
                        "role": "assistant", "model": request.get("model", "pmt-native-fixture"), "content": [],
                        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
            else:
                response_id = "resp_" + uuid.uuid4().hex
                part = {"type": "output_text", "text": text, "annotations": []}
                item = {"type": "message", "id": message_id, "role": "assistant", "status": "completed", "content": [part]}
                response = {"id": response_id, "object": "response", "created_at": int(time.time()), "status": "completed",
                            "error": None, "incomplete_details": None, "output": [item],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
                if not request.get("stream", False):
                    self.json_response(response)
                    return
                events = [
                    ("response.created", {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}),
                    ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
                    ("response.content_part.added", {"type": "response.content_part.added", "item_id": message_id, "output_index": 0, "content_index": 0, "part": {**part, "text": ""}}),
                    ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": message_id, "output_index": 0, "content_index": 0, "delta": text}),
                    ("response.output_text.done", {"type": "response.output_text.done", "item_id": message_id, "output_index": 0, "content_index": 0, "text": text}),
                    ("response.content_part.done", {"type": "response.content_part.done", "item_id": message_id, "output_index": 0, "content_index": 0, "part": part}),
                    ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
                    ("response.completed", {"type": "response.completed", "response": response}),
                ]
            encoded = []
            for index, (name, value) in enumerate(events):
                value.setdefault("sequence_number", index)
                encoded.append(f"event: {name}\ndata: {json.dumps(value)}\n\n")
            body = "".join(encoded).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(json.dumps({"port": server.server_port, "host": "127.0.0.1", "real_llm": False}), flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
