"""Durable conversation messages, including tool calls and their results."""


def field_visibility_scope(user):
    """Server-only visibility stamp; never add the stored schema to model input.

    Preview tool results are not published evidence, so citation validation alone
    cannot detect an older transcript that still contains now-hidden fields.
    An explicit selection change makes that transcript require a fresh query.
    """
    from django.db.models import Q
    from .access import kb_q
    from .models import KnowledgeBase
    if user is not None and not hasattr(user, 'is_authenticated'):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(pk=user, is_active=True).first()
    if user is None:
        return {}
    rows = KnowledgeBase.objects.filter(kb_q(user)).filter(
        Q(category='business') | Q(table_query=True), active_fields__isnull=False,
    ).order_by('slug').values_list('slug', 'active_fields')
    return {slug: fields for slug, fields in rows}


def field_visibility_current(context, user, *, scope=None):
    """Legacy history is reusable only when no field selection can hide data."""
    current = field_visibility_scope(user) if scope is None else scope
    saved = context.get('field_visibility') if isinstance(context, dict) else None
    return not current if saved is None else isinstance(saved, dict) and saved == current


def recent_history(messages, *, enhanced=False, visible, max_chars=None, max_pairs=None):
    """Bootstrap older chats that do not yet have a complete model transcript."""
    pairs = []
    question = None
    question_message = None
    for item in messages:
        if item.role == 'user':
            question = item.content
            question_message = {'role': 'user', 'content': question}
            if hasattr(item, 'attachments'):
                ids = [str(attachment.pk) for attachment in item.attachments.all()]
                if ids:
                    question_message['additional_kwargs'] = {'chat_attachment_ids': ids}
            continue
        if item.role != 'ai' or question is None:
            continue
        if getattr(item, "completion_status", "complete") != "complete":
            if question_message.get('additional_kwargs'):
                # Retry a failed turn with its user-provided source, without
                # replaying the failed assistant draft or any tool actions.
                pairs.append((question, None, question_message))
            question = None
            question_message = None
            continue
        if item.citations and not visible(item.citations):
            question = None
            question_message = None
            pairs = []
            continue
        pairs.append((question, item.content, question_message))
        question = None
        question_message = None
    if question_message and question_message.get('additional_kwargs'):
        pairs.append((question, None, question_message))
    chosen = []
    size = 0
    for question, answer, question_message in reversed(pairs[-max_pairs:] if max_pairs else pairs):
        length = len(question) + len(answer or '')
        if max_chars is not None and size + length > max_chars:
            break  # Do not truncate facts halfway through a sentence/table row.
        chosen.insert(0, [question_message] + ([{'role':'assistant','content':answer}] if answer is not None else []))
        size += length
    return [message for pair in chosen for message in pair]


def load_context(conversation, user, *, field_scope=None):
    """Reuse the exact transcript only while its source material remains visible."""
    from .publication import visible_citations
    context = conversation.agent_context
    if not isinstance(context, dict) or context.get('version') != 1:
        return {}
    if not field_visibility_current(context, user, scope=field_scope):
        return {}
    from langchain_core.messages import messages_from_dict
    try:
        messages_from_dict(context.get('messages', []))
        sources = list(context.get('citations', []))
        checked_documents = {(str(c.get('doc_id')), c.get('document_digest')) for c in sources if c.get('doc_id')}
        def add_document(snapshot):
            identity = (str(snapshot['doc_id']), snapshot['document_digest'])
            if identity not in checked_documents:
                sources.append({'doc_id': identity[0], 'document_digest': identity[1]})
                checked_documents.add(identity)
        for cid,item in context.get('rag', {}).get('candidates', {}).items():
            snapshot=item['snapshot']
            if snapshot.get('kind')=='image' and cid in context.get('rag',{}).get('loaded',[]) and not snapshot.get('image_digest'):return {}
            if snapshot.get('kind')=='image' and snapshot.get('image_digest'):
                from .publication import image_snapshot_current
                if not image_snapshot_current(snapshot,user):return {}
            add_document(snapshot)
        for snapshot in context.get('evidence', []):
            if snapshot.get('kind') == 'chat_attachment':
                from .chat_attachments import snapshot_current
                if str(snapshot.get('conversation_id')) != str(conversation.pk) or not snapshot_current(snapshot, user.pk):
                    return {}
            elif snapshot.get('kind') == 'graph':
                sources.append({'kind': 'graph', 'relation_id': snapshot['relation_id'],
                                'fact_digest': snapshot['fact_digest'], 'graph_snapshot': snapshot})
            elif snapshot.get('kind') == 'tracker':
                sources.append({'kind': 'tracker', 'result_digest': snapshot['result_digest'],
                                'kb_slug': snapshot['query']['kb_slug'], 'tracker_snapshot': snapshot})
            elif snapshot.get('doc_id'):
                add_document(snapshot)
        if not visible_citations(sources, user):
            return {}
    except (KeyError, ValueError, TypeError):
        return {}
    return context


def conversation_history(conversation, user, *, enhanced=False):
    """Choose model context without reviving prose from a stale field policy."""
    from .publication import visible_citations
    scope = field_visibility_scope(user)
    context = load_context(conversation, user, field_scope=scope)
    if context or not field_visibility_current(conversation.agent_context, user, scope=scope):
        return context, []
    published = recent_history(
        conversation.messages.prefetch_related('attachments').order_by('created_at', 'pk'),
        enhanced=enhanced, visible=lambda citations: visible_citations(citations, user),
    )
    return context, published


def remove_legacy_kb_defaults(messages):
    """Remove only the old injected first line, preserving user text and tool data."""
    import re
    from langchain_core.messages import HumanMessage
    prefix=re.compile(r'^【当前默认知识库：[^\r\n】]*】\r?\n')
    cleaned=[]
    for message in messages:
        if isinstance(message,HumanMessage) and isinstance(message.content,str):
            text=prefix.sub('',message.content,count=1)
            if text!=message.content:message=message.model_copy(update={'content':text})
        cleaned.append(message)
    return cleaned
