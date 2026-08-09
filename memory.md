# FOS·RAG — 项目编码规则（memory.md）

> 本文件是本项目的**编码宪法**。每条规则都源自代码库的实际惯例（附 `文件:行` 佐证），
> 而非凭空设想。新增功能或改动代码时，**必须遵守**这些规则以保持一致性。
>
> 冲突时的优先级：① 安全/数据完整性 ② 本文件的规则 ③ 个人偏好。

---

## 0. 最高原则：交互必须无刷新（AJAX-first）

**这是本项目最重要的交互规则。** 任何用户操作都不得触发整页刷新；全部用 AJAX 实时更新 DOM。

- **后端**：写操作视图（删除/保存/更新）必须检测 AJAX 并返回 `JsonResponse`，而非 `redirect`。
  检测方式（首选 `X-Requested-With`，兼容 `Accept`）：
  ```python
  is_ajax = (request.headers.get("x-requested-with") == "XMLHttpRequest"
             or "application/json" in (request.META.get("HTTP_ACCEPT") or ""))
  ```
  - AJAX → `JsonResponse({"ok": True, ...})`（带操作结果 + 最新统计，供前端即时渲染）。
  - 非 AJAX → `redirect(...)`（**优雅降级**，保留无 JS 时的可用性，但主路径永远是 AJAX）。
  - 参考：`kb/views.py:doc_delete`、`kb/views.py:site_settings` 的 `_json_response()`。
- **前端**：用 `fetch` POST，带 CSRF 头 + `X-Requested-With: XMLHttpRequest`；成功后**直接操作 DOM**（`row.remove()`、`textContent = 新值`、更新统计数字），失败用 `alert` 或 toast 反馈。
- **唯一例外**：SSE 流式问答（`/kb/stream/`）用 `@csrf_exempt` + 流式 `fetch`，因为它本身就在流式更新 DOM。

> ⚠ 反模式（禁止）：用 `<form method=post>` 整页 POST 后 `redirect` 重新渲染页面。
> 哪怕「能用」，也违背本项目的交互基调。已有表单（上传、设置保存）保留 POST 是历史遗留，
> 新增交互一律走 AJAX。

---

## 1. 权限装饰器（统一的 staff 分层）

```python
from django.contrib.auth.decorators import login_required, user_passes_test
_is_staff = user_passes_test(lambda u: u.is_staff)
```

| 场景 | 装饰器栈（从上到下） | 示例视图 |
|---|---|---|
| 仅 staff | `@_is_staff` + `@login_required`（顺序：staff 在外层） | `manage_*`、`doc_delete`、`site_settings` |
| 登录即可 | `@login_required` | `ask`、`document_html`、`conversation_*` |
| 公开 | 无装饰器 | `register_view` |
| 写操作 | 额外加 `@require_http_methods(["POST"])`（放最内层） | `doc_delete`、`conversation_delete` |

- `_is_staff` 在模块顶部定义一次、复用（`kb/views.py:23`、`dashboard/urls.py`）。
- 不要手写 `if not request.user.is_staff: ...`，用装饰器。

---

## 2. 设计系统："Inspection Tag"

**所有视觉必须用 `static/css/fos.css` 的 token 与组件**，不要硬编码颜色/字体。

### 颜色 token（`fos.css:8-27`）
| 用途 | token | 值 |
|---|---|---|
| 暖纸底 | `--paper` | `#F0EDE4` |
| 卡片面 | `--surface` | `#FFFFFF` |
| 深墨正文 | `--text` | `#241F18` |
| 通过/成功 | `--ok` | `#2E7D43` |
| 警告/注意 | `--warn` | `#E0A100` |
| 危险/主操作 | `--danger`（=`--accent`） | `#C2362C` |

> ⚠ 状态三色（`--ok`/`--warn`/`--danger`）的十六进制值在多处硬编码同步
> （`fos.css` 徽标 + `dashboard/views.py:DOC_STATUS_META`）。改一处要全局对齐。

### 字体
- `--fd` = Archivo（标题/标签，**UPPERCASE**）
- `--fb` = Inter（正文，含 PingFang SC 中文回退）
- `--fm` = IBM Plex Mono（数据/代码/时间戳）

### 签名组件
- `.fos-tag` —— 状态挂牌（`.th.ok/.warn/.danger` 彩色头 + `.stamp` 徽标 + `.tb` 正文）。`fos.css:171-177`。
- `.stat-card` —— 统计卡（`.num` Plex Mono + `.label` Archivo UPPERCASE）。
- `.doc-badge` + `.badge-{pending,ocr,indexing,completed,failed}` —— 文档状态徽标。
- `.btn` —— `.primary`(绿)/`.outline`/`.danger`(红)/`.sm`。
- `.toast` / `#toastHost` —— 无刷新反馈通知。

### 可访问性 & 响应式
- `:focus-visible` 必须有高亮轮廓（`fos.css:36`）。
- 移动端断点 `620px`（`fos.css:328`）；`prefers-reduced-motion` 关闭动画（`fos.css:341`）。

---

## 3. 模板结构

- **所有 kb/dashboard 页面 `{% extends "kb/_base.html" %}`**，不要自写完整 `<html>`。
  `_base.html` 提供 `<head>`（字体 + `fos.css`）、`.fos-topbar` 导航、`.fos-container`，两个块：
  - `{% block content %}` —— 页面主体
  - `{% block scripts %}` —— 页面专属 JS
- **POST 表单必须带 `{% csrf_token %}`**；JS 发 POST 时从 cookie 或隐藏域取 token（见 §5）。
- 页面专属样式可内联 `<style>`，但**不要定义新 token**；只用已有 token。
- `<html lang="zh-CN">`。

---

## 4. Python 风格

### 必须
- **文件首行 `from __future__ import annotations`**（几乎所有模块都有）。
- **类型注解**：函数签名 + 返回类型；局部变量按需。用 `dict`、`list[str]`、`str | None`、`Path`。
- **docstring 用中文**（模块 + 函数/类）。例：`"""kb views: 知识库管理 + RAG 问答 + SSE 流式端点。"""`。
- **`.save()` 必须传 `update_fields=[...]`** 做部分更新，绝不无参全量保存（`kb/views.py:138`、`pipeline.py` 全程）。
- **import 顺序**：stdlib → 第三方 → Django → 本地（`.models`/`.config`），组间空行。

### 关键惯例
- **延迟 import 避免 `AppRegistryNotReady`**：后台线程入口（`pipeline.process_document`）和 async 上下文里，先 `django.setup()` 再 import `.models`；或在函数体内 import。例：`pipeline.py:266-269`、`agent.py:36-46`。
- **可能运行在 Django 未就绪时的第三方 import** 加 `# noqa: E402`（`pipeline.py:29-40`）。
- **访问 Chroma 私有 API** 用 `# noqa: SLF001`（`views.py:118`）。
- **缓存聚合字段**（`KnowledgeBase.doc_count`/`chunk_count`）只统计 `status=COMPLETED` 的文档；删除/流水线两处重算逻辑必须一致（`views.py:134-138` ↔ `pipeline.py:304-307`）。

---

## 5. CSRF 与 AJAX 调用（前端）

| 场景 | CSRF 来源 | 备注 |
|---|---|---|
| 表单内的 JS POST | 隐藏域 `querySelector('[name=csrfmiddlewaretoken]')` | settings 页全局复用一个 token |
| 无表单的 JS POST（删除等） | cookie `csrftoken`（`getCSRFToken()` 助手） | `manage_detail.html:75-78`、`ask.html:348-351` |
| SSE 流式 | 无（视图 `@csrf_exempt`） | `/kb/stream/` |

**标准 fetch 调用模板**：
```javascript
const resp = await fetch(url, {
  method: 'POST',
  headers: {
    'X-CSRFToken': getCSRFToken(),
    'X-Requested-With': 'XMLHttpRequest',
  },
});
if (!resp.ok) throw new Error('HTTP ' + resp.status);
const data = await resp.json();
// 直接操作 DOM，不刷新页面
```

---

## 6. 配置层（`kb/config.py`）

- **DB 优先 → `.env` 回退**，统一走 `_eff(db_value, settings_default)` 助手（`config.py:16-20`）。
  空字段（`None`/`""`）→ 回退到 `.env`；非空 → 用 DB 值。
- **`SiteConfig` 单行模式**：`save()` 删除其它所有行；`SiteConfig.get()` 返回 `first() or create()`。
- **热更新**：保存即生效，**无需重启**。
- 四个取值函数：`llm_settings()`、`embedding_settings()`、`retrieval_settings()`、`mineru_settings()`，返回「有效值」dict。
- `retrieval_settings` 的整型字段用 `is not None` 判断（默认 0，不是 `""`），不用 `_eff`。

---

## 7. 零新依赖原则

- **本项目无独立 `requirements.txt`，复用上层 `MedicalAgent/.venv`。禁止往共享 venv 加新 pip 包。**
- 能用 stdlib / 内联实现就绝不引第三方库。已验证先例：
  - `kb/md2html.py`：纯 stdlib（`html`/`re`）的 Markdown→HTML 转换器，**不引入 `markdown` 库**。
  - 浏览器 xlsx 写入器：stdlib `zipfile` + XML 手写，monkey-patch 到 `pd.DataFrame.to_excel`（因为 Pyodide 无 openpyxl）。
  - Dashboard 趋势图：Python 内联 SVG，**无外部图表库**（不用 ECharts/Chart.js）。
- 图表/可视化优先用**内联 SVG**（Python 端预计算几何坐标，模板只输出）。

---

## 8. 命名与文案

| 类型 | 语言 | 示例 |
|---|---|---|
| 代码标识符 / 字段名 / slug | 英文小写 | `llm_base_url`、`chunk_count`、`P8` |
| 用户可见文案（按钮/提示/导航） | **中文** | 「新建对话」「删除」「手册」「检索」 |
| 模型 `verbose_name` / 字段首参 | **中文** | `CharField("名称", ...)`、`Meta.verbose_name="知识库"` |
| 状态 `choices` 显示值 | **中文** | `PENDING="pending","待处理"` |
| `__str__` | 用户友好（可含中文） | `f"{self.original_name} ({self.get_status_display()})"` |

- slug 字段 `allow_unicode=True`（允许 `P8` 等非纯 ASCII）。
- Agent 生成、面向 LLM 的提示词用中文（`agent.py` 的 system_prompt）。

---

## 9. 文档处理流水线

- OCR（MinerU）→ HTML 化（`md2html`，即时存 `Document.html_content`）→ 分块（按 `##` 标题，800 字/150 重叠，中文分隔符 `。`/`；`）→ 向量化（Chroma，每 KB 独立 collection）。
- **后台 daemon 线程**执行；前端每 2s 轮询 `/status/`。
- 文件名清洗集中化：`_mineru_safe_name()`、`_chroma_collection_name()` 都强制 `[a-zA-Z0-9._-]` 且长度 ≥3。
- **回填命令幂等**：`python manage.py build_doc_html [--force]`，单份失败不中断整体。

---

## 10. SSE 流式问答（`/kb/stream/`）

- 视图：`@csrf_exempt` + `@login_required`，`async def`。
- **所有 DB/配置工作放进 `_prepare()`，用 `sync_to_async` 包裹**（禁止在 async 生成器里直接访问 DB）。
- 事件帧：`f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"`；流尾 `event: done`。
- 响应头：`content_type="text/event-stream"`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。
- 最终落库（AI 消息 + citations）放生成器的 `finally` 块。
- 前端按 `\n\n` 分帧，前缀解析 `event:`/`data:` 行。
- 事件类型常量集中在 `kb/agent.py` 顶部（`SSE_TOKEN`/`SSE_TOOL_START`/`SSE_CITATIONS`…）。

---

## 11. 验证习惯

本项目**无自动化测试套件**，验证靠运行时检查。改动后至少做：

1. `python manage.py check` —— 无问题。
2. `python manage.py makemigrations --check` —— 确认无遗漏迁移（迁移是受追踪的，目前 0001–0007）。
3. 模板编译：用 `get_template(name)` 或直接访问端点看 200。
4. 端点冒烟：`django.test.Client` 登录后 GET/POST，断言状态码 + 关键内容。
5. **防御性编码替代测试**：可选副作用用 `try/except` + 日志（非阻断）；`get_or_create`/`update_or_create` 保证幂等；懒构建做「安全网」。

> 这些检查目前**未脚本化**；养成习惯每次改动后手动跑一遍。

---

## 12. 安全与数据完整性

- Agent 生成的代码**只在浏览器 Pyodide 沙箱执行**，绝不服务端 `exec`。
- OCR 内容经 `md2html` 时，普通 markdown 文本行做 HTML 转义（防注入）；MinerU 原始 HTML 块（`<table>`/`<img>`）原样透传。
- 站点配置 API Key 明文存 DB（与 `.env` 同级别）—— 已知限制，当前接受。
- 删除知识库时清 `data/chroma/<slug>/` 和 `data/md/<slug>/`；删除文档时清对应向量（按 `source` 过滤）+ 原始文件 + DB 记录，再重算 KB 缓存。
- 嵌入模型变更后，已索引文档需重新向量化。

---

## 快速自查清单（改动前）

- [ ] 新交互走 AJAX，无整页刷新？（§0）
- [ ] 权限装饰器用对了？（§1）
- [ ] 颜色/字体用 token，没硬编码？（§2）
- [ ] 模板 extends `_base.html`？（§3）
- [ ] `from __future__ import annotations` + 类型注解 + 中文 docstring？（§4）
- [ ] `.save(update_fields=...)`？（§4）
- [ ] 没往共享 venv 加新 pip 包？（§7）
- [ ] 文案中文、标识符英文？（§8）
- [ ] 改完跑 `manage.py check` + `makemigrations --check`？（§11）
