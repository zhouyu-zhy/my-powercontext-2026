from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def chat(base_url: str, api_key: str, player_id: str, query: str, user: str) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}/chat-messages",
        data=json.dumps(
            {
                "inputs": {"player_id": player_id},
                "query": query,
                "response_mode": "blocking",
                "user": user,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Dify chat-messages returned HTTP {error.code}: {detail}") from error


def compact(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": response.get("conversation_id"),
        "message_id": response.get("message_id"),
        "answer": response.get("answer"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run three independent Dify conversations against the published app API.")
    parser.add_argument("--base-url", default="http://127.0.0.1/v1", help="Dify API root including /v1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    api_key = os.environ["DIFY_API_KEY"]

    run_id = str(time.time_ns())
    player_one = f"alice-{run_id}-one"
    player_two = f"alice-{run_id}-two"

    first = chat(args.base_url, api_key, player_one, "我迟到是因为电梯突然进入了省电模式。", "reviewer-001")
    first_answer = first.get("answer") or ""
    assert "首案或独立审理" in first_answer, first_answer
    assert "案卷已写入 PowerContext" in first_answer, first_answer

    second = chat(args.base_url, api_key, player_one, "我又迟到了，这次是闹钟被猫判了缓刑。", "reviewer-001")
    second_answer = second.get("answer") or ""
    assert second.get("conversation_id") != first.get("conversation_id")
    assert "再犯审理" in second_answer, second_answer
    assert "案卷已写入 PowerContext" in second_answer, second_answer

    other = chat(args.base_url, api_key, player_two, "我又迟到了，这次是闹钟被猫判了缓刑。", "reviewer-001")
    other_answer = other.get("answer") or ""
    assert "首案或独立审理" in other_answer, other_answer
    assert player_one not in other_answer, other_answer
    assert "案卷已写入 PowerContext" in other_answer, other_answer

    evidence = {
        "result": "PASS",
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "player_ids": [player_one, player_two],
        "checks": {
            "first_player_starts_with_empty_history": True,
            "same_player_in_new_conversation_is_repeat": True,
            "different_player_does_not_inherit_history": True,
            "all_three_writes_return_real_receipts": True,
        },
        "runs": [compact(first), compact(second), compact(other)],
    }
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    print(rendered)


if __name__ == "__main__":
    main()
