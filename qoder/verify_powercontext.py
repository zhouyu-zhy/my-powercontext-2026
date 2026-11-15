from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json
import os

from server import PowerContext

os.environ["HANDOFF_SCOPE_KEY"] = f"research-handoff-acceptance-{uuid4()}"
notes = [
    {"project": "RERANK", "title": "重排模型 A/B 对照", "notes": "在固定的 120 条评估样本上，加入重排后 nDCG@10 从 0.62 到 0.70。样本只覆盖公开论文摘要。", "next_step": "补查重排的 P95 延迟，用中文论文分组复测。"},
    {"project": "RERANK", "title": "建立基准运行配置", "notes": "锁定随机种子 42，按主题分层抽样。基准配置重新跑了三次，结果一致。", "next_step": "把配置和评估数据版本写进实验报告。"},
    {"project": "ABLATION", "title": "移除查询扩展的消融", "notes": "查询扩展主要改善含缩写的问句。全量报告仍需按查询长度切分。", "next_step": "抽取 30 条缩写查询，标注查询扩展带来的误召回。"},
]
memory = PowerContext()
assert memory.recall({"query": "RERANK"}) == {"status": "empty", "items": []}
receipts = [memory.remember(note)["artifact"] for note in notes]
assert len({a["artifact_id"] for a in receipts}) == len(notes)
reader = PowerContext()
assert reader.scope == memory.scope
examples = []
for label, query, expected in [
    ("重排实验", "RERANK", receipts[:2]),
    ("消融实验", "ABLATION", receipts[2:]),
    ("无匹配示例", "zxcvbnopqrst", []),
]:
    result = reader.recall({"query": query})
    assert result["status"] == ("ready" if expected else "empty"), result
    assert {i["artifact"]["artifact_id"] for i in result["items"]} == {a["artifact_id"] for a in expected}, result
    examples.append({"label": label, "query": query, "result": result})
output = Path(__file__).resolve().parent / "site" / "demo.json"
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps({
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "powercontext_version": "1.5.1",
    "examples": examples,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print("PASS empty scope, three write receipts, independent search, and no-match search")
