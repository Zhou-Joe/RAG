from __future__ import annotations

"""知识图谱查看与审核；所有写操作使用 CSRF 和 AJAX。"""

import asyncio
import json
import time

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from . import access
from .knowledge_graph import (
    PREDICATES, attach_document, evidence_current, path_label, relation_current,
    relation_queryset, review_relation, seed_dataset, walk_graph,
    fact_snapshot, validate_fact,
)
from .models import Document, GraphEntity, GraphExtraction, GraphRelation, KnowledgeBase, StructuredDataset


def _extraction_events(user_id: int, job_id: int, slug: str, cursor: int) -> tuple[dict, list[dict]]:
    """每批重新校验当前用户、文档库和管理权限，避免长连接沿用已撤回权限。"""
    user = get_object_or_404(get_user_model(), pk=user_id, is_active=True)
    libraries = _libraries(user)
    kb = get_object_or_404(libraries, slug=slug)
    scopes = libraries.filter(Q(pk=kb.pk) | Q(parent=kb))
    job = get_object_or_404(GraphExtraction.objects.select_related('document__kb'), pk=job_id, document__kb__in=scopes)
    if not access.can_manage_kb(user, job.document.kb):
        raise Http404('任务不存在')
    snapshot = {'id':job.pk, 'status':job.status, 'document':job.document.original_name, 'focus':job.focus,
                'created':job.created_relations, 'rejected':job.rejected, 'done':job.done,
                'total':job.total, 'error':job.error, 'coverage':job.coverage,
                'updated_at':job.updated_at.isoformat()}
    events = [{'id':event.pk, 'kind':event.kind, 'payload':event.payload, 'at':event.created_at.isoformat()}
              for event in job.events.filter(pk__gt=cursor).order_by('pk')[:100]]
    return snapshot, events


def _event_frame(kind: str, payload: dict, event_id: int | None = None) -> str:
    """用 JSON 编码事件正文，保留换行文本而不破坏 SSE 帧。"""
    return (f'id: {event_id}\n' if event_id is not None else '') + f'event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'


@login_required
async def graph_events(request, job_id: int):
    """只读 SSE：读取持久化增量，断开页面不会终止或重复执行抽取任务。"""
    value = request.headers.get('Last-Event-ID') or request.GET.get('after', '0')
    if not value.isascii() or not value.isdecimal() or len(value) > 19 or int(value) > 9223372036854775807:
        return JsonResponse({'error':'无效的活动记录位置。'}, status=400)
    cursor = int(value)
    user = await request.auser()
    slug = request.GET.get('kb', '')
    snapshot, events = await sync_to_async(_extraction_events)(user.pk, job_id, slug, cursor)

    async def stream():
        nonlocal cursor, snapshot, events
        started = heartbeat = time.monotonic()
        previous = None
        yield 'retry: 1200\n\n'
        while True:
            if snapshot != previous:
                yield _event_frame('snapshot', snapshot)
                previous = snapshot
            for event in events:
                cursor = event['id']
                yield _event_frame('progress', event, cursor)
            if len(events) < 100 and snapshot['status'] in ('completed', 'failed'):
                yield _event_frame('done', snapshot)
                return
            if time.monotonic() - started >= 55:
                return  # 浏览器自动携带 Last-Event-ID 重连，不占用无限长请求。
            if time.monotonic() - heartbeat >= 10:
                yield ': heartbeat\n\n'
                heartbeat = time.monotonic()
            if len(events) < 100:
                await asyncio.sleep(0.25)
            try:
                snapshot, events = await sync_to_async(_extraction_events)(user.pk, job_id, slug, cursor)
            except Http404:
                yield _event_frame('unavailable', {'message':'任务已删除或访问权限已变化。'})
                return

    response = StreamingHttpResponse(stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache, no-store'
    response['X-Accel-Buffering'] = 'no'
    return response


def _libraries(user):
    """从当前权限解析范围，不依赖客户端声明。"""
    qs = KnowledgeBase.objects.all()
    qs = qs.filter(access.kb_q(user,admin_all=True))
    return qs.order_by("name")


def _located_evidence(evidence):
    """从当前块身份解析页码，复用聊天原文证据面板；不伪造无版面的定位。"""
    result = {'item': evidence, 'current': evidence_current(evidence), 'locations': []}
    if not evidence.document_id or not result['current']:
        return result
    from .keyword_index import get_chunks
    from .models import ChunkProvenance
    doc = evidence.document
    rows = get_chunks(doc.kb.slug, doc.original_name)
    ids = [row['chunk_id'] for row in rows if row.get('chunk_id') and evidence.quote in row['text']]
    result['locations'] = [{'chunk_id': p.chunk_id, 'page': p.page_label()}
                           for p in ChunkProvenance.objects.filter(document=doc, chunk_id__in=ids)]
    return result


def _relation_row(relation, visible_library_ids: set) -> dict:
    """列表、图谱侧栏共享同一份来源与适用条件。"""
    from .graph_schema import CONDITION_NAMES
    return {"relation": relation, "current": relation_current(relation),
            "conditions": [{'label': CONDITION_NAMES.get(key, key), 'value': value}
                           for key, value in relation.qualifiers.items()],
            "predicate": PREDICATES.get(relation.predicate, relation.predicate),
            "evidence": [_located_evidence(ev) for ev in relation.evidence.all()
                         if (ev.document_id and ev.document.kb_id in visible_library_ids)
                         or (ev.record_id and ev.record.dataset.kb_id in visible_library_ids)]}


@login_required
def graph_relation(request, relation_id: int):
    """只返回当前可见范围内一条关系的原文审核卡。"""
    libraries = _libraries(request.user)
    kb = get_object_or_404(libraries, slug=request.GET.get('kb', ''))
    scopes = libraries.filter(Q(pk=kb.pk) | Q(parent=kb))
    relation = get_object_or_404(relation_queryset().select_related('extraction'), pk=relation_id,
                                 subject__kb__in=scopes, object__kb__in=scopes)
    manager = access.can_manage_kb(request.user, relation.subject.kb)
    row = _relation_row(relation, set(libraries.values_list('pk', flat=True)))
    if not manager and (relation.status != GraphRelation.Status.VERIFIED or not row['current']):
        raise Http404('关系不存在')
    return JsonResponse({'html': render_to_string('kb/_graph_relation.html', {
        'row': row, 'can_manage': manager,
    }, request=request), 'relation_id': relation.pk})


@login_required
def graph_fact(request, relation_id: int):
    """引用页按快照版本重验，已撤回或来源变化时不显示旧关系为当前事实。"""
    if isinstance(relation_id,str) and relation_id.startswith('k') and relation_id[1:].isascii() and relation_id[1:].isdecimal():
        from .hierarchy_views import fact
        if len(relation_id)>19:raise Http404('内容不存在')
        return fact(request,int(relation_id[1:]))
    if not str(relation_id).isascii() or not str(relation_id).isdecimal() or len(str(relation_id))>18:
        raise Http404('关系不存在')
    relation = get_object_or_404(relation_queryset(), pk=relation_id,
                                 subject__kb__in=_libraries(request.user), object__kb__in=_libraries(request.user))
    snapshot = fact_snapshot(relation)
    if snapshot is None or not validate_fact(snapshot, request.user):
        return HttpResponse("该关系已撤回或来源失效，请重新检索。", status=410)
    if request.GET.get("v") and request.GET["v"] != snapshot["fact_digest"]:
        return HttpResponse("该关系或来源已变化，请重新检索。", status=410)
    return render(request, "kb/graph_fact.html", {"fact": snapshot, "relation": relation})


@login_required
def graph_view(request):
    """展示来源、审核状态和两跳关联路径，搜索通过 AJAX 刷新结果区。"""
    libraries = _libraries(request.user)
    slug = request.GET.get("kb", "")
    documents = Document.objects.filter(kb__in=libraries,status='completed').exclude(md_content='').select_related('kb__parent').order_by('kb__parent__name','original_name')
    selected_document = None
    document_id = request.GET.get('document','')
    if document_id:
        try:
            selected_document = get_object_or_404(documents,pk=document_id)
        except (ValidationError,ValueError):
            return JsonResponse({'error':'请选择有效的资料。'},status=400)
    kb = get_object_or_404(libraries, slug=slug) if slug else None
    if selected_document:
        if kb and kb.pk!=selected_document.kb_id and selected_document.kb.parent_id!=kb.pk:
            raise Http404('资料不属于当前知识库')
        kb = selected_document.kb
    elif kb and not kb.is_folder:
        selected_document = documents.filter(kb=kb).first()
    elif not kb:
        latest = GraphExtraction.objects.filter(document__in=documents).select_related('document__kb').order_by('-pk').first()
        selected_document = latest.document if latest else documents.first()
        kb = selected_document.kb if selected_document else None
    manager = bool(kb and access.can_manage_kb(request.user, kb))
    visible_library_ids = set(libraries.values_list("pk", flat=True))
    scopes = libraries.filter(Q(pk=kb.pk) | Q(parent=kb)) if kb else libraries.none()
    scope_slugs = list(scopes.values_list("slug", flat=True))
    query = request.GET.get("q", "").strip()[:1000]
    status = request.GET.get("status", "")
    offset_text = request.GET.get('offset','0')
    if not offset_text.isascii() or not offset_text.isdecimal() or len(offset_text)>7 or int(offset_text)>1000000:
        return JsonResponse({'error':'无效的关系页位置。'},status=400)
    offset = int(offset_text)
    run_id = request.GET.get('run', '')
    selected_run = None
    if run_id:
        if not run_id.isascii() or not run_id.isdecimal() or len(run_id) > 18:
            return JsonResponse({'error': '请选择有效的抽取批次。'}, status=400)
        jobs = GraphExtraction.objects.filter(document__kb__in=scopes)
        if selected_document:
            jobs = jobs.filter(document=selected_document)
        selected_run = get_object_or_404(jobs, pk=run_id)
    walked = walk_graph(scope_slugs, query) if kb and query else {"paths": [], "ambiguous": False, "truncated": False}
    relations = relation_queryset().select_related('extraction').filter(subject__kb__in=scopes, object__kb__in=scopes).order_by("-pk") if kb else GraphRelation.objects.none()
    if selected_document:
        relations = relations.filter(Q(extraction__document=selected_document)|Q(evidence__document=selected_document)).distinct()
        path_ids = {relation.pk for path in walked['paths'] for relation in path}
        if path_ids:
            visible_path_ids = set(relations.filter(pk__in=path_ids).values_list('pk',flat=True))
            walked['paths'] = [path for path in walked['paths'] if all(relation.pk in visible_path_ids for relation in path)]
    if not manager:
        relations = relations.filter(status=GraphRelation.Status.VERIFIED)
    elif status in GraphRelation.Status.values:
        relations = relations.filter(status=status)
    if selected_run:
        relations = relations.filter(extraction=selected_run)
    if query:
        path_ids = {relation.pk for path in walked["paths"] for relation in path}
        relations = relations.filter(Q(subject__label__icontains=query) | Q(object__label__icontains=query)
                                     | Q(subject__key__icontains=query) | Q(object__key__icontains=query)
                                     | Q(pk__in=path_ids))
    if selected_run:
        walked['paths'] = [path for path in walked['paths'] if all(r.extraction_id == selected_run.pk for r in path)]
    node_id = request.GET.get('node', '')
    if node_id:
        if not node_id.isascii() or not node_id.isdecimal() or len(node_id) > 18:
            return JsonResponse({'error': '请选择有效的实体。'}, status=400)
        node = get_object_or_404(GraphEntity, pk=node_id, kb__in=scopes)
        relations = relations.filter(Q(subject=node) | Q(object=node))
    records = list(relations[offset:offset+101])
    if request.GET.get('format') == 'graph':
        nodes, edges = {}, []
        for relation in records[:100]:
            current = relation_current(relation)
            if not manager and not current:
                continue
            for entity in (relation.subject, relation.object):
                nodes[str(entity.pk)] = {'id': str(entity.pk), 'data': {
                    'label': entity.label, 'kind': entity.kind, 'kb_slug': entity.kb.slug}}
            edges.append({'id': f'relation-{relation.pk}', 'source': str(relation.subject_id),
                          'target': str(relation.object_id), 'data': {
                              'relation_id': relation.pk, 'label': PREDICATES.get(relation.predicate, relation.predicate),
                              'status': relation.status, 'current': current,
                              'focus': relation.extraction.focus if relation.extraction_id else '',
                          }})
        return JsonResponse({'nodes': list(nodes.values()), 'edges': edges, 'truncated': len(records) > 100,
                             'total_count':relations.count() if manager else None,
                             'offset':offset,
                             'can_manage': manager, 'selected_node': node_id})
    rows = []
    for relation in records[:100]:
        current = relation_current(relation)
        if not manager and not current:
            continue
        rows.append(_relation_row(relation, visible_library_ids))
    runs = GraphExtraction.objects.filter(document__kb__in=scopes).select_related('document__kb').order_by('-pk')
    if selected_document:
        runs = runs.filter(document=selected_document)
    context = {
        "libraries": libraries, "selected_kb": kb, "can_manage": manager,
        "q": query, "status": status, "statuses": GraphRelation.Status.choices,
        "selected_run": selected_run,
        "runs": runs, 'selected_document':selected_document,
        "rows": rows, "truncated": len(records) > 100 or walked["truncated"],
        "offset":offset,"previous_offset":max(0,offset-100),"next_offset":offset+100,
        "paths": list(dict.fromkeys(path_label(path) for path in walked["paths"])),
        "ambiguous": walked["ambiguous"],
        "extractions": [{"job": job, "active": job.status in ("pending", "running"),
                         "label": {"pending": "等待抽取", "running": "正在抽取", "completed": "抽取完成", "failed": "抽取失败"}.get(job.status, job.status)}
                        for job in runs],
    }
    context['live_jobs'] = [{'id':run.pk, 'status':run.status, 'error':run.error,
                             'created':run.created_relations, 'focus':run.focus,
                             'label':f'#{run.pk} · {run.focus or "旧版全书抽取"} · {run.document.original_name}'}
                            for run in context['runs'] if manager and access.can_manage_kb(request.user, run.document.kb)]
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"html": render_to_string("kb/_graph_results.html", context, request=request),
                             "live_jobs":context['live_jobs'],
                             "runs": [{'id': run.pk, 'label': f'#{run.pk} · {run.focus or "旧版全书抽取"} · {run.document.original_name}'}
                                      for run in context['runs']]})
    context['documents'] = documents
    parent=kb.parent if kb and kb.parent_id else kb
    context['hierarchy_scope']=parent if parent and parent.pk in visible_library_ids else kb
    from .models import KnowledgeObject
    from .knowledge_hierarchy import visible_assertions
    from itertools import islice
    visible_object_ids={item.node.object_id for item in islice(visible_assertions(request.user,list(libraries.values_list('slug',flat=True))),2000)}
    context['knowledge_objects']=KnowledgeObject.objects.filter(pk__in=visible_object_ids).order_by('label')
    groups = {}
    for document in documents:
        group = document.kb.parent.name if document.kb.parent_id else document.kb.name
        groups.setdefault(group,[]).append(document)
    context['document_groups'] = [{'name':name,'documents':items} for name,items in groups.items()]
    context["unassigned"] = StructuredDataset.objects.filter(
        active=True, kb__isnull=True,
    ).order_by("source_name") if access.is_global_admin(request.user) else []
    return render(request, "kb/graph.html", context)


@login_required
@require_POST
@transaction.atomic
def graph_action(request):
    """服务器校验管理权限、关系范围和证据原文后应用操作。"""
    kb = get_object_or_404(_libraries(request.user), slug=request.POST.get("kb", ""))
    if not access.can_manage_kb(request.user, kb):
        raise Http404("知识库不存在")
    action = request.POST.get("action", "")
    try:
        if action not in {"bind", "backfill", "review", "attach", "extract", "retry_extract"}:
            raise ValueError("未知操作。")
        if action in {"review", "attach", "bind", "retry_extract"}:
            field = "dataset" if action == "bind" else "extraction" if action == "retry_extract" else "relation"
            value = request.POST.get(field, "").strip()
            if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 9223372036854775807:
                raise ValueError("请先选择要归入的业务数据集。" if field == "dataset"
                                 else "请先在关系列表中选择一条关系，再进行操作。")
        if action == "attach":
            import uuid
            try:
                uuid.UUID(request.POST.get("document", ""))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("请选择要关联的手册。") from None
        if action == "bind":
            if not access.is_global_admin(request.user):
                raise Http404("数据集不存在")
            dataset = get_object_or_404(StructuredDataset.objects.select_for_update(),
                                        pk=request.POST.get("dataset"), active=True, kb__isnull=True)
            if StructuredDataset.objects.filter(kb=kb, kind=dataset.kind, active=True).filter(
                Q(source_name=dataset.source_name) | Q(checksum=dataset.checksum)
            ).exists():
                raise ValueError("本库已有同类同名或相同内容的数据集，请先核对版本。")
            dataset.kb = kb
            dataset.save(update_fields=["kb"])
            count = seed_dataset(dataset)
            return JsonResponse({"ok": True, "message": f"已归入知识库，生成 {count} 条待核对关系。"})
        if action == "backfill":
            count = sum(seed_dataset(ds) for ds in StructuredDataset.objects.filter(kb=kb, active=True))
            return JsonResponse({"ok": True, "message": f"已生成 {count} 条新候选，已有审核结论保持不变。"})
        if action in {"extract", "retry_extract"}:
            from .graph_extraction import schedule_document
            previous = None
            if action == 'retry_extract':
                scopes = _libraries(request.user).filter(Q(pk=kb.pk) | Q(parent=kb))
                previous = get_object_or_404(GraphExtraction.objects.select_for_update().select_related('document__kb'),
                                             pk=request.POST.get('extraction'), document__kb__in=scopes)
                if previous.status != 'failed':
                    raise ValueError('只有已失败的抽取任务可以重试；正在运行的任务无需重复提交。')
            focus = previous.focus if previous else request.POST.get('focus', '').strip()
            if not 2 <= len(focus) <= 1000:
                raise ValueError('请填写一个关注方面（2–1000字），例如“检测人员的资质和视力要求”。')
            docs = Document.objects.filter(Q(kb=kb) | Q(kb__parent=kb), kb__in=_libraries(request.user), status="completed").exclude(md_content="")
            document = get_object_or_404(docs, pk=previous.document_id if previous else request.POST.get('document') or None)
            if not access.can_manage_kb(request.user,document.kb):
                raise Http404('资料不可管理')
            if previous:
                from .publication import digest
                if digest(document.md_content) != previous.source_digest:
                    raise ValueError('资料已变化，请在上方重新指定关注方面并开始构建。')
            target=None
            if previous:
                target=previous.target_object
            elif request.POST.get('target_object'):
                from .models import KnowledgeObject
                target_id=request.POST['target_object']
                if not target_id.isascii() or not target_id.isdecimal() or len(target_id)>18:
                    raise ValueError('请选择有效的已有对象。')
                target=get_object_or_404(KnowledgeObject.objects.filter(nodes__assertions__document__kb__in=_libraries(request.user)).distinct(),pk=target_id)
            if target:
                from .knowledge_hierarchy import visible_assertions
                from itertools import islice
                if target.pk not in {item.node.object_id for item in islice(visible_assertions(request.user,list(_libraries(request.user).values_list('slug',flat=True))),2000)}:
                    raise Http404('对象不存在')
            count = int(schedule_document(document, retry=True, focus=focus,schema_version=2,target_object=target))
            job = GraphExtraction.objects.filter(document=document).order_by('-pk').first()
            return JsonResponse({"ok": True, "job_id":job.pk if job else None,
                                 "message": ("已新建重试任务，原有候选保留，仍需逐条确认。" if previous else "已开始构建，可在下方查看进度。") if count
                                             else "这份资料已有任务正在构建，已显示当前进度。"})
        scopes = _libraries(request.user).filter(Q(pk=kb.pk) | Q(parent=kb))
        relation = get_object_or_404(relation_queryset().select_for_update(),
                                     pk=request.POST.get("relation"), subject__kb__in=scopes, object__kb__in=scopes)
        if action == "review":
            review_relation(relation, request.POST.get("status", ""), request.user)
        elif action == "attach":
            document = get_object_or_404(Document.objects.filter(kb__in=_libraries(request.user)),
                                          pk=request.POST.get("document"))
            attach_document(relation, document, request.POST.get("quote", ""), request.user)
        else:
            raise ValueError("未知操作。")
    except (ValueError, TypeError, ValidationError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "message": "已保存。新增原文证据后需重新核对。" if action == "attach" else "审核状态已更新。"})
