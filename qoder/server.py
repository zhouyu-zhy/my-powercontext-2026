"""Local HTTP bridge for the research handoff workspace."""

import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

SITE = Path(__file__).with_name("site")
FIELDS = {"project": 48, "title": 100, "notes": 650, "next_step": 240}
MARKER = "research-handoff-v1 "
BEGIN = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n"
END = "\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"


class PowerContext:
    def __init__(self):
        self.url = os.environ.get("POWERCONTEXT_SERVER_URL", "http://127.0.0.1:8791").rstrip("/")
        self.token = os.environ.get("POWERCONTEXT_API_TOKEN")
        self.scope = self.post("/v1/scopes", {
            "title": "研务接续台",
            "summary": "Research notes and next actions",
            "idempotency_key": os.environ.get("HANDOFF_SCOPE_KEY", "research-handoff-v1"),
        })["scope_id"]

    def post(self, path, payload):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(
            self.url + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def remember(self, body):
        note = {field: required_text(body, field, length) for field, length in FIELDS.items()}
        receipt = self.post("/v1/memory/remember", {
            "scope_id": self.scope,
            "kind": "research_handoff",
            "description": f"{note['project']} {note['title']}",
            "reason": "Researcher saved a handoff note",
            "text": MARKER + json.dumps(note, ensure_ascii=False),
        })
        artifact = receipt["records"][0]["artifact"]
        return {"artifact": artifact}

    def recall(self, body):
        query = required_text(body, "query", 160)
        prepared = self.post("/v1/context/prepare", {
            "scope_id": self.scope,
            "query": query,
            "max_bytes": 24000,
        })
        status = prepared["status"]
        if status == "empty":
            return {"status": status, "items": []}
        if status != "ready":
            raise ValueError(f"Unexpected PowerContext status: {status}")
        _, opening, block = prepared["content"].partition(BEGIN)
        block, closing, _ = block.partition(END)
        if not opening or not closing:
            raise ValueError("PowerContext returned an incomplete envelope")
        items = []
        for item in json.loads(block)["items"]:
            content = item.get("content", "")
            if item.get("truncated") or not content.startswith(MARKER):
                continue
            note = json.loads(content.removeprefix(MARKER))
            items.append({**note, "artifact": item["citation"]["artifact_ref"]})
        return {"status": status, "items": items}


class InvalidInput(ValueError):
    pass


def required_text(body, field, length):
    value = body.get(field) if isinstance(body, dict) else None
    if not isinstance(value, str) or not 0 < len(value.strip()) <= length:
        raise InvalidInput(f"{field} 应包含 1 至 {length} 个字符。")
    return value.strip()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, memory, **kwargs):
        self.memory = memory
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self):
        if self.path == "/api/mode":
            self.reply(200, {"mode": "live"})
            return
        super().do_GET()

    def do_POST(self):
        endpoints = {"/api/notes": self.memory.remember, "/api/recall": self.memory.recall}
        operation = endpoints.get(self.path)
        if operation is None:
            self.reply(404, {"error": "接口不存在。"})
            return
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get("Host")
        if host not in allowed_hosts or self.headers.get("Origin", f"http://{host}") != f"http://{host}":
            self.reply(403, {"error": "请从本机工作台访问。"})
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "接口需要 JSON 请求。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16000:
                raise ValueError("Invalid body size")
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self.reply(400, {"error": "请求内容无效。"})
            return
        try:
            result = operation(body)
        except InvalidInput as error:
            self.reply(400, {"error": str(error)})
            return
        except (URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as error:
            self.log_error("PowerContext request failed (%s)", type(error).__name__)
            self.reply(502, {"error": "PowerContext 未确认请求，请检查服务后重试。"})
            return
        self.reply(201 if self.path == "/api/notes" else 200, result)

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.environ.get("HANDOFF_PORT", "8789"))
    with ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, memory=PowerContext())) as server:
        print(f"http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
