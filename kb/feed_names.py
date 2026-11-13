"""Source names follow the KB; delivery titles describe their own purpose."""
from django.db.models import Q
from django.utils import timezone


def current_name(feed):
    from .models import KnowledgeBase
    return KnowledgeBase.objects.values_list('name',flat=True).get(pk=feed.kb_id)


def named_spec(spec,name):
    if not isinstance(spec,dict) or not spec:return spec
    if spec.get('kind')=='report':return {**spec,'title':spec.get('title') or name}
    if 'name' in spec or 'body_fields' in spec:return {**spec,'name':name}
    return spec


def sync_related_names(kb):
    """Called inside the KB save transaction; leave schedules and credentials intact."""
    from .models import KnowledgeFeed,FeedReportPlan,ConnectorDraft
    now=timezone.now()
    for feed in KnowledgeFeed.objects.filter(kb_id=kb.pk).only('pk','adapter_spec'):
        KnowledgeFeed.objects.filter(pk=feed.pk).update(name=kb.name,
            adapter_spec=named_spec(feed.adapter_spec,kb.name),updated_at=now)
    for plan in FeedReportPlan.objects.filter(feed__kb_id=kb.pk).only('pk','spec'):
        spec=named_spec(plan.spec,kb.name)
        if spec!=plan.spec:FeedReportPlan.objects.filter(pk=plan.pk).update(spec=spec,updated_at=now)
    for draft in ConnectorDraft.objects.filter(Q(kb_id=kb.pk)|Q(feed__kb_id=kb.pk)).only('pk','spec','preview'):
        spec=named_spec(draft.spec,kb.name);preview=draft.preview
        if isinstance(preview,dict) and 'source_feed' in preview:preview={**preview,'source_feed':kb.name}
        if spec!=draft.spec or preview!=draft.preview:
            ConnectorDraft.objects.filter(pk=draft.pk).update(spec=spec,preview=preview,updated_at=now)


def is_name_question(field):
    """Recognize task-name prompts without discarding column/field questions."""
    import re
    if field.get('key') in {'kb_name','knowledge_base_name','feed_name','source_name','data_source_name',
                            'report_name','report_title','schedule_name','report_schedule_name'}:return True
    label=str(field.get('label',''))
    if re.search(r'字段|列名|\b(?:field|column)\b',label,re.I):return False
    return bool(re.search(r'(?:知识库|数据源|报告|任务|计划)(?:的)?(?:名称|标题)|\b(?:knowledge\s*base|data\s*source|report|task|schedule)(?:\s+(?:plan|schedule))?\s+(?:name|title)\b',label,re.I))
