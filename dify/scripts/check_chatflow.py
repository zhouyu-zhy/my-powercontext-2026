from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DSL_PATH = ROOT / "shuai-guo-court.yml"


def load_main(node: dict):
    namespace: dict = {}
    exec(node["data"]["code"], namespace)
    return namespace["main"]


def main() -> None:
    data = yaml.safe_load(DSL_PATH.read_text(encoding="utf-8"))
    assert data["kind"] == "app"
    assert data["app"]["mode"] == "advanced-chat"
    assert data["version"] == "0.7.0"

    workflow = data["workflow"]
    graph = workflow["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert len(nodes) == len(graph["nodes"])
    assert graph["edges"]

    for edge in graph["edges"]:
        assert edge["source"] in nodes, edge
        assert edge["target"] in nodes, edge

    outgoing = {edge["source"] for edge in graph["edges"]}
    incoming = {edge["target"] for edge in graph["edges"]}
    assert "1000000000001" not in incoming
    assert {"1000000000010", "1000000000011"}.isdisjoint(outgoing)

    environment = {item["name"]: item for item in workflow["environment_variables"]}
    assert environment["POWERCONTEXT_API_TOKEN"]["value_type"] == "secret"
    assert environment["POWERCONTEXT_API_TOKEN"]["value"] == ""
    assert environment["JUDGE_MODEL"]["value_type"] == "llm"

    for node in nodes.values():
        for match in re.finditer(r"\{\{#([^.{}#]+)\.", yaml.safe_dump(node, allow_unicode=True)):
            prefix = match.group(1)
            assert prefix in nodes or prefix in {"env", "sys", "conversation"}, (node["id"], prefix)
        if node["data"]["type"] == "code":
            for variable in node["data"]["variables"]:
                selector = variable["value_selector"]
                assert selector[0] in nodes or selector[0] in {"env", "sys", "conversation"}, selector

    remember = nodes["1000000000008"]["data"]
    assert remember["retry_config"]["retry_enabled"] is False
    assert remember["url"].endswith("/v1/memory/remember")
    assert nodes["1000000000004"]["data"]["url"].endswith("/v1/context/prepare")
    assert nodes["1000000000006"]["data"]["model_selector"] == ["env", "JUDGE_MODEL"]

    prepare = load_main(nodes["1000000000002"])
    valid = prepare("player001", "电梯进入了省电模式", "scope-test")
    assert valid["ok"] == 1
    assert json.loads(valid["recall_body"])["query"] == "player001"
    assert prepare("bad player!", "辩解", "scope-test")["ok"] == 0
    assert prepare("player001", "辩解", "")["ok"] == 0

    parse_recall = load_main(nodes["1000000000005"])
    ready = parse_recall(200, json.dumps({"status": "ready", "content": "case_player_id=player001 旧案"}))
    assert ready["history_status"] == "ready"
    assert "player001" in ready["history_text"]
    assert parse_recall(200, json.dumps({"status": "empty", "content": None}))["history_status"] == "empty"
    assert parse_recall(0, "")["history_status"] == "error"

    curate = load_main(nodes["1000000000007"])
    model_json = json.dumps(
        {
            "verdict": "有罪",
            "charge": "电梯玄学采信罪",
            "verdict_text": "本庭认为电梯不会自己省电，玩家的想象力倒是一直满格。",
            "penalty": "给电梯写一封道歉信。",
            "history_echo": "上次的理由也很离谱。",
            "case_summary": "玩家把迟到归因于电梯省电。",
        },
        ensure_ascii=False,
    )
    first = curate(model_json, "电梯省电", "player001", "scope-test", "empty", "")
    assert first["repeat_offender"] == 0
    assert "首案" in first["history_echo"]
    repeat = curate(model_json, "电梯省电", "player001", "scope-test", "ready", "真实旧案")
    assert repeat["repeat_offender"] == 1
    saved_body = json.loads(repeat["save_body"])
    assert saved_body["kind"] == "court_case"
    assert "case_player_id=player001" in saved_body["text"]
    fallback = curate("不是 JSON", "辩解", "player001", "scope-test", "empty", "")
    assert fallback["parse_ok"] == 0

    finalize = load_main(nodes["1000000000009"])
    receipt = json.dumps({"records": [{"artifact": {"artifact_id": "memory-123"}}]})
    args = ["player001", "有罪", "罪名", "判决书", "处罚", "旧案回声", 1, "已找到旧案"]
    success = finalize(200, receipt, *args)
    assert success["saved"] == 1
    assert "memory-123" in success["answer"]
    failed = finalize(0, "", *args)
    assert failed["saved"] == 0
    assert "不会声称保存成功" in failed["answer"]

    print("PASS: YAML、图连接、变量引用、无密钥配置和 14 项节点行为检查")
    print(f"DSL: {DSL_PATH}")


if __name__ == "__main__":
    main()
