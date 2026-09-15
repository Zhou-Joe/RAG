"""Bounded query understanding shared by the enhanced answer flow."""
from dataclasses import dataclass, asdict
import json
import re


@dataclass(frozen=True)
class QueryPlan:
    original: str
    standalone: str
    action: str
    output: str
    subquestions: list[str]
    identifiers: list[str]

    def prompt(self):
        return json.dumps(asdict(self), ensure_ascii=False)


def parse_plan(text, original):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get('action') not in ('new', 'follow_up', 'format') or data.get('output') not in ('text','table'):
        return None
    question = data.get('standalone')
    subquestions = data.get('subquestions', [])
    if not isinstance(question, str) or not question.strip() or len(question)>1000:
        return None
    if not isinstance(subquestions,list) or len(subquestions)>3 or any(not isinstance(q,str) or not q.strip() or len(q)>600 for q in subquestions):
        return None
    identifiers = re.findall(r'(?<!\w)[A-Za-z0-9]*\d[A-Za-z0-9_.-]*(?!\w)', original)
    if any(identifier not in question for identifier in identifiers):
        return None
    return QueryPlan(original,question,data['action'],data['output'],subquestions,identifiers)
