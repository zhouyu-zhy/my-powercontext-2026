# 规有所依 · 工程上下文模块

这是一个用于规格驱动开发的 CLI。验证样例为会议室预订列表增加资源代号筛选，同时保留原有状态筛选、升序排序和输入不变性。样例工程、规格及数据均为本项目编写的演示材料。

## 安装

- Node.js 22 或更新版本。验收使用 Node.js 24.21.0。
- `npm ci` 安装 seekdb 本地运行时 `@oceanbase/seekdb-node@1.4.0` 和 MySQL 客户端 `mysql2@3.24.6`。seekdb 子进程使用随机本机 TCP 端口及约 2 GB 内存预算。
- PowerContext 1.5.1。进入 `minimax/`，在单独终端运行以下命令。

```powershell
uv tool install "powercontext[cli,server]==1.5.1"
powercontext server run --env-file powercontext.env.example
```

CLI 默认连接 `http://127.0.0.1:8791`。接已有服务时通过 `POWERCONTEXT_SERVER_URL` 和 `POWERCONTEXT_API_TOKEN` 配置。本地运行配置只绑定回环地址。以下命令在 `minimax/` 下执行。

## 使用与验收

```powershell
npm ci
node context.mjs import example/task.json example/catalog.json
node context.mjs prepare example/task.json
node verify.mjs
```

| 命令 | 输入 | 输出 |
| --- | --- | --- |
| `import` | 含任务 ID 和关键词的 JSON，以及目录清单 JSON | seekdb 导入条数 |
| `prepare` | 任务 JSON | 有效资料原文、路径、行范围、摘要、查询参数、已验证的检查点和下一步 |
| `save` | 任务 JSON 和检查点 JSON | PowerContext 的真实 Artifact 回执 |

初始资料来自 `example/catalog.json`。预期命中有效规格 `RES-12-v2`、`RES-02`，函数 `listReservations`、目标测试 `resource.test.mjs`、回归测试 `compatibility.test.mjs` 和测试数据 `fixture.mjs`。过时草案 `RES-12-v1` 与无关邮件规范 `NOTIFY-01` 不进入上下文。目标函数只接受字符串资源筛选，样例记录中的资源代号亦为字符串。

同一组功能与回归测试使用 `RESERVATION_VARIANT=before` 和 `RESERVATION_VARIANT=after` 分别选择快照。`node verify.mjs` 在独立的 seekdb 目录及 PowerContext Scope 中运行，按顺序断言：

1. 首次导入后取得六份指定来源，检查点为空。
2. 修改前三条目标断言失败，原有两项回归和省略筛选等价性通过。修改后六项全部通过。
3. 在独立进程里保存检查点，再启动 CLI，取回相同 Artifact ID，并使用已保存的下一步。
4. 重导包含 v3 的目录清单。新的上下文引用 v3 原文，旧的 v2 验证结果标为过时并停止复用。

普通调用的默认 seekdb 目录为 `.runtime/seekdb`。设置新的 `SDD_RUN` 可获得空白 PowerContext Scope，设置新的 `SDD_DATA_DIR` 可获得空白 seekdb；重新导入即可初始化。验收脚本为每次运行生成新的这两项配置，并把日志写到 `.runtime/verification.json`。

## 技术说明

`任务 JSON → seekdb 查询 → 带来源的上下文 → MiniMax 编辑候选 → 固定测试 → PowerContext 检查点`

清单包括来源路径、类型、适用主题及生效状态。导入在一笔事务中替换该任务索引。SQL 按任务、关键词和生效状态筛选，CLI 直接使用数据库返回的原文构造上下文。当前示例使用关键词精确匹配，需要在任务和目录中使用约定好的标签。

PowerContext 通过 HTTP API 写入 `/v1/memory/remember` 并从 `/v1/context/prepare` 取回状态。CLI 将来源列表及内容摘要计算成上下文 ID，仅在版本相同时消费检查点的下一步。选择新规格后保留旧回执标记，以便核对为何需要重测。

开发助手接入由 `agent.mjs` 负责。它把 `prepare` 结果转换为模型输入，并保存模型代码候选及来源记录，供人工比对后应用。业务测试运行不依赖在线模型返回相同文字。

## MiniMax 开发记录

当前分支的业务改动由 Codex 实现，尚缺 MiniMax 成功生成并应用代码的证据。实际尝试 `OpenCode 1.19.5 / opencode/minimax-m3.1-flash-free` 时服务返回 `Free usage limit reached`；Puter 的公开演示转入登录流程，MiniMax 独立演示同样要求登录。官方 API 需要可用的 `MINIMAX_API_KEY`。这些失败不计为 MiniMax 参与。

有可用 Key 后，可按以下方式把 seekdb 构造的上下文交给 MiniMax。默认使用中国区官方地址，也可通过 `MINIMAX_BASE_URL=https://api.minimax.io/v1` 选择国际地址。确认拟发送的内容、候选代码与测试结果后，再将该次真实开发记录和变更提交。

```powershell
node context.mjs prepare example/task.json > .runtime/context.json
node agent.mjs generate .runtime/context.json .runtime/minimax-receipt.json
node --check .runtime/minimax-candidate.mjs
```

本轮的实际 SQL、PowerContext 回执、重启复用与两组原始测试输出见 `evidence/verification.json`。修改前后源码及差异分别见 `example/before/`、`example/after/` 和 `evidence/reservations.diff`。

[题面与评审要求](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/qlqnoryzrs7gtqbw)
