# FOS·RAG — 游乐设施维护手册智能检索系统

基于 Django + LangGraph 的检索增强生成（RAG）问答系统，专为**游乐设施维护手册**设计。上传 PDF/Markdown/TXT 手册，系统自动 OCR 解析、分块、向量化，随后用户可用自然语言提问，获得带出处的流式回答；还能在浏览器沙箱里运行 Python（pandas/matplotlib）做数据分析与 Excel/图表导出。

---

## 技术栈

| 层 | 技术 |
|---|---|
| **Web 框架** | Django 6.0.6 + Daphne（ASGI，支持 SSE 流式） |
| **Agent 框架** | LangChain 1.3.9 + LangGraph 1.2.5 |
| **LLM** | DeepSeek（OpenAI 兼容接口，可配置切换） |
| **Embedding** | SiliconFlow（OpenAI 兼容，可配置切换） |
| **向量数据库** | ChromaDB 1.5.9（每个知识库一个 collection） |
| **OCR** | MinerU 3.3.1（本地 API，PDF → Markdown） |
| **Agent 记忆** | LangGraph AsyncSqliteSaver（`data/checkpoints.sqlite3`，跨重启持久化） |
| **浏览器沙箱** | Pyodide v0.26.4（WASM，pandas/numpy/matplotlib） |
| **数据库** | SQLite（`db.sqlite3`） |
| **设计系统** | 自研 "Inspection Tag" 主题（Archivo + Inter + IBM Plex Mono） |
| **Python** | 3.14（复用上层 `MedicalAgent/.venv`） |

---

## 项目结构

```
FOS_RAG/
├── fos_rag/                # Django 项目配置
│   ├── settings.py         # 配置（.env 读取、数据目录、静态文件）
│   ├── urls.py             # 根路由
│   ├── asgi.py / wsgi.py
├── accounts/               # 用户认证（登录/注册/登出）
├── dashboard/              # 数据看板（运行日志：统计卡 + 14天趋势 + 文档状态 + 手册库一览 + 最近活动）
├── kb/                     # 核心应用
│   ├── models.py           # 6 个模型（KnowledgeBase 含文件夹/文档库层级，见下）
│   ├── agent.py            # LangGraph Agent + SSE 流式（层级选库 + 文件夹扇出检索）
│   ├── pipeline.py         # OCR → HTML 化 → 分块 → 向量化流水线
│   ├── retriever.py        # Chroma 检索封装（search 单库 / search_folder 跨子库扇出）
│   ├── md2html.py          # 零依赖 Markdown→HTML 转换器
│   ├── config.py           # 配置有效值读取层（DB 优先 → .env 回退）
│   ├── views.py            # 所有视图（管理/问答/配置/会话）
│   ├── urls.py
│   └── migrations/
├── templates/              # 模板
│   ├── home.html           # 首页
│   ├── prototype.html      # 设计原型（可移除）
│   ├── kb/
│   │   ├── _base.html      # 基础布局（顶栏+导航）
│   │   ├── ask.html        # 问答页（SSE + Pyodide + 会话历史）
│   │   ├── manage_list.html
│   │   ├── manage_detail.html
│   │   └── settings.html   # 站点配置（含分类预设）
│   ├── accounts/
│   └── dashboard/
├── static/css/fos.css      # Inspection Tag 设计系统
├── data/
│   ├── checkpoints.sqlite3 # Agent 会话记忆
│   ├── chroma/             # 向量库（每个 KB 一个子目录）
│   └── md/                 # OCR 输出
├── media/documents/        # 上传的文档
├── logs/                   # 日志
├── .env                    # 环境变量（LLM/Embedding/MinerU 配置）
└── manage.py
```

---

## 数据模型（`kb/models.py`）

| 模型 | 用途 |
|---|---|
| **KnowledgeBase** | 知识库节点（UUID pk，name/slug/description/parent self-FK/is_folder）。分两层：**文件夹**（`is_folder=True`，分组容器，不直接挂文档，搜索时扇出到子库）和**文档库**（`is_folder=False`，每份文档独占一个 Chroma collection）。`aggregate_counts()` 聚合子库统计；`child_doc_slugs()` 返回扇出检索的子库 slug 列表。 |
| **Document** | 上传的文档（kb FK，file，md_content，html_content，status: pending→ocr→indexing→completed/failed） |
| **SiteConfig** | 站点配置（单行，LLM/Embedding/检索/MinerU 字段，空值回退 .env） |
| **ConfigPreset** | 配置预设（按分类 llm/embedding/retrieval/mineru，JSON 存快照） |
| **Conversation** | 会话（user FK，kb FK，title，thread_id ← LangGraph checkpointer key） |
| **Message** | 会话消息（conversation FK，role: user/ai，content，citations JSON），用于 UI 历史展示 |

### 知识库层级结构（文件夹 → 文档库）
```
📁 CSEI Design Review (文件夹 slug=csei-design-review)
  📄 P8 报告 (文档库 slug=p8-report, 125 向量块) — 独占 data/chroma/p8-report/
  📄 Dumbo 报告 (文档库 slug=dumbo-report, 89 向量块) — 独占 data/chroma/dumbo-report/
```
- 每份文档独占一个 Chroma collection，从根本上杜绝跨文档检索混杂
- Agent 通过 `list_knowledge_bases` 看到完整层级树（含文档名 + slug），精准选库
- `kb_search` 传文档库 slug → 只搜该文档；传文件夹 slug → 跨所有子库扇出合并（`search_folder`）
- 数据迁移：`python manage.py split_kb_to_hierarchy` 把旧扁平 KB 拆成文件夹 + 子文档库 + 重建索引

---

## 核心功能

### 1. 文档处理流水线（`kb/pipeline.py`）
- **上传**：PDF / Markdown / TXT → 按知识库归档
- **OCR**：PDF → MinerU API（本地 :8888）→ Markdown
- **HTML 化（查看页用）**：OCR 后即时用零依赖转换器（`kb/md2html.py`）把 Markdown 转成 HTML 正文存入 `Document.html_content`（MinerU 表格/图片等原始 HTML 块原样透传，供文档查看页渲染真表格）
- **嵌入前清洗（`_md_for_embedding`，仅嵌入路径）**：MinerU 的「Markdown」其实是 HTML 超集——表格是原始 `<table><tr><td rowspan=N colspan=M>`。若直接 embedding，~50% token 是 `rowspan=1 colspan=1` 噪声。`_md_for_embedding` 在切块前把：HTML 表格 → **结构化纯文本**（二维网格 + rowspan/colspan 下沉算法，还原合并单元格，每行 `|` 分隔）；`<img>`/markdown 图片 → 去除；`<details>` → 只留正文；LaTeX `$…$` → 去 `$`；HTML 实体 → 反转义。**关键：查看页（`md_to_html`）仍用原始 `md_content` 渲染真 HTML 表格，清洗只发生在 `run_indexing` 的嵌入路径，二者独立。**
- **分块**：按 `##` 标题分节，RecursiveCharacterTextSplitter（800 字 / 150 重叠）
- **向量化**：Embedding → Chroma（每个 KB 独立 collection + persist dir）
- **异步执行**：后台 daemon 线程，前端每 2s 轮询状态
- **回填**：`python manage.py build_doc_html` 可为已处理文档从 `md_content` 重建 HTML（无需重新 OCR）
- **清洗重建**：`python manage.py reindex_clean [--kb SLUG]` 对已处理文档用清洗后文本重新向量化（不重新 OCR，需联网调 embedding）。用于让 `_md_for_embedding` 改造在旧向量上生效。

**关键修复**：
- `_mineru_safe_name()`：中文/特殊字符文件名转 ASCII（MinerU 要求 `[a-zA-Z0-9._-]`）
- `_chroma_collection_name()`：短 slug（如 `P8`）补齐到 3 字符（Chroma 最小长度要求）

### 2. RAG 问答（`kb/agent.py` + `kb/views.py`）
- **LangGraph Agent**：`create_agent` + 3 个工具
  - `list_knowledge_bases()`：列出知识库层级树（📁 文件夹 + 📄 文档库，含文档名 + slug + 向量数），agent 据此精准选库
  - `kb_search(query, k, kb_slug)`：语义检索；传文档库 slug → 只搜该文档（零跨文档混杂）；传文件夹 slug → 跨所有子库扇出合并（`retriever.search_folder`）
  - `write_analysis(code, filename)`：生成 Python 分析代码 → 浏览器沙箱执行
- **检索经济性**：system_prompt 约束单轮 kb_search ≤ 2 次 + recursion_limit=15，控制 token 膨胀
- **SSE 流式**：`astream_events(v2)` → token 逐字输出
- **工具状态展示**：`tool_start`/`tool_end` 事件 → Claude 风格的实时状态行（🔍 检索知识库… ✓）
- **Token 统计**：顶栏小表格显示累计(入/出) + 单次最大(入/出)，用于估测本地部署上下文窗口

### 3. 浏览器代码沙箱（`templates/kb/ask.html`）
- **Pyodide v0.26.4**（WASM Python）：页面加载时后台预热（pandas/numpy/matplotlib）
- **Excel 导出**：纯 Python xlsx 写入器（zipfile+XML），monkey-patch 到 `pd.DataFrame.to_excel`
- **图表渲染**：matplotlib → `savefig("plot.png")` → 页面内联显示 + 下载
- **安全隔离**：代码只在浏览器 WASM 执行，不触碰服务器

### 4. 会话管理（持久化多轮对话）
- **Conversation/Message 模型**：UI 历史展示
- **AsyncSqliteSaver**：Agent 真实记忆（`data/checkpoints.sqlite3`），跨重启保持上下文
- **操作**：新建对话 / 查看历史 / 继续对话 / 删除

### 5. 站点配置（`kb/settings.html` + `kb/views.py`）
- **热更新**：保存即生效，无需重启（`kb/config.py` 读取层 DB 优先 → .env 回退）
- **分类预设**：每个服务（LLM/Embedding/检索/MinerU）各自存预设，独立切换
- **AJAX 无刷新**：保存/加载/删除全部用 fetch + toast 通知
- **连接测试**：探测 LLM/Embedding（`/models`）和 MinerU（`/health`）

### 6. 运行日志看板（`dashboard/views.py` + `templates/dashboard/index.html`）
- **统计卡**：累计用户 / 手册文档 / 向量块总数 / 检索会话 / 累计消息 / 今日活跃（全量来自既有模型，无新表）
- **14 天问答趋势**：`Message` 按 `TruncDate` 聚合，inline SVG 双折线（提问/回复），无外部图表库
- **文档状态分布**：`Document` 按 status 聚合 → 水平条形，复用 Inspection Tag 状态色
- **手册库一览**：每库的文档/向量数 + 状态拆分（完成/处理中/失败）
- **最近活动**：最近会话列表，跳转问答页
- **权限**：staff-only（与 `/kb/manage/`、`/kb/settings/` 一致）

### 7. 文档查看 + AI 出处高亮（`kb/md2html.py` + `kb/document_html.html` + `kb/agent.py`）
- **HTML 化存储**：文档 OCR 后即时转成 HTML 正文存入 `Document.html_content`（零依赖转换器，MinerU 表格/图片原样透传）
- **文档查看页**：`/kb/doc/<doc_id>/html/` 渲染 HTML 正文（Inspection Tag 排版，表格/图片/标题样式化）
- **AI 出处链接**：每轮问答结束后，AI 回答下方出现「📎 来源出处」芯片，点击在新标签打开对应文档查看页并**高亮引用的文本片段**
  - `kb_search` 工具检索时把来源（doc_id/source/text）累积到 citations sink
  - 流结束发出 `citations` SSE 事件 → 前端渲染可点击链接 → 持久化到 `Message.citations`（历史会话重载仍可见）
  - 高亮为**客户端文本匹配**（去空白后子串定位，无需重新向量化，对已有 125 个向量即时生效）
- **`doc_id` 解析**：向量只带 `source=文件名`，检索时按 KB 一次性把文件名解析成 doc UUID（无需给向量加迁移字段）


---

## URL 路由

| URL | 视图 | 权限 | 说明 |
|---|---|---|---|
| `/` | `home.html` | 公开 | 首页 |
| `/accounts/login/` | `LoginView` | 公开 | 登录 |
| `/accounts/register/` | `register_view` | 公开 | 注册 |
| `/accounts/logout/` | `LogoutView` | 登录 | 登出 |
| `/kb/ask/` | `ask` | 登录 | 问答页（会话历史 + 聊天 + 沙箱） |
| `/kb/stream/` | `chat_stream` | 登录 | SSE 流式问答端点（`@csrf_exempt`） |
| `/kb/manage/` | `manage_list` | staff | 知识库管理 |
| `/kb/manage/<slug>/` | `manage_detail` | staff | 文档上传 + 状态 |
| `/kb/manage/<slug>/status/` | `doc_status_api` | staff | 文档处理状态 JSON |
| `/kb/doc/<doc_id>/html/` | `document_html` | 登录 | 文档 HTML 查看（?h= 高亮片段） |
| `/kb/settings/` | `site_settings` | staff | 站点配置 + 预设 |
| `/kb/settings/test/` | `settings_test` | staff | 连接测试 |
| `/kb/conv/<tid>/messages/` | `conversation_messages` | 登录 | 会话历史 JSON |
| `/kb/conv/<tid>/delete/` | `conversation_delete` | 登录 | 删除会话 |
| `/dashboard/` | `index` | staff | 数据看板（运行日志：统计 + 趋势 + 状态 + 库一览 + 最近活动） |
| `/prototype/` | `prototype.html` | 公开 | 设计原型页 |

---

## 环境变量（`.env`）

```ini
DJANGO_SECRET_KEY=...
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=...

# MinerU OCR
MINERU_API_BASE=http://localhost:8888
MINERU_API_KEY=
MINERU_BACKEND=pipeline
MINERU_LANG=ch

# Embedding (SiliconFlow)
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=...
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
EMBEDDING_DIMENSIONS=1024

# LLM (DeepSeek)
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=...
LLM_MODEL=deepseek-v4-flash
LLM_TEMPERATURE=0.2

# 检索参数
KB_CHUNK_SIZE=800
KB_CHUNK_OVERLAP=150
KB_TOP_K=5
```

> 以上配置均可在 `/kb/settings/` 页面用 UI 修改（存入数据库），空值自动回退到 .env。

---

## 运行方式

```bash
# 1. 激活上层 venv（含 Django + LangChain + ChromaDB）
source /Users/zc/development/MedicalAgent/.venv/bin/activate

# 2. 启动 MinerU OCR（另一个终端）
mineru-api --host 127.0.0.1

# 3. 启动 Django 开发服务器
cd /Users/zc/development/MedicalAgent/FOS_RAG
python manage.py migrate
python manage.py split_kb_to_hierarchy   # 一次性：把旧扁平 KB 拆成文件夹 + 文档库层级（重建索引）
python manage.py build_doc_html          # 一次性：为已处理文档生成 HTML 正文（文档查看页用）
python manage.py reindex_clean           # 一次性：用清洗后文本（HTML 表格→结构化）重新向量化已处理文档
python manage.py runserver 0.0.0.0:8000

# 4. 浏览器访问
# http://127.0.0.1:8000/          首页
# http://127.0.0.1:8000/kb/ask/   问答（需登录）
# http://127.0.0.1:8000/kb/manage/  管理（需 staff）
# http://127.0.0.1:8000/kb/settings/ 配置（需 staff）
```

---

## 设计系统（"Inspection Tag"）

源自游乐设施维护的 LOTO（Lockout/Tagout）挂牌操作语境：

- **配色**：暖纸底 `#F0EDE4` + 深墨 `#241F18` + 状态三色（绿 OK `#2E7D43` / 黄 CAUTION `#E0A100` / 红 DANGER `#C2362C`）
- **字体**：Archivo（标题/标签，UPPERCASE）+ Inter（正文）+ IBM Plex Mono（数据/代码）
- **签名组件**：`.fos-tag` 状态挂牌（彩色头 + stamp 徽标 + 内容体）
- **响应式**：620/760/840px 断点 + `prefers-reduced-motion` 支持

---

## 依赖（复用上层 `MedicalAgent/.venv`）

> ⚠ 本项目无独立 `requirements.txt`，依赖上层 venv。

核心包：django 6.0.6 · daphne 4.2.2 · langchain 1.3.9 · langgraph 1.2.5 · langchain_openai 1.3.2 · langchain_chroma 0.2.6 · chromadb 1.5.9 · openai 2.42.0 · httpx 0.28.1 · aiosqlite 0.22.1 · langgraph-checkpoint-sqlite 3.1.1 · pandas 3.0.3 · python-dotenv

浏览器 CDN：Pyodide v0.26.4 · marked v12

---

## 已知限制

- 无 `requirements.txt`，不能独立安装（依赖上层 venv）
- Dashboard 不含 LLM token 用量统计（token 仅在每次请求内存累计、未持久化）；趋势图「提问」以会话内用户消息数近似
- 浏览器沙箱 xlsx 写入器仅支持单表/文本单元格（无格式/公式/多表）
- 站点配置的 API Key 以明文存储（与 .env 同级别）
- 嵌入模型变更后，已索引文档需重新向量化
- 出处高亮为**客户端文本匹配**（去空白后子串定位）：若 LLM 改写了检索片段或片段在文档里多次出现，可能定位不到/定位到首处（会给出「未定位」提示，不影响阅读）
- 向量只带 `source=文件名`，`doc_id` 在检索时按 KB 一次性解析；同库内重名文件会导致出处链接歧义
