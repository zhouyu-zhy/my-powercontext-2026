# 栖光 · 本地知识库

端侧 AI 推理性能优化与 seekdb 混合检索作品。文档和查询都在浏览器 Web Worker 中完成向量化，Node.js 只接收文本、ID、元数据和已经计算好的向量；全文与向量结果由 seekdb 内部通过 `DBMS_HYBRID_SEARCH` 和 RRF 统一排序，不在浏览器拼接两路结果。

## 功能

- 导入 TXT、MD、JSON，或内置 24 篇可复现实验语料。
- `Xenova/all-MiniLM-L6-v2` 浏览器本地推理，384 维均值池化、L2 归一化向量。
- FP32 / INT8、batch 1 / 4 / 8 / 16 可选。
- WebGPU 优先；未提供 WebGPU 或 GPU 初始化失败时自动降级 WASM，也可强制指定后端。
- seekdb HNSW 向量索引、IK 全文索引、RRF 混合排序，展示完整原文、ID、元数据、分数和分段耗时。
- 增量导入、多个独立知识集合、真实 FP32 / INT8 质量与性能实验、原始 JSON 下载。

## 运行

要求 Node.js 22 或更高版本。已在 Windows 11、Node.js 24、Chrome 157 验证；本地 seekdb 默认需要约 2 GB 内存预算。

```bash
npm run setup
npm run dev
```

打开 `http://127.0.0.1:5173`。`npm run setup` 安装依赖、准备本地 ONNX Runtime 资源，并下载固定 revision 的模型；模型下载默认使用 `HF_ENDPOINT=https://hf-mirror.com`，可通过环境变量覆盖。应用运行阶段不需要云端推理 API 或密钥。

- `http://127.0.0.1:5173/?backend=auto`：WebGPU 优先，失败降级 WASM。
- `http://127.0.0.1:5173/?backend=webgpu`：强制 WebGPU。
- `http://127.0.0.1:5173/?backend=wasm`：强制 WASM。

生产构建：

```bash
npm run build
NODE_ENV=production npm run dev
```

Windows PowerShell 设置环境变量可使用 `$env:NODE_ENV="production"; npm run dev`。服务只监听 `127.0.0.1`；本地 seekdb 使用空密码，不要将服务或数据库端口暴露到不受信任的网络。

## 架构与关键实现

```text
TXT / MD / JSON
      ↓
浏览器 Web Worker：Tokenizer + ONNX Runtime Web
      ↓ 384 维向量、原文、稳定 ID、元数据
Node.js 本地 API（不执行向量化）
      ↓
seekdb 1.4.0：HNSW + FULLTEXT + DBMS_HYBRID_SEARCH + RRF
      ↓
排序结果、RRF 分数、完整原文
```

- `@oceanbase/seekdb-node@1.4.0` 管理独立 seekdb 进程，Windows 使用随机 TCP 端口。
- `seekdb@1.4.0` 执行 `collection.hybridSearch()`，RRF `rankWindowSize=60`、`rankConstant=60`。
- 每次写入后调用 `refresh_index()`；数据库操作在本地服务内串行执行，避免连接级会话状态互相影响。
- 列举集合时使用 `listCollections({ withEmbeddingFunction: false })`，不加载服务端 embedding provider。
- Transformers.js 4.3.2 的 pipeline registry 会漏掉浏览器本地 HTTP 模型的分词器文件，因此 Worker 显式加载 `BertTokenizer` 与 `AutoModel`，再执行相同的 mean pooling 和归一化。
- Vite 不监视 `data`、模型、运行时和证据目录，避免 Windows 对 seekdb 原生可执行文件的监视锁错误。

## 模型文件

固定 revision：`751bff37182d3f1213fa05d7196b954e230abad9`。

统一口径：每个精度总大小 = 相同的共享配置与分词文件 + 对应 ONNX 权重。共享文件为 944,310 字节，不在同一精度内重复计算。

| 精度 | ONNX 文件 | ONNX 字节数 | 同口径总字节数 |
|---|---|---:|---:|
| FP32 | `onnx/model.onnx` | 90,387,606 | 91,331,916 |
| INT8 | `onnx/model_quantized.onnx` | 22,972,370 | 23,916,680 |

压缩比为 **3.819×**。`evidence/model-files.json` 保存逐文件大小、下载来源和实际 SHA-256；大二进制模型及数据库文件不提交 Git，可由 `npm run models` 重建。

## 实测结果

以下数据来自本仓库 `evidence/performance.json` 的一次完整实测，不是预填指标。环境为 Windows、Headless Chrome 157，固定 WASM 后端；24 篇文档、6 个带相关性标签的查询，每个查询重复 3 次，延迟取逐轮原始值的中位数。

| 指标 | INT8 | FP32 |
|---|---:|---:|
| 新 Worker 模型加载 | 320.0 ms | 1465.4 ms |
| 24 篇向量化，batch=1 | 326.4 ms | 506.4 ms |
| 24 篇向量化，batch=8 | 302.0 ms | 536.6 ms |
| 查询向量中位耗时 | 5.95 ms | 9.30 ms |
| seekdb 查询中位耗时 | 4.28 ms | 4.52 ms |
| 查询端到端中位耗时 | 16.80 ms | 20.75 ms |
| Recall@5 | 1.000 | 1.000 |
| nDCG@5 | 1.000 | 1.000 |

本次 INT8 batch=8 相对 FP32 batch=8 的文档向量化加速为 **1.777×**。INT8 batch=8 相对自身 batch=1 为 1.081×；FP32 的对应比值是 0.944×，即这一轮没有提速，保留该结果而不宣称批量处理始终更快。

“新 Worker 模型加载”表示创建新 Worker 和模型实例的耗时，不等于清空浏览器 Cache Storage 后的首次网络下载。小语料上的满分指标只说明该固定实验没有观察到量化质量损失，不能外推为其他数据集上的通用结论。不同设备的绝对延迟会变化，应重新运行实验。

## 验证

```bash
npm run check
npm run build
npm audit --audit-level=high
npm run test:db
npm run test:browser
```

`test:db` 启动真实本地 seekdb，验证全文分支、向量分支、原文返回、索引刷新和增量写入。

`test:browser` 默认在 5187 端口启动独立应用实例，并自动清理测试集合，覆盖：

- 24 篇浏览器推理、写入、混合检索、RRF 分数和完整原文；
- JSON 增量导入、空集合隔离、切回集合后的数据保持；
- FP32 / INT8 质量和性能实验；
- 测试 Worker 缺少 WebGPU 时的真实 WASM 降级；
- 当前浏览器可用时的真实 WebGPU 推理与查询；
- 主流程无页面异常、无外部 HTTP 请求。

测试优先使用 Windows 系统 Chrome；其他平台可先执行 `npx playwright install chromium`，也可通过 `BROWSER_EXECUTABLE` 指定浏览器。使用 `BASE_URL` 时可测试已经启动的实例，`HEADED=1` 可显示浏览器窗口。

## 证据

- `evidence/model-files.json`：固定模型文件、字节数、SHA-256、压缩比。
- `evidence/db-smoke.json`：真实数据库混合检索及独立查询计时。
- `evidence/browser-e2e.json`：浏览器主流程、增量与集合隔离、WASM 降级、WebGPU、网络断言。
- `evidence/performance.json`：实验配置、环境、逐轮延迟、逐查询排名与质量指标。
- `evidence/browser-search.png`：真实混合检索界面。
- `evidence/experiment.png`：真实实验结果界面。

`PASS` 表示对应功能流程和实验完整性检查通过，不表示官方评测已经提交或获得名次。
