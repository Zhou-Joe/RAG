"""Selected KB fields bound both the interface and EnGenie; stored originals remain intact."""
from __future__ import annotations


def available_fields(tracker) -> list[str]:
    """返回去重后的抽取结构标签，保持原始顺序。"""
    return list(dict.fromkeys(field['label'] for field in (tracker.fields if tracker else [])
                             if isinstance(field, dict) and isinstance(field.get('label'), str)))


def field_selection_owner(kb):
    """Document children inherit their business library's field selection."""
    parent = kb.parent if kb.parent_id else None
    if (parent and not kb.is_folder and (parent.category == 'business' or parent.table_query)
            and parent.active_fields is not None):
        return parent
    if kb.category == 'business' or kb.table_query:
        return kb
    return parent if parent and not kb.is_folder and (parent.category == 'business' or parent.table_query) else None


def visible_fields(kb, tracker) -> list[str]:
    """默认展示全量；显式选择不随下一次采集自动扩展。"""
    labels = available_fields(tracker)
    owner = field_selection_owner(kb)
    selected = owner.active_fields if owner else None
    own_selected = kb.active_fields if owner and owner.pk != kb.pk and (kb.category == 'business' or kb.table_query) else None
    return [label for label in labels if (selected is None or label in selected)
            and (own_selected is None or label in own_selected)]


def field_options(kb, tracker) -> list[dict]:
    """编辑器总能列出隐藏字段，供管理员重新启用。"""
    selected = set(visible_fields(kb, tracker))
    return [{'label': label, 'selected': label in selected} for label in available_fields(tracker)]


def validate_selection(value, tracker) -> list[str] | None:
    """校验展示白名单；不接受未知字段或空的显式选择。"""
    if value is None:
        return None
    labels = available_fields(tracker)
    if (not isinstance(value, list) or not value or len(value) > len(labels)
            or any(not isinstance(label, str) or label not in labels for label in value)
            or len(set(value)) != len(value)):
        raise ValueError('请至少选择一个已有字段，或展示全部字段。')
    return [label for label in labels if label in value]
