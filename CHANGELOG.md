# Changelog

## 2026-10-01 — PR 集成验证与升级说明

- 将远程 API 配置入口/总开关、上传恢复与表格兼容、手册概览修复集成到现有 PR #3，保留已提交的后台回答、设备隔离、未读提醒和响应式页面改进。
- 在最终 PR 分支重新执行：206 项 Django 测试、23 项 Node 测试全部通过；`makemigrations --check --dry-run` 无遗漏，diff 检查通过。
- 从尚未启用全部远程 API 开关的版本升级，需要执行 `python manage.py migrate` 应用迁移 0028；开关默认关闭。上传和概览修复本身不增加数据库迁移，也不自动重解析已有资料。
- 升级前备份业务数据库和索引，等待当前上传/回答结束后重启 ASGI。显式重建命令需暂停入库；同主机队列锁不支持跨服务器调度。
- 回退代码前先停止 Web 并备份数据；旧代码可读新索引，但无法提供批次续传。远程 API 总开关迁移保留兼容字段，可先保留数据库再回退代码；本轮不删除资料或已有索引。

## 2026-10-01 — 修复手册概览串库与章节遗漏

- 根因：混合文件名 `FL-8B漂流 使用说明书 PRINT.pdf` 的中文设备名没有独立参与词面路由；无明确范围时可能落到另一份默认手册。新增从文件名提取中文主题、剥离通用手册后缀的匹配，无需维护设备别名词表；同分不同资料仍视为歧义。
- “漂流手册有啥，给我总结一下”等整本概览先从本地关键词索引读取目录和各章代表片段，再生成回答，避免相似度 Top-K 只覆盖局部。兼容旧索引去掉章节数字、截短标题的情况；目录展示使用保存的原始 Markdown 标题。
- 概览片段有数量及字符预算，保留完整片段、不截断条件；说明目录和代表片段并非全文。来源快照、红圈出处与发布时的权限/内容校验继续使用现有机制。
- 概览聚焦章节内容，不罗列 OCR 数字和设备参数；强制先核对草稿再发布，即使普通解释的增强开关关闭也不提前发布。文件夹内多本手册要求先明确文档，避免混成一本。
- 无数据库迁移、无需重解析或重建索引。回滚本节对应代码即可；旧错误回答保留为历史记录，修复后的回答需要重新提问生成。
- 核对不通过时不直接拒绝整本概览：程序从当前可见文档、校验值一致的原始解析文本中摘取章节标题，作为明确标注的原文章节概览；不复用被拒绝草稿、不补写数字。发布前仍再次核查来源权限和内容。
- 验证：206 项 Django 回归通过，覆盖原问及改写、混合中文文件名、同名主题歧义、后部章节、旧标题形式、完整片段预算、强制核对及拒绝草稿后的原文回退。原问题真实回放约 56.40 秒，核对/原文发布检查通过，仅引用 `FL-8B漂流 使用说明书 PRINT.pdf`；小矮人等范围外出处 0。回放中模型的扩写草稿仍有缺证据条款，已回退为原文章节概览；不宣称所有自由总结事实问题已解决。已在无活动回答和上传任务时重启本地 Web。

## 2026-10-01 — 上传排队、分批保存和失败续传

- 上传流程改为同一主机串行队列：数据库保存 pending 状态，主机文件锁防止多个 Web 工作线程同时解析；ASGI 重启后自动回收并继续中断任务。
- 向量片段使用稳定 ID 分批 upsert，保存成功批次作为检查点。重试跳过已存在片段，复用已保存 OCR，不再整份重新计算。
- 超时、429、5xx 仅对失败批次进行一次拆分重试；认证、维度等错误直接失败。半批仍失败则停止并保留进度，不无限重试。
- 管理页显示批次进度、增加「重试（继续进度）」按钮；重复重试返回 409，保留部门权限和 CSRF。未完整写入关键词索引的任务不标记完成。
- 新集合保存向量配置指纹，阻止模型/端点变化后混用片段；无需新增数据库迁移。
- 验证：196 项 Django、23 项 Node 回归通过；包括真实 Chroma 中断后续传、两个工作线程串行处理和关键词失败恢复。运行边界及回退见 [上传可靠性说明](docs/upload-reliability.md)。

## 2026-09-30 — 修复表格省略结束标签导致整份文档入库失败

- 现象：「测试用」库中 `FL-8B漂流 使用说明书 PRINT.pdf` OCR 成功，向量化前报「表格 HTML 不完整」，整份文档失败。
- 根因：MinerU 输出的 79 张表中有 2 张电气接线表，最后一行缺少 `</tr>`，直接以 `</table>` 结束。HTML 规范允许省略 `</tr>`、`</td>`、`</th>`，由下一行、下一单元格或 `</table>` 隐式闭合；`kb/table_structure.py` 原先将其视为残缺。
- 修复：解析器按 HTML 规则隐式闭合单元格与行，末行内容完整保留。缺少 `</table>` 的截断表格和嵌套表格仍然报错，不静默丢内容。该文档 79 张表现已全部可解析，两张表的末行均已进入嵌入文本。
- 新增 `python manage.py reindex_failed [--doc <id>] [--dry-run]`：用已保存的 OCR 结果恢复失败文档，不重跑 MinerU。只处理独占文档库的失败文档；所在库还有其它文档时跳过；先检测 embedding 端点，不可用则不修改任何数据。
- 验证：新增 5 项回归（省略结束标签、截断/嵌套仍拒绝、恢复命令成功/共享库跳过/dry-run），共 179 项 Django 回归通过。真实文档 dry-run 列出 1 份待恢复；实际恢复因 8081 WeMM 向量服务未运行而安全中止，该文档仍为失败状态，待向量服务启动后执行。
- 网页进程以 `--noreload` 运行，需重启后新上传才使用修复后的解析器。

## 2026-09-30 — 修复索引数据库只读错误 1032

- 重建与删除知识库通过 Chroma API 删除集合，不再直接删除正在使用的 SQLite 目录，避免旧连接触发 `SQLITE_READONLY_DBMOVED`。删除并重建集合仍支持更换向量维度。
- 统一刷新本进程集合缓存，保留其他集合和数据库文件；只忽略集合不存在，不吞掉真实存储错误。
- 回归使用真实 Chroma 客户端，验证持有连接时重建、维度变化、保留其他集合、数据库 inode 不变及重复清理。
- 验证：180 项 Django 回归通过。受影响文档从已保存 OCR 恢复为 completed，数据库片段数与实际 Chroma 向量数均为 269，错误信息已清除；已备份业务数据库并重启 Web 刷新缓存。

## 2026-09-30 — 修复 WeMM 入库请求超时

- WeMM 的 OpenAI 兼容接口原先每批 16 条、等待 45 秒，资源竞争时一批耗时可能超过上限。改为默认最多 4 条、等待 180 秒，原生接口同步使用相同配置。
- 增加 `WEMM_BATCH_SIZE` / `WEMM_REQUEST_TIMEOUT` 部署参数；其他向量模型保留原批次及超时规则。
- 回归通过 MockTransport 验证 9 条输入实际分成 4/4/1 请求，以及原生接口批次、顺序、向量数量和超时配置。失败资料从保存的 OCR 恢复，不重新解析。

验证：182 项 Django 全量回归通过，diff 检查通过。失败资料以新批次完成真实向量化恢复，文档状态为 completed、数据库片段数与实际 Chroma 向量数均为 269、错误信息清空。已重启 Web 刷新集合缓存。

## 2026-09-26 — 配置首页直接切换远程 API

- 在 LLM 模型和地址下方直接显示远程／局域网 API 开关、已开启／已关闭状态及资料发送说明，无需打开编辑弹窗。
- 点击立即保存并刷新状态，保留地址、密钥、模型和其他服务配置；当前配置标为自定义，不覆盖已保存预设。保留管理员权限和 CSRF 校验。
- 增加开关往返保存、配置保留、预设不变、非法值及非管理员访问回归。

## 2026-09-26 — 全部远程 API 总开关

- 配置页顶部新增「允许全部远程／局域网 API」，覆盖 LLM、向量、重排和 MinerU，支持 HTTP/HTTPS，点击即保存。默认关闭，不修改服务地址、密钥或预设。
- 离线边界改为同时维护四类已配置端点的主机/端口白名单；任意服务读取配置均同步全部端点，避免服务间互相覆盖放行状态。非配置地址保持禁止。
- 总开关开启时，LLM 独立按钮显示由总开关控制；关闭总开关恢复 LLM 原独立设置，其他服务恢复离线模式的本机限制。
- 新增迁移 0028；数据库已备份并完成迁移，页面重启后确认总开关可见。174 项 Django 回归、迁移一致性及 diff 检查通过，未进行真实远程服务端到端验收。

## 2026-09-22 — 远程与局域网回答 API

- 回答模型新增「允许远程回答 API」开关，支持 OpenAI 兼容服务；HTTP 与 HTTPS 均可使用，局域网 IP 与公网域名适用相同选择规则。
- 非本机调用仍需显式开启；离线模式仅放行配置的主机、端口及 DNS 解析地址。旧配置默认关闭，旧预设不会继承远程权限。
- 测试和实际问答共用配置校验；远程模式不发送本地引擎参数或可选流式 usage，避免服务兼容性错误。不跟随重定向、不使用系统代理。
- 增加迁移 0027；保留现有本地模型配置。页面说明问题、历史和检索资料的发送范围，向量、OCR、重排仍独立配置。
- 验证：170 项 Django、23 项 Node 回归通过，迁移一致性与脚本语法检查通过。未配置真实外部账号，未宣称远程或局域网服务实测通过。

使用与回退见 [远程回答 API](docs/remote-answer-api.md)。

## Unreleased — 后台回答、设备范围隔离与界面改进

### 回答任务与会话恢复

- 回答生成从 HTTP/SSE 连接中独立出来。切换页面、刷新或断开显示连接后继续生成；回到会话自动恢复正文、引用和进度。只有显式停止、删除会话、超时或服务关闭才终止任务。
- 增加排队和生成中状态；同一会话只允许一个活动回答，当前 Web 进程一次执行一个生成任务，其余排队。重复提交返回 409。
- 首页复用公共数据流客户端，处理断流、超时、错误和完成事件；恢复读取忽略过期响应，权限失效或会话删除时停止轮询。
- 保存首字前失败原因，区分没有正文与已有部分正文。服务重启遗留的活动状态改为未完成，不让页面永久转圈；不承诺模型推理断点续算。

### 检索范围与回答输出

- 设备文件夹参与名称路由；同范围内中英文资料可共同检索。用户明确指定资料时覆盖旧默认库，切换主题时清除旧上下文，后续追问沿用最近明确命名的范围。
- 目录、关键词／向量检索、文件夹展开、原文提取和台账工具共同执行后端范围白名单，模型不能扩大检索范围。
- 工具调用最多五轮，重复同工具同参数时提前收尾，依据已有原文回答或说明缺失，避免循环耗尽步数后没有正文。
- 按完整段落发布普通回答，拦截拆分传输的原始工具调用标记；增强模式仍须通过来源与语义核对，不提前发布草稿。

### 提示与布局

- 生成中的会话标题缓慢闪烁，支持系统减少动画偏好。成功回答完成后显示未读小点，点击会话后消失；已读状态持久化，旧点击不能误清掉新回答提醒。
- 检索页最大宽度调整为 1680px，移除聊天栏的额外限宽；侧栏在 240–280px 自适应，标题增至 14px，两侧保留适度空白。保留手机抽屉和宽表格内部滚动。

### 迁移、部署与回滚

- 运行 `python manage.py migrate`，应用 0024–0026：未完成原因、活动状态与单会话唯一约束、已读游标。既有完整回答初始化为已读，不批量弹出旧消息提醒。
- 升级前等待当前回答结束并备份 SQLite；升级后重启 Web、刷新浏览器。无模型替换或文档索引重建。
- **后台任务目前仅支持单 Web 进程 ASGI 部署**。多 worker／多机需要共享任务队列；进程重启保留已保存内容，但不自动续算。
- 回滚前停止 Web，将 queued/running 记录标记为 incomplete；使用本版本代码回迁到 0023，再恢复旧代码。回迁会丢弃失败原因与已读游标，保留消息正文。详见 [后台任务运行说明](docs/background-answers.md)。

### 验证与边界

- Django 全量回归 162 项、Node 输入／数据流／恢复／通知回归 23 项通过；包含断开后继续、主动停止、排队、权限隔离、范围拒绝、追问继承与未读确认。
- 本地真实连接在首个心跳后断开，后台仍完整保存答案；浏览器验证切换页面再返回、进度恢复和未读小点点击清除。
- 真实指定设备问题回放只引用限定范围内的两份语言版本，范围外引用和工具标记均为零；这不等同于全部事实已完成人工核验。
- 1920px 屏幕下主体 1680px、侧栏 280px、回答栏 1276px；390px 屏幕下回答栏 358px，页面无横向溢出。
- 未宣称代表性资料集准确率或生产延迟目标通过。部分实测中重排服务未启动，使用原有降级检索。

---

## Previous release — PR #2

- Preserve HTML table cells when parser output omits span attributes, quotes them, reorders them, or uses header cells; reject malformed spans instead of silently dropping content.
- Bound embedding batches and request timeouts; support OpenAI-compatible WeMM endpoints and complete reranker URLs.
- Validate model test responses, distinguish inference from index compatibility, and provide a sequential test-all action. MinerU HTTP 200 health responses count as successful health checks.
- Bound conversation history and whole-turn execution; add streaming heartbeats, cancellation, timeout handling, and explicit incomplete-answer errors. Persist complete answers before sending the terminal SSE event.
- Improve citation navigation, page validation, Chinese IME handling, and add a return-to-site link in Django admin.
- Add optional structured query planning and verification-before-publication with current-source checks; keep enhancement opt-in.
- Correct evaluation ranks to use the globally merged retrieval order.
- Add opt-in local resource restrictions and configurable locally hosted Pyodide assets.

See [upgrade notes](docs/quality-upgrade.md) for configuration, migration, validation and limitations.

### Conversation persistence follow-up

Preserve interrupted visible answers with an incomplete status when switching pages; keep them out of future factual context. Remove device-name examples from the system prompt and prevent unverified historical claims from being replayed. Requires migration 0023. Regression totals: 143 Django and 15 Node.
