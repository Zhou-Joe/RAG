from __future__ import annotations

"""定时采集任务的进程内服务层：一句话 → 验证过的采集脚本 → 定时数据源。

页面（`kb/connector_views`）、命令行（`manage.py build_feed`）、外部接口与 AI 助手对话工具都调用
这里，业务规则只有一份实现：谁能建源、内网开关、凭据归属、cron 计算、CAS 状态机、首跑。
所有函数只收普通参数、用 `ValueError` 报业务错误，不接触 `request` 或 `JsonResponse`。

典型用法（外部入口都是这三步）：

    job = collector_api.create_draft(user=user, requirement='每天13点拉 …/board/ 每车里程', api_url=…,
                                     authentication={'type': 'bearer', 'token': key}, created_via='cli')
    job.refresh_from_db()            # 生成在后台线程跑，分钟级；轮询 status/preview/questions
    feed = collector_api.approve(user=user, job=job)      # 显式批准才会建 cron 数据源

批准步骤永远不由系统自动完成：模型只能产出候选脚本和真实样例，用户确认后才入库并启用调度。
"""

import json
import uuid

from django.db import transaction
from django.utils import timezone

from . import access
from .connector_agent import authorize, recover_stale, start_async
from .connector_tools import check_spec, redact
from .feed_credentials import api_origin, bindings, discard_if_unused, secret_values, store
from .feed_cron import CronSchedule
from .feed_http import validate_headers
from .feed_protocol import validate_url
from .feeds import claim as claim_feed, execute as execute_feed, start_async as start_feed
from .models import ConnectorDraft, DEPARTMENT_GENERAL, KnowledgeBase, KnowledgeFeed

_UNSET = object()          # 区分「没给这个参数」和「显式给了 None/False」
UNSET = _UNSET             # 入口层（页面/CLI/外部接口）使用的公开别名


# ---------------------------------------------------------------- 权限与开关

def require_manager(user) -> None:
    """建采集任务需要资料管理权限（全局管理员或部门管理员）。"""
    if not user or not getattr(user, "is_active", False) or not access.is_manager(user):
        raise ValueError("当前账号没有资料管理权限，无法配置自动采集。")


def resolve_private(user, requested, *, inherited=False) -> bool:
    """内网/本机地址开关的唯一判定。

    - 采集任务默认开放内网：新任务没给参数即开启，链路本地、组播等硬底线仍在 `feed_http`/`browser_runtime`。
    - 编辑对话里没给参数＝沿用上一轮；显式传 False 仍可关闭。
    """
    if requested is None or requested is _UNSET:
        return bool(inherited)
    if not isinstance(requested, bool):
        raise ValueError("内网开关格式无效。")
    return requested


def build_credential(user, authentication, url, *, header_env=None):
    """结构化认证说明 → `(credential, header_env 引用)`。

    `authentication` 必须带 `type`（`kb.feed_credentials.normalize_auth` 会在歧义时报错，
    绝不静默按「不认证」处理）。凭据明文只进加密列，返回值里只有环境变量引用。
    """
    if authentication is not None and not isinstance(authentication, dict):
        raise ValueError("认证配置格式无效。")
    credential = store(user, url, authentication or {"type": "none"}) if url else None
    if credential is not None:
        return credential, bindings(credential)
    headers = validate_headers(header_env or {})
    if headers and not access.is_global_admin(user):
        raise ValueError("环境变量认证仅供全局管理员配置，请直接使用认证表单填写凭据。")
    return None, headers


def resolve_auth(user, url, *, authentication=_UNSET, credential=_UNSET, header_env=_UNSET):
    """把三种认证给法归一为 `(credential, header_env)`，不在此发明默认含义。

    结构化 `authentication` 交给 `store()`（形状不合法会报错，不会静默变成无认证）；
    页面可以传已存在的凭据或环境变量引用（包括「复用上一个任务的凭据」）。
    """
    if credential is not _UNSET or header_env is not _UNSET:
        current = None if credential is _UNSET else credential
        if header_env is _UNSET:
            return current, (bindings(current) if current else {})
        if current is not None and set(header_env) != set(bindings(current)):
            raise ValueError("凭据引用与实际请求头不一致，请重新填写认证。")
        # 只有直接给出的 FOS_FEED_ 环境变量引用需要全局管理员；凭据自带的绑定引用不是。
        if current is None and header_env and not access.is_global_admin(user):
            raise ValueError("环境变量认证仅供全局管理员配置，请直接使用认证表单填写凭据。")
        return current, header_env
    if authentication is _UNSET:
        authentication = {"type": "none"}
    return build_credential(user, authentication, url)


# ---------------------------------------------------------------- 生成任务

def create_draft(*, user, requirement, api_url="", authentication=_UNSET, credential=_UNSET,
                 header_env=_UNSET, allow_private=True, sample=None, kb=None, created_via="web", conversation=False):
    """创建采集脚本生成任务并在后台启动，立即返回草稿（不等生成结果）。

    认证三种给法：结构化的 `authentication={'type':'bearer','token':…}`（API/CLI/对话，
    服务层落库加密）；已存在的 `credential`/`header_env`（页面复用已存凭据）；都不给则无认证。
    """
    require_manager(user)
    recover_stale()
    requirement = (requirement or "").strip()
    minimum=1 if conversation else 10
    if not minimum <= len(requirement) <= 10000:
        raise ValueError("请输入1至10000字符的问题或任务需求。" if conversation else "请用10至10000字符说明数据来源、采集内容与更新频率。")
    if ConnectorDraft.objects.filter(user=user, status="running").exists():
        raise ValueError("已有生成任务正在运行，请等待完成。")
    if api_url:
        validate_url(api_url, allow_fragment=True)
    if kb is not None and not access.can_manage_kb(user, kb):
        raise ValueError("没有该知识库的管理权限。")
    private = resolve_private(user, allow_private)
    credential, headers = resolve_auth(user, api_url, authentication=authentication,
                                       credential=credential, header_env=header_env)
    if headers and not api_url:
        raise ValueError("请在「实际 API 地址」填写接收凭据的接口地址。")
    if sample is not None and len(json.dumps(sample, ensure_ascii=False, allow_nan=False)) > 40000:
        raise ValueError("请提供不超过40000字符的脱敏 JSON 样例。")
    # 先建凭据再脱敏：用户原话里的密钥会被替换为 [REDACTED]，模型改用已绑定的凭据取数。
    secrets = secret_values(type("Auth", (), {"credential": credential, "header_env": headers})())
    with transaction.atomic():
        job = ConnectorDraft.objects.create(
            user=user, kb=kb, prompt=redact(requirement, secrets), api_url=api_url,
            credential=credential, header_env=headers, allow_private=private,
            sample=redact(sample, secrets), created_via=created_via)
    start_async(job.pk)
    return job


def refine_draft(*, user, job, requirement, api_url=_UNSET, credential=_UNSET, header_env=_UNSET,
                 allow_private=None, question_answers=None) -> ConnectorDraft:
    """在同一对话里按自然语言反馈生成新候选；沿用已保存凭据，仍等待用户批准。"""
    authorize(job)
    if job.user_id != user.pk:
        raise ValueError("原任务不属于当前账号。")
    if job.status not in ("answered", "ready", "needs_info", "failed", "cancelled", "enabled"):
        raise ValueError("请等待当前预览完成后再调整；已启用的数据源请在数据源页面管理。")
    if ConnectorDraft.objects.filter(user=user, status="running").exists():
        raise ValueError("已有预览正在生成，请等待或停止后重试。")
    feedback = (requirement or "").strip() or "请使用最新的网页与 API 采集工具，按原始需求重新读取真实数据并生成预览。"
    if question_answers is not None:
        from .feed_reports import capture_question_answers
        feedback=capture_question_answers(user,job,feedback,question_answers)
    if not 1 <= len(feedback) <= 4000:
        raise ValueError("请用1至4000字符回答问题或说明修改要求。")
    if any(host in feedback for host in ('.environment.api.powerplatform.com','.logic.azure.com','.logic.azure.cn')):
        from .feed_reports import source_feed, capture_feedback
        feedback = capture_feedback(user, source_feed(job), feedback)
    with transaction.atomic():
        url = _chain_url(job) if api_url is _UNSET else (api_url or "")
        if url:
            validate_url(url, allow_fragment=True)
        if credential is _UNSET:
            credential, headers = job.credential, job.header_env
            if credential and url and api_origin(url) != credential.api_origin:
                raise ValueError("API 主机已改变，请在认证表单重新填写凭据。")
        else:
            headers = job.header_env if header_env is _UNSET else header_env
        private = resolve_private(user, allow_private, inherited=job.allow_private)
        holder = type("Auth", (), {"credential": credential, "header_env": headers})()
        feedback = redact(feedback, (*secret_values(job), *secret_values(holder)))
        if job.status == "enabled":
            if not job.feed_id:
                raise ValueError("原数据源已被删除，请创建新任务。")
            if not ConnectorDraft.objects.filter(pk=job.pk, status="enabled").update(updated_at=timezone.now()):
                raise ValueError("该对话上下文已清空，请重新打开数据源继续修改。")
        elif not ConnectorDraft.objects.filter(pk=job.pk, status__in=["answered", "ready", "needs_info", "failed", "cancelled"],
                                              feed__isnull=True).update(status="revised", updated_at=timezone.now()):
            raise ValueError("该预览已被更新或启用，请查看最新任务。")
        child = ConnectorDraft.objects.create(
            user=user, kb=job.kb, parent=job, feedback=feedback, prompt=job.prompt, api_url=url,
            header_env=headers, credential=credential, allow_private=private, sample=job.sample,
            retain_context=job.retain_context or bool(job.feed_id), created_via=job.created_via)
    start_async(child.pk)
    return child


def cancel(*, user, job) -> ConnectorDraft:
    """停止正在生成的任务；已入库的数据源不受影响。"""
    authorize(job)
    if job.user_id != user.pk:
        raise ValueError("原任务不属于当前账号。")
    ConnectorDraft.objects.filter(pk=job.pk, status="running").update(
        status="cancelled", error="用户已停止生成。", updated_at=timezone.now())
    job.refresh_from_db()
    return job


# ---------------------------------------------------------------- 批准入库

def approve(*, user, job, write_mode=None, department=None, run_now=True, trigger="manual",
            first_run_async=True, selected_fields=None) -> KnowledgeFeed:
    """批准已验证方案；页面异步首跑，CLI 用 first_run_async=False 等待入库完成。

    selected_fields 只筛选正文字段，保留取数/计算依赖与系统生成的采集日期。
    """
    with transaction.atomic():
        authorize(job)
        if job.user_id != user.pk:
            raise ValueError("原任务不属于当前账号。")
        if job.status == "enabled" and job.feed_id:
            return job.feed
        if job.status != "ready":
            raise ValueError("请先完成连接器生成和样例校验。")
        if not ConnectorDraft.objects.filter(pk=job.pk, status="ready", feed__isnull=True).update(status="enabling"):
            raise ValueError("连接器正在启用，请稍后查看。")
        spec = check_spec(job, job.spec)
        if selected_fields is not None:
            available = {field['label'] for field in spec['body_fields']}
            if (not isinstance(selected_fields, list) or
                    any(not isinstance(label, str) or label not in available for label in selected_fields)):
                raise ValueError('请选择预览中已有的保留字段。')
            # Only project the output. Browser capture, stable IDs and calculation inputs stay intact.
            keep = set(selected_fields) | {'采集日期'}
            spec = {**spec, 'body_fields': [field for field in spec['body_fields'] if field['label'] in keep]}
        if spec.get("source_type") == "browser" and (
                not job.browser_state.get("tested") or job.browser_state.get("validation_scope") != "full_batch"):
            raise ValueError("网页脚本尚未通过完整批次验证，请继续对话重新验证后确认。")
        mode = write_mode or spec.get("write_mode", "append")
        if mode not in ("append", "upsert"):
            raise ValueError("请选择追加保留历史或覆盖更新。")
        spec = {**spec, "write_mode": mode}
        kb = job.kb
        if kb is None:
            dept = (department or DEPARTMENT_GENERAL) if access.is_global_admin(user) else access.user_department(user)
            if dept not in access.all_departments():
                raise ValueError("请选择已有部门。")
            kb = KnowledgeBase.objects.create(name=spec["name"], slug="auto-" + uuid.uuid4().hex[:20], is_folder=True,
                category="business", description="由采集脚本自动更新的业务数据", department=dept, created_by=user)
        elif not access.can_manage_kb(user, kb):
            raise ValueError("没有该知识库的管理权限。")
        cron = spec.get("cron", "0 * * * *")
        zone = spec.get("timezone", "Asia/Shanghai")
        schedule = CronSchedule(cron, zone)
        existing, seen = None, set()
        ancestor = job.parent if job.parent_id else None
        while ancestor and ancestor.pk not in seen and len(seen) < 100:
            seen.add(ancestor.pk)
            if ancestor.user_id != user.pk:
                raise ValueError("原任务不属于当前用户。")
            if ancestor.feed_id:
                existing = ancestor.feed
                break
            ancestor = ancestor.parent
        if existing:
            if existing.kb_id != kb.pk or not access.can_manage_kb(user, kb):
                raise ValueError("原数据源所属知识库已变化，请重新建立任务。")
            if existing.lease_token and existing.lease_until and existing.lease_until > timezone.now():
                raise ValueError("原数据源正在更新，请完成后再批准新脚本。")
            feed = existing
            feed.name = spec["name"]; feed.url = spec["url"]; feed.mode = "pull"; feed.adapter_spec = spec
            feed.write_mode = mode; feed.credential = job.credential; feed.header_env = job.header_env
            feed.allow_private = job.allow_private; feed.cron = cron; feed.timezone = zone
            feed.enabled = True; feed.cursor = ""; feed.etag = ""
            feed.next_run_at = schedule.next_after(timezone.now())
            feed.save(update_fields=["name", "url", "mode", "write_mode", "adapter_spec", "credential", "header_env",
                "allow_private", "cron", "timezone", "enabled", "cursor", "etag", "next_run_at", "updated_at"])
        else:
            feed = KnowledgeFeed.objects.create(kb=kb, name=spec["name"], url=spec["url"], mode="pull",
                write_mode=mode, adapter_spec=spec, credential=job.credential, header_env=job.header_env,
                allow_private=job.allow_private, cron=cron, timezone=zone, created_by=user,
                next_run_at=schedule.next_after(timezone.now()))
        job.kb = kb; job.feed = feed; job.status = "enabled"; job.spec = spec
        job.save(update_fields=["kb", "feed", "status", "spec", "updated_at"])
        from .connector_storage import retain_chain
        retain_chain(job)
        run = claim_feed(feed.pk, trigger=trigger, force=True)[0] if run_now else None
    if run is not None:
        if first_run_async:
            start_feed(run.pk)
        else:
            execute_feed(run.pk)
    return feed


# ---------------------------------------------------------------- 只读辅助

def status(job) -> dict:
    """给非 HTTP 入口（CLI、外部接口、对话工具）用的最小状态视图，不包含凭据明文。"""
    return {
        "id": str(job.pk), "status": job.status, "created_via": job.created_via,
        "api_url": job.api_url, "allow_private": job.allow_private,
        "has_credential": bool(job.credential_id or job.header_env),
        "questions": job.questions, "answer": job.preview.get("answer", ""), "error": job.error,
        "preview": {"valid": bool(job.preview.get("valid")), "items": job.preview.get("items", 0),
                    "warnings": len(job.preview.get("warnings", []))} if job.preview else {},
        "spec": job.spec, "feed_id": str(job.feed_id) if job.feed_id else None,
        "kb": job.kb.slug if job.kb_id else "",
        "needs_user": job.status in ("needs_info", "ready", "failed"),
    }


def discard_orphan(credential_id) -> None:
    """替换凭据后清理不再被引用的旧密文（页面与 CLI 共用）。"""
    if credential_id:
        discard_if_unused(credential_id)


def _chain_url(job) -> str:
    """沿对话链找第一个已确定的来源地址（新建任务常只在后续消息里给出真实 URL）。"""
    node, url, seen = job, "", set()
    while node and node.pk not in seen and len(seen) < 100:
        seen.add(node.pk)
        url = node.api_url or node.spec.get("url", "")
        if url:
            break
        node = node.parent
    return url or job.api_url


source_url = _chain_url     # 公开别名：入口层绑凭据时要先知道实际主机



source_url = _chain_url     # 公开别名：入口层绑凭据时要先知道实际主机
