# 研务接续台

研务接续台面向需要反复回到同一课题的研究者。每次实验结束时，保存项目关键词、实验记录和下一步。下次打开工作台后，按项目关键词召回交接笔记，核对原始记忆 ID。

## 本地运行

需要 Python 3.11+、uv 和 PowerContext 1.5.1。在仓库根目录的 PowerShell 执行以下命令，保持进程运行。

```powershell
uv tool install "powercontext[cli,server]==1.5.1"
powercontext server run --env-file dify/server.env.example --port 8791
```

在第二个终端执行以下命令，然后打开 [本地工作台](http://127.0.0.1:8789/)。

```powershell
python qoder/server.py
```

填写一份笔记并保存。使用英文项目代号（例如 `RERANK`）作为关键词，可以在全文召回时直接匹配。刷新页面后检索相同代号，结果会显示写入时的 Artifact ID、实验记录和下一步。数据持久化在 PowerContext 默认的本地 SQLite 中。可用环境变量 `POWERCONTEXT_SERVER_URL`、`POWERCONTEXT_API_TOKEN` 和 `HANDOFF_SCOPE_KEY` 接入已有实例并选择工作区。

## 数据流

浏览器同源请求本地 Python 桥接器，桥接器创建或复用一个 PowerContext Scope。保存通过 `/v1/memory/remember`，界面在收到真实写入回执后显示 Artifact ID。检索通过 `/v1/context/prepare`，桥接器解析 PreparedContext 并返回笔记与引用。Scope 和服务凭据都由本地桥接器持有。

## 验证和演示

在 PowerContext 运行时执行 `python qoder/verify_powercontext.py`。脚本在新建的验收 Scope 中验证空结果、三条写入回执、新客户端召回及无匹配结果。它把真实检索响应导出到 `site/demo.json`，供静态站点在没有本地后端时回放。样例笔记及其中的实验指标均为虚构。

Qoder CLI CN 1.1.89 已登录。开发请求使用该账号列出的 `Qwen3.8-Flash`，服务返回 HTTP 403 并指向套餐页面。本提交的前端由 Codex 实现。Qoder Sites 的待发布目录为 `qoder/site`。
