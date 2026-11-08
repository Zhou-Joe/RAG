from django.db import migrations
from django.db.models import Q


def align_names(apps,schema_editor):
    KB=apps.get_model('kb','KnowledgeBase')
    Feed=apps.get_model('kb','KnowledgeFeed')
    Plan=apps.get_model('kb','FeedReportPlan')
    Draft=apps.get_model('kb','ConnectorDraft')
    alias=schema_editor.connection.alias
    def named(spec,name):
        if not isinstance(spec,dict) or not spec:return spec
        if spec.get('kind')=='report':return {**spec,'title':name}
        if 'name' in spec or 'body_fields' in spec:return {**spec,'name':name}
        return spec
    for kb in KB.objects.using(alias).filter(feeds__isnull=False).distinct().iterator():
        for feed in Feed.objects.using(alias).filter(kb_id=kb.pk).iterator():
            Feed.objects.using(alias).filter(pk=feed.pk).update(name=kb.name,adapter_spec=named(feed.adapter_spec,kb.name))
        for plan in Plan.objects.using(alias).filter(feed__kb_id=kb.pk).iterator():
            Plan.objects.using(alias).filter(pk=plan.pk).update(spec=named(plan.spec,kb.name))
        for draft in Draft.objects.using(alias).filter(Q(kb_id=kb.pk)|Q(feed__kb_id=kb.pk)).iterator():
            preview=draft.preview
            if isinstance(preview,dict) and 'source_feed' in preview:preview={**preview,'source_feed':kb.name}
            Draft.objects.using(alias).filter(pk=draft.pk).update(spec=named(draft.spec,kb.name),preview=preview)


class Migration(migrations.Migration):
    dependencies=[('kb','0053_unscoped_conversations')]
    operations=[migrations.RunPython(align_names,migrations.RunPython.noop)]
