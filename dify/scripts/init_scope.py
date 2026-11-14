from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or return the stable Scope used by 甩锅法庭.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--idempotency-key", default="shuai-guo-court-v1")
    args = parser.parse_args()

    payload = {
        "title": "甩锅法庭",
        "summary": "Dify NPC judge fictional case Memory; player_id is a lookup key, not authorization",
        "idempotency_key": args.idempotency_key,
    }
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("POWERCONTEXT_API_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{args.base_url.rstrip('/')}/v1/scopes",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Scope initialization failed with HTTP {error.code}: {detail}") from error
    print(json.dumps({"scope_id": result["scope_id"], "title": payload["title"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
