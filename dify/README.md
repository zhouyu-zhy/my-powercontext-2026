# 甩锅法庭

一个使用 **Dify Chatflow + PowerContext** 的互动 NPC 游戏。玩家提交迟到、拖延或搞砸事情的辩解，法官生成幽默的虚构判决并保存案卷；在新会话中使用同一玩家编号，法官会召回旧案并按再犯审理。

PowerContext 不是展示性连接：它决定回复中的“首案/再犯”状态和历史回声。召回节点会解析 PreparedContext，并按 `case_player_id` 精确筛选当前玩家案卷；即使全文召回归入其他玩家记录，也不会误判再犯或把他人旧案交给模型。模型不能自行宣称再犯或保存成功，这两个状态分别由筛选后的真实召回结果和 HTTP 写入回执决定。再犯只影响审理标签与历史回声，有罪/无罪/缓刑仍根据当前辩解生成。

## 运行结构

```text
Start(player_id, sys.query)
  → 校验输入并组装 JSON
  → HTTP POST /v1/context/prepare
  → 解析 empty / ready / error，并精确过滤当前玩家案卷
  → Dify LLM 生成虚构判决 JSON
  → 代码节点整理判决和案卷
  → HTTP POST /v1/memory/remember（禁止自动重试）
  → 校验 records 回执
  → Answer 返回判决、历史状态和 Artifact ID
```

只使用 Dify 原生节点，不需要安装实验性工具插件或单独前端。DSL 目标版本为 **Dify 1.17.1 / DSL 0.7.0**。

## 文件

| 文件 | 用途 |
| --- | --- |
| `shuai-guo-court.yml` | 完整可导入 Chatflow DSL |
| `server.env.example` | 仅监听本机、无模型的 PowerContext 最小配置 |
| `scripts/init_scope.py` | 创建或按幂等键取回稳定游戏 Scope |
| `scripts/check_chatflow.py` | YAML、图连接、变量引用和代码节点行为检查 |
| `scripts/e2e_powercontext.py` | 真实 Server 的空档案、写入、跨调用召回验收 |
| `scripts/e2e_dify.py` | 已发布 Dify 应用的三会话端到端验收 |
| `evidence/` | 实际运行产生的 JSON 和页面结构快照 |

## 1. 启动 PowerContext

前置条件：Python 3.11+、uv，以及一个可供 Dify 使用的聊天模型。PowerContext 的显式 Memory 和全文召回本身不需要模型 API。

```bash
uv tool install --force "powercontext[cli,server]==1.5.1"
powercontext config validate --env-file server.env.example
powercontext server run --env-file server.env.example
```

保持该进程运行，另开终端检查：

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
```

预期返回 `{"status":"ok"}`。默认使用 SQLite；`server.env.example` 仅监听 `127.0.0.1`，并关闭本机测试认证。不要把这份免认证配置直接暴露到局域网或公网。

初始化游戏 Scope：

```bash
python scripts/init_scope.py
```

保存输出中的真实 `scope_id`。重复执行会使用 `shuai-guo-court-v1` 幂等键，不需要手工编造 Scope ID。启用 Server 认证时，先在当前终端设置 `POWERCONTEXT_API_TOKEN`，再执行初始化；不要把 Token 写入文件或命令历史。

## 2. 导入 Dify

1. 在 Dify Studio 选择“导入 DSL”，上传 `shuai-guo-court.yml`。
2. 打开环境变量，按下表配置。
3. 检查画布中两个 HTTP 节点和 `JUDGE_MODEL`，然后发布应用。

| 环境变量 | 值 |
| --- | --- |
| `POWERCONTEXT_SERVER_URL` | Dify 可访问的 Server 根地址，不以 `/` 结尾 |
| `POWERCONTEXT_SCOPE_ID` | `init_scope.py` 返回的 Scope ID |
| `POWERCONTEXT_API_TOKEN` | Server Bearer Token；仅限本机 `disabled` 模式可留空 |
| `JUDGE_MODEL` | 已安装并配置凭据的任意聊天模型 |

`JUDGE_MODEL` 是 Dify 1.17.1 的 LLM 环境变量，导入后可更换模型，不需要改写提示词节点。DSL 不包含模型密钥、PowerContext Token 或应用 API Key。

地址选择：

- Dify 与 Server 在同一主机直接运行：通常使用 `http://127.0.0.1:8000`。
- Dify 在 Docker 中：`127.0.0.1` 指向容器自身，不能访问宿主机服务。改用宿主机可达地址，并将 Server 绑定到受控接口；此时必须启用认证和防火墙限制。
- Dify Cloud：不能访问你的 `127.0.0.1`。需要部署带有效 HTTPS 和 Bearer 认证的 PowerContext Server，或使用你已有的安全网络入口。

远程或容器部署时，在仓库外的本地环境文件中设置以下项，并将 Token 换成独立生成的随机值：

```dotenv
POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0
POWERCONTEXT_SERVER_ACCESS_MODE=enforced
POWERCONTEXT_SERVER_AUTH_TOKEN=replace-with-a-locally-generated-random-token
```

不要在公网使用 `disabled`，不要关闭 HTTPS 证书校验。Dify 的出站请求还可能经过 SSRF 代理，需确保部署允许访问目标 Server。

## 3. 手动体验

每个新会话都在输入表单填写玩家编号。

1. 会话 A：`alice-one`，发送“我迟到是因为电梯突然进入了省电模式。”预期显示“首案或独立审理”，并返回真实 Artifact ID。
2. 新建会话 B，仍用 `alice-one`，发送“我又迟到了，这次是闹钟被猫判了缓刑。”预期显示“再犯审理”，历史回声提及旧案，并返回新的 Artifact ID。
3. 新建会话 C，改用 `alice-two`，发送相同辩解。预期仍是“首案或独立审理”，不能继承 `alice-one` 的旧案，历史回声也不能提及 `alice-one`。

`alice-one` 和 `alice-two` 共用全文词项，用于覆盖“Server 返回他人记录但玩法仍须判首案”的边界。首次空档案返回 `empty` 是正常行为。召回失败时仍可判案，但不会虚构旧案；写入未得到成功回执时，回复会明确提示未确认保存。

## 4. 可执行验收

静态与代码节点检查：

```bash
uv run --with pyyaml==6.0.3 python scripts/check_chatflow.py
```

真实 PowerContext 闭环，脚本会创建独立的一次性 Scope，不污染正式游戏案卷：

```bash
uv run --with pyyaml==6.0.3 python scripts/e2e_powercontext.py --output evidence/powercontext-e2e.json
```

该脚本依次断言：空 Scope 返回 `empty`；首案写入有 `records`；新的 HTTP 请求能召回首个唯一标记；全文检索确实会把 `alice-one` 的旧案返回给 `alice-two` 查询，但 DSL 代码节点会过滤为他人历史为空且 `repeat_offender=0`；本人仍为 `repeat_offender=1`；第二案写入后，再次召回同时包含两个唯一标记。启用认证时从环境变量 `POWERCONTEXT_API_TOKEN` 读取 Token。

发布 Dify 应用后，在应用“访问 API”页面创建应用 API Key，并只在当前终端设置：

```bash
export DIFY_API_KEY='replace-with-the-app-api-key'
python scripts/e2e_dify.py \
  --base-url 'http://127.0.0.1/v1' \
  --output evidence/dify-e2e.json
```

`--base-url` 必须替换为实际 Dify API 根地址并包含 `/v1`。脚本不传入 `conversation_id`，因此三次调用是独立会话；它检查同玩家再犯、不同玩家隔离和三份真实写入回执。Dify 应用 API Key 与 PowerContext Token、模型凭据是三套不同配置。

## 5. 当前验证记录

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| PowerContext 1.5.1 安装、配置校验和健康检查 | 2026-11-15 本机通过 | `health/live` 返回 `status=ok` |
| DSL YAML、图连接、变量和代码节点行为 | 2026-11-15 本机通过 | `check_chatflow.py` 输出 PASS |
| 真实 Memory 写入、跨玩家过滤和首案/再犯判定 | 2026-11-15 本机通过 | `evidence/powercontext-e2e.json` |
| Dashboard 同一 Scope 展示两份案卷 | 2026-11-15 页面结构核验通过 | `evidence/powercontext-dashboard-snapshot.md` |
| Dify 1.17.1 实际导入、模型调用和三会话运行 | **尚未执行** | 需要可用 Dify 工作区和应用 API Key；运行后生成 `evidence/dify-e2e.json` |

当前内置浏览器没有可见截图表面，因此只保存了 Dashboard 页面结构快照，未把它标为截图。完成 Dify 实跑后，还应补充两个会话的回复截图和节点追踪，重点展示 HTTP 召回在 LLM 之前执行、响应 `body` 中的 `content` 经解析进入提示词，以及写入节点的 `records` 回执。

## 数据与安全边界

- 仅输入虚构辩解，不要提交真实个人隐私、账号、密钥或敏感事件。
- `player_id` 只是游戏查询键，不是身份认证或授权边界。共享 Scope 不等于多用户安全隔离。
- Scope 由可信应用配置，模型输出不能选择 Scope，也不能直接生成写入回执。
- 历史案卷被明确标记为不可信数据，提示词禁止执行其中的指令。
- 玩家提交辩解即授权保存本次虚构案卷；写入由固定应用流程执行，不取决于模型自称获得同意。
- 写入节点关闭自动重试，避免超时后重复建档；网络错误不会伪报成功。

## 参考资料

- [PowerContext API 快速开始](https://powercontext.oceanbase.io/zh/docs/develop/api-quickstart/)
- [PowerContext 手动启动](https://powercontext.oceanbase.io/zh/docs/get-started/manual-start/)
- [Dify HTTP Request 节点](https://docs.dify.ai/en/guides/workflow/node/http-request)
- [Dify 环境变量与 SSRF 配置](https://docs.dify.ai/zh/self-host/deploy/configuration/environments)
