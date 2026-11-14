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


def post(base_url: str, path: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {error.code}: {detail}") from error


def case_text(player_id: str, excuse: str, marker: str, recall_status: str) -> str:
    return (
        "甩锅法庭虚构案卷 | "
        f"case_player_id={player_id} | 玩家编号: {player_id} | "
        f"当前辩解: {excuse} | 判决: 有罪 | 罪名: 想象力超载罪 | "
        f"虚构处罚: 给电梯写一封道歉信 | 验收标记: {marker} | "
        f"本次召回状态: {recall_status}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate real PowerContext Memory recall across independent HTTP calls.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.environ.get("POWERCONTEXT_API_TOKEN", ""))
    parser.add_argument("--scope-id", default="")
    parser.add_argument("--player-id", default="player001")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    scope_id = args.scope_id
    if not scope_id:
        scope = post(
            args.base_url,
            "/v1/scopes",
            {
                "title": "甩锅法庭端到端验收",
                "summary": "Disposable Scope for explicit Memory and cross-call PreparedContext validation",
                "idempotency_key": f"shuai-guo-court-e2e-{time.time_ns()}",
            },
            args.token,
        )
        scope_id = scope["scope_id"]

    marker_one = f"case-{time.time_ns()}-one"
    marker_two = f"case-{time.time_ns()}-two"
    initial = post(
        args.base_url,
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": args.player_id, "max_bytes": 6000},
        args.token,
    )
    assert initial.get("status") == "empty", initial
    assert initial.get("content") is None, initial

    first = post(
        args.base_url,
        "/v1/memory/remember",
        {
            "scope_id": scope_id,
            "kind": "court_case",
            "text": case_text(args.player_id, "电梯突然进入省电模式", marker_one, "empty"),
            "reason": "玩家提交辩解，授权保存本次虚构案卷",
            "description": f"甩锅法庭玩家 {args.player_id} 的首案",
        },
        args.token,
    )
    assert first.get("records"), first

    recalled = post(
        args.base_url,
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": args.player_id, "max_bytes": 6000},
        args.token,
    )
    assert recalled.get("status") == "ready", recalled
    assert marker_one in (recalled.get("content") or ""), recalled

    second = post(
        args.base_url,
        "/v1/memory/remember",
        {
            "scope_id": scope_id,
            "kind": "court_case",
            "text": case_text(args.player_id, "闹钟被猫判了缓刑", marker_two, "ready"),
            "reason": "玩家再次提交辩解，授权保存本次虚构案卷",
            "description": f"甩锅法庭玩家 {args.player_id} 的再案",
        },
        args.token,
    )
    assert second.get("records"), second

    recalled_again = post(
        args.base_url,
        "/v1/context/prepare",
        {"scope_id": scope_id, "query": args.player_id, "max_bytes": 6000},
        args.token,
    )
    content = recalled_again.get("content") or ""
    assert recalled_again.get("status") == "ready", recalled_again
    assert marker_one in content and marker_two in content, recalled_again

    evidence = {
        "result": "PASS",
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "scope_id": scope_id,
        "player_id": args.player_id,
        "checks": {
            "empty_scope_returns_empty": True,
            "first_memory_write_returns_records": True,
            "independent_recall_contains_first_marker": True,
            "second_memory_write_returns_records": True,
            "later_recall_contains_both_markers": True,
        },
        "markers": {"first": marker_one, "second": marker_two},
        "memory_artifacts": [
            record["artifact"] for record in [first["records"][0], second["records"][0]]
        ],
        "recall_statuses": [initial["status"], recalled["status"], recalled_again["status"]],
        "final_content": content,
    }
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
