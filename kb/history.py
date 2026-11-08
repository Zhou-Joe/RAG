"""Durable conversation messages, including tool calls and their results."""


def recent_history(messages, *, enhanced=False, visible, max_chars=None, max_pairs=None):
    """Bootstrap older chats that do not yet have a complete model transcript."""
    pairs = []
    question = None
    for item in messages:
        if item.role == 'user':
            question = item.content
            continue
        if item.role != 'ai' or question is None:
            continue
        if getattr(item, "completion_status", "complete") != "complete":
            question = None
            continue
        if item.citations and not visible(item.citations):
            question = None
            pairs = []
            continue
        pairs.append((question, item.content))
        question = None
    chosen = []
    size = 0
    for question, answer in reversed(pairs[-max_pairs:] if max_pairs else pairs):
        length = len(question) + len(answer)
        if max_chars is not None and size + length > max_chars:
            break  # Do not truncate facts halfway through a sentence/table row.
        chosen.insert(0, [{'role':'user','content':question}, {'role':'assistant','content':answer}])
        size += length
    return [message for pair in chosen for message in pair]


def load_context(conversation, user):
    """Reuse the exact transcript only while its source material remains visible."""
    from .publication import visible_citations
    context = conversation.agent_context
    if not isinstance(context, dict) or context.get('version') != 1:
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
        for item in context.get('rag', {}).get('candidates', {}).values():
            add_document(item['snapshot'])
        for snapshot in context.get('evidence', []):
            if snapshot.get('kind') == 'graph':
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
