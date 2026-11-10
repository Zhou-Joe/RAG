# 更换 Embedding 模型操作手册

> 适用场景：把向量模型从 Qwen3-Embedding-4B（SiliconFlow 云端）切换到 WeMM-Embedding（本地部署），
> 或切换到任何其它 OpenAI 兼容的 embedding 服务（Ollama / vLLM / LM Studio / 云端 API）。

## 原理：为什么换模型必须整库重建

1. **Chroma collection 的维度建库时固定**。Qwen3-Embedding-4B 输出 2560 维，WeMM-2B 输出 2048 维。
   在旧 collection 里 `delete(where=...)` 清空数据不会改变维度，重灌不同维度向量直接报错。
2. **不同模型的向量空间不可比**。即使维度恰好相同（如都 2560），旧模型入库的向量和新模型编码的
   查询向量之间算相似度毫无意义。所以换模型 = 全部文档重新 embedding。

配套的代码支持：

- `kb/management/commands/reindex_clean.py` — 整目录删除 `data/chroma/<slug>/` 后从 `md_content`
  重新切块 + embedding（不重新 OCR）。
- `kb/retriever.py` — 向量库缓存带 embedding 配置指纹（base_url + model），设置页换模型后
  缓存自动失效，无需重启即可用新模型编码查询（但重建索引后仍建议重启）。

## 路径 A：本地 llama-server（Apple Silicon，当前实际在用 ✅）

**LM Studio 为什么不行（截至 2026-09 实测）**：
1. LM Studio 把 WeMM GGUF（arch=`qwen35`）归类为 LLM 而非 embedding（架构白名单未收录，
   见 lmstudio-bug-tracker #2177），`/v1/embeddings` 无法服务该模型；
2. LM Studio 0.4+ 的 API token 鉴权开启后，token 只在创建时显示一次，无法程序化获取；
3. 其 `/v1/embeddings` 仅支持文本——WeMM 的多模态能力在 LM Studio 里不可用。

**实际方案**：用 llama.cpp 的 `llama-server` 单独供 embedding（与 LM Studio 的 LLM 服务
共存，端口 1234=LLM、1235=embedding）。GGUF 已下载在 LM Studio 模型目录，两边共用。

```bash
# 已安装：brew install llama.cpp
# 一键启动（重启机器后需重新执行）：
./scripts/start_embedding.sh        # 后台可 nohup ./scripts/start_embedding.sh &

# 验证（应返回 2048 维向量）：
curl -s http://127.0.0.1:1235/v1/embeddings -H "Content-Type: application/json" \
  -d '{"model":"wemm-embedding-2b","input":["连接测试"]}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("维度:", len(d["data"][0]["embedding"]))'
```

站点设置页 `/kb/settings/`（staff）向量模型四项（2026-09-06 已按此配置）：

| 字段 | 值 |
|---|---|
| 向量 Base URL | `http://127.0.0.1:1235/v1` |
| 向量 API Key | `local-no-key`（本地服务忽略鉴权） |
| 向量模型 | `wemm-embedding-2b` |
| 向量维度 | 2048（信息记录用，请求不发送该参数） |

## 路径 A'：LM Studio（暂不可用，等官方支持）

LM Studio 后续版本若把 `qwen35` 加入 embedding 白名单，可直接在其内加载
`Weidows/WeMM-Embedding-2B-GGUF`，Base URL 改回 `http://127.0.0.1:1234/v1`，
API Key 填 LM Studio 的 API token（Developer → Server Settings → Manage Tokens），
再跑一次 `reindex_clean`（同维度换端点也建议重建，两个服务 tokenizer/池化策略可能有差异）。

## 路径 B：vLLM（Linux GPU 服务器，完整多模态）

WeMM 官方部署方式，文本/图片/视频统一向量空间：

```bash
pip install vllm
vllm serve Tencent/WeMM-Embedding-2B --runner pooling --port 8001
# 4B 换 Tencent/WeMM-Embedding-4B（2560 维）；9B → 4096 维
```

之后 `/kb/settings/` 里向量 Base URL 填 `http://<GPU机IP>:8001/v1`，模型名 `Tencent/WeMM-Embedding-2B`，
API Key 任意非空。其余步骤相同。

## 切换后重建索引（两条路径通用）

```bash
cd /Users/zc/development/MedicalAgent/FOS_RAG
.venv 上层解释器：/Users/zc/development/MedicalAgent/.venv/bin/python

# 1. 重建（整目录删除 data/chroma/<slug>/ 后从 md_content 重新 embedding）
/Users/zc/development/MedicalAgent/.venv/bin/python manage.py reindex_clean

# 2. 重启 Django（清掉长驻进程的旧向量库句柄）
```

## 回滚

把 `/kb/settings/` 的向量三项改回原值（SiliconFlow + Qwen/Qwen3-Embedding-4B），
再跑一次 `reindex_clean`。配置指纹机制保证查询侧立即跟随新配置。

> ⚠️ 2026-09-06 实测：SiliconFlow 账户余额不足（402），云端 embedding 已不可用，
> 这也是切本地的原因之一。回滚云端前需先充值。

## 图片多模态块入库的前置条件与自检

图片块（`type=image`）与整页块只在 embedding 客户端**具备图片嵌入能力**时才会入库（`kb.pipeline.supports_image_embedding`）：

| 配置 | 客户端 | 图片块 |
|---|---|---|
| 模型名含 `embeddinggemma`，URL 以 `/v1` 结尾 | `OpenAIGemmaVisionEmbeddings` | ✅ `POST {base}/embeddings` 的 `{"text":…, "image_base64":…}` 条目 |
| 模型名含 `embeddinggemma` 或 `wemm`，URL **不**以 `/v1` 结尾 | `EmbeddingGemmaEmbeddings` / `WeMMEmbeddings` | ✅ `POST {base}/embed` 原生协议 |
| 其它（含 WeMM 挂 vLLM `/v1/embeddings`、云端 OpenAI） | `OpenAIEmbeddings` | ❌ 跳过（日志 INFO「不支持图片嵌入」） |

开关与能力检查：

```bash
# 1. 服务侧多模态开关（modalities 应含 image / text+image）
curl -s http://10.136.185.9:8001/health

# 2. 图文融合条目直测（应返回 768 维且与同 caption 文本余弦 ≈0.8）
curl -s http://10.136.185.9:8001/v1/embeddings -H "Content-Type: application/json" \
  -d '{"model":"embeddinggemma-2","input":[{"text":"图纸","image_base64":"<裸base64>"}]}'

# 3. 已入库的图片块数（为 0 = 图片根本没嵌）
python - <<'PY'
import sqlite3, glob
t = 0
for f in glob.glob('data/chroma/*/chroma.sqlite3'):
    c = sqlite3.connect('file:' + f + '?mode=ro', uri=True)
    t += c.execute("select count(*) from embedding_metadata where key='type' and string_value='image'").fetchone()[0]
print('image vectors:', t)
PY
```

只补图片而文本向量仍有效时（同一模型/维度/端点，图文共用一个向量空间），`reindex_clean` 会连文本一起重嵌——本项目实测 161 份文档约 18 分钟，直接跑它最省心。若确实不能停检索，可用 `backfill_doc_images.py --reindex` 的思路按文档粒度推（注意该命令**会重跑 MinerU**，图片已在盘上时不适用）。

另：`Document.page_embed`（整页视觉入库）是按文档勾选的独立开关，默认关；开启后才会走 `_index_page_chunks` 渲染整页。打开后新索引仅对**重新索引过的文档**生效。

## 已知限制

- LM Studio 的 `/v1/embeddings` 不支持 `dimensions` 参数（Matryoshka 截断不可用），
  本项目代码本就不发送该参数，无影响。
- WeMM-2B 文本检索质量（MMEB-v3 Text ~45-49）不保证优于 Qwen3-Embedding-4B（2560 维，
  MTEB 强项）；换本地模型的主要收益是**离线/零成本**，以及走 vLLM 时的多模态扩展。
- 换模型后旧的检索质量体感应重新评估（问答页问几个已知问题对照）。
