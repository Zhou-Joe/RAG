# 知识库层级化改造计划（plan.md）

> 目标：把扁平的「一个 KB 塞多份文档」改成 **文件夹 → 文档库** 层级结构。
> 每份文档独占一个 Chroma collection，从根本上杜绝跨文档检索混杂。
> Agent 能看到完整层级树并精准选库（搜某个文档 or 搜整个文件夹）。

---

## 现状问题

- P8 知识库下有 2 份文档（P8 报告 + Dumbo 报告），共用一个 Chroma collection（214 向量块）
- 检索时两份文档的结果混杂——问「P8 受力部件」会返回 Dumbo 内容
- 之前的临时修复（硬编码 P8/Dumbo 关键词过滤）脆弱、不可扩展
- `list_knowledge_bases` 只返回扁平列表，不显示文档名，Agent 无法知道哪份文档在哪个库

## 确认的设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 向量隔离方式 | **每文档独立 collection** | 彻底隔离，零跨文档混杂 |
| 跨文档搜索 | **允许搜整个文件夹** | 通用问题时扇出合并所有子库结果 |

---

## 改动清单（按依赖顺序）

### ✅ 1. 数据模型 — `kb/models.py` + migration（已完成）

给 `KnowledgeBase` 加层级字段：
```python
parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                           related_name="children", verbose_name="父知识库")
is_folder = models.BooleanField("是否文件夹", default=False)
```

- **文件夹**（`is_folder=True, parent=None`）：分组容器，不直接挂文档，无自身向量
- **文档库**（`is_folder=False, parent=<folder>`）：每份文档独占一个 Chroma collection

新增方法：
- `aggregate_counts()` — 文件夹聚合所有子库的 doc_count/chunk_count
- `child_doc_slugs()` — 返回扇出检索的子库 slug 列表

Migration: `0008_knowledgebase_is_folder_knowledgebase_parent.py`（已生成并 apply）

### ✅ 2. 检索器 — `kb/retriever.py`（已完成）

新增 `search_folder(child_slugs, query, k)`：
- 对每个子库 slug 调 `search(child, query, k=k)`
- 合并所有结果，按 score 排序，取前 k
- 某子库出错不阻断整体（try/except 跳过）

`search()` 本身不变（单库语义搜索）。

### ✅ 3. Agent 工具 — `kb/agent.py`（已完成）

#### `list_knowledge_bases()` — 重写为层级树
返回格式：
```
可用知识库（📁=文件夹可跨文档检索，📄=文档库搜单份文档）：
📁 CSEI Design Review（文件夹, slug=csei-design-review, 214 向量块）
  📄 P8 报告（slug=p8-report, 125 向量块）— 文档: P8 CSEI DR report...pdf
  📄 Dumbo 报告（slug=dumbo-report, 89 向量块）— 文档: 13YF140-YS01_Dumbo...pdf
```
让 LLM 清楚看到文档名 + slug，精准选库。

#### `kb_search()` — 文件夹扇出 + 删除硬编码过滤
- **删除** P8/Dumbo 硬编码关键词过滤（不再需要）
- 传文档库 slug → `search(target, query, k)`（单库）
- 传文件夹 slug → `search_folder(child_slugs, query, k)`（扇出合并）

#### system_prompt — 更新选库指引
说明层级结构 + 何时搜单文档 vs 搜整个文件夹。

### ⬜ 4. 数据迁移命令 — `kb/management/commands/split_kb_to_hierarchy.py`（待实现）

把现有扁平 KB（含多文档）拆成层级：
1. 把原 KB 改成文件夹（`is_folder=True`）
2. 为每份文档创建一个子文档库（slug 从文件名派生，如 `p8-report`、`dumbo-report`）
3. `Document.kb` 重指向子库
4. 删除旧 `data/chroma/<old_slug>/`，按文档重新向量化到各自 `data/chroma/<child_slug>/`
5. 更新各子库 `doc_count`/`chunk_count`

幂等、可重跑。需联网调 embedding API（重建向量）。

### ⬜ 5. 视图 — `kb/views.py`（待实现）

| 视图 | 改动 |
|---|---|
| `manage_list` | 区分文件夹/文档库查询，传层级结构给模板 |
| `manage_detail` | 文件夹页显示子库列表 + 创建子库表单；文档库页保持原行为 |
| `chat_stream._prepare()` | kb_slug 未指定时优先选有向量的文档库（而非文件夹） |
| `ask` | 传层级给前端，显示当前搜索范围 |

### ⬜ 6. 模板（待实现）

| 模板 | 改动 |
|---|---|
| `manage_list.html` | 文件夹卡片（📁，含子库列表 + 新建子库入口）；文档库卡片（📄） |
| `manage_detail.html` | 文件夹详情（子库列表 + 创建子库）；文档库详情（原文档列表 + 上传） |
| `ask.html` | 顶部显示搜索范围；`/kb/stream/` POST 带上 `kb_slug`（修当前不发的问题） |

### ⬜ 7. Admin — `kb/admin.py`（待实现）
`list_display` 加 `is_folder`、`parent`；`list_filter` 加 `is_folder`。

### ⬜ 8. 验证（待执行）
- `manage.py makemigrations --check` + `migrate` + `check` 通过
- 运行 `split_kb_to_hierarchy`：P8 KB → 文件夹 + 2 子库
- 验证向量数：文件夹聚合 = 214（89+125）
- `list_knowledge_bases` 返回层级树（含文档名 + slug）
- 搜 `p8-report` 库 → **只返回 P8 文档内容**（零 Dumbo 混杂）
- 搜文件夹 `csei-design-review` → 两份文档结果合并
- 手册管理页显示文件夹 → 子库层级
- 问答页显示当前搜索范围

### ✅ 9. 文档 — `project.md`（已完成）
更新数据模型、Agent 工具、项目结构、运行命令。

---

## 文件清单

| 文件 | 状态 | 改动 |
|---|---|---|
| `kb/models.py` | ✅ | `+parent` self-FK, `+is_folder`, `+aggregate_counts()`, `+child_doc_slugs()` |
| `kb/migrations/0008_*.py` | ✅ | 自动生成并已 apply |
| `kb/retriever.py` | ✅ | 新增 `search_folder()` |
| `kb/agent.py` | ✅ | 重写 `list_knowledge_bases`（层级树）、`kb_search`（扇出+删硬编码）、system_prompt |
| `kb/management/commands/split_kb_to_hierarchy.py` | ⬜ | **新** — 数据迁移命令 |
| `kb/views.py` | ⬜ | `manage_list`/`manage_detail`/`chat_stream`/`ask` 适配层级 |
| `templates/kb/manage_list.html` | ⬜ | 层级卡片 UI |
| `templates/kb/manage_detail.html` | ⬜ | 文件夹 vs 文档库两种视图 |
| `templates/kb/ask.html` | ⬜ | 显示搜索范围 + stream POST 带 kb_slug |
| `kb/admin.py` | ⬜ | 加 is_folder/parent 显示 |
| `project.md` | ✅ | 更新数据模型 + 架构说明 |

---

## 已知限制

- 数据迁移会**重建向量索引**（旧 `data/chroma/<slug>/` 删除，按文档重新 embedding），需联网调 embedding API
- 现有 `Conversation.kb` 仍指向旧 KB（迁移后指向文件夹），功能不受影响（文件夹可扇出搜索）
- 向量 metadata 仍只有 `source=文件名`，但因每个文档独占 collection，不再有跨文档混杂问题
