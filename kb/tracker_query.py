from __future__ import annotations

"""追踪表只读查询：数据库过滤/汇总、字段预览和单轮共享输出预算。"""

import json
import math
import threading
from datetime import date

from django.db.models import Avg, Case, Count, F, FloatField, Max, Min, Q, Sum, TextField, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Substr

from .models import KbTracker, KnowledgeBase, TrackerRow

RESPONSE_CHARS = 10000
TURN_CHARS = 20000
CELL_CHARS = 800
NUMERIC = r'^[-+]?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'


class TrackerText(KeyTextTransform):
    """SQLite 按字面字段名取标量，区分 JSON null/字符串并正确处理数字形字段名。"""
    def as_sqlite(self, compiler, connection):
        lhs, params, _ = self.preprocess_lhs(compiler, connection)
        return ("(SELECT CASE type WHEN 'null' THEN NULL WHEN 'true' THEN 'true' "
                "WHEN 'false' THEN 'false' ELSE CAST(value AS TEXT) END "
                f"FROM JSON_EACH({lhs}) WHERE key = %s LIMIT 1)", tuple(params)+(self.key_name,))


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def cell(value, limit=CELL_CHARS):
    if isinstance(value, float) and not math.isfinite(value):
        return {'value': None, 'warning': '非有限数字'}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = value if isinstance(value, str) else encode(value)
    if len(text) <= limit:
        return text
    return {'text': text[:limit], 'truncated': True, 'original_chars': len(text)}


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} 必须在 {low} 至 {high} 之间。')
    return value


class TrackerQuery:
    def __init__(self, allowed, evidence=None, relationship_scope=None, row_scope=None):
        self.allowed = allowed
        self.evidence = evidence if evidence is not None else []
        self.remaining = TURN_CHARS
        self.lock = threading.Lock()
        from .kb_links import RelationshipScope
        self.relationship_scope = relationship_scope or RelationshipScope(allowed)
        self.row_scope = row_scope

    def call(self, method, **params):
        """并行工具调用共用同一字符预算；不截断 JSON 或假装返回完整表格。"""
        with self.lock:
            if self.remaining < 1200:
                return self._budget_notice()
            cap = min(RESPONSE_CHARS, self.remaining)
            try:
                result = getattr(self, method)(cap=cap, **params)
                text = encode(result)
                if len(text) > cap:
                    raise ValueError('字段配置过长，请指定更少字段或更小的查询范围。')
                if method == 'lookup':
                    from .publication import digest
                    snapshot = {'kind': 'tracker', 'source': '追踪表数据库查询', 'text': text,
                                'query': params, 'response_chars': cap, 'result_digest': digest(text)}
                    if snapshot not in self.evidence:
                        self.evidence.append(snapshot)
                tables = self.tables(params.get('kb_slug', ''))
                if method == 'preview':
                    returned = {table['kb_slug'] for table in result['tables']}
                    tables = [table for table in tables if table.kb.slug in returned]
                elif result.get('rows'):
                    order = list(dict.fromkeys(row['kb_slug'] for row in result['rows']))
                    tables = sorted([table for table in tables if table.kb.slug in order], key=lambda table: order.index(table.kb.slug))
                # 导航说明不进入事实快照：修改关联不会使已经核实的表格数据失效。
                text = encode(self.relationship_scope.attach(result, [table.kb for table in tables], cap=cap,
                    establish=method == 'lookup' and result.get('total_matched', 0) > 0))
            except ValueError as exc:
                text = encode({'error': str(exc)[:400]})
            self.remaining -= len(text)
            return text

    def _budget_notice(self):
        """Caller holds lock; even exhaustion notices spend the remaining budget."""
        text = encode({'error': '本轮追踪表输出预算已用完，请基于已有结果回答或缩小范围后继续提问。',
                       'budget_exhausted': True})
        if len(text) > self.remaining:
            text = '{}' if self.remaining >= 2 else ''
        self.remaining -= len(text)
        return text

    def error(self, message):
        with self.lock:
            if self.remaining < 1200:
                return self._budget_notice()
            text = encode({'error': str(message)[:400]})
            self.remaining -= len(text)
            return text

    def tables(self, kb_slug):
        if not isinstance(kb_slug, str) or len(kb_slug) > 200:
            raise ValueError('kb_slug 格式无效。')
        tables = KbTracker.objects.exclude(fields=[]).select_related('kb__parent').order_by('pk')
        if kb_slug:
            kb = KnowledgeBase.objects.select_related('parent').filter(slug=kb_slug).first()
            if kb is None or not self.allowed(kb):
                raise ValueError(f'知识库「{kb_slug}」不存在。')
            own = tables.filter(kb=kb)
            if own.exists():
                tables = own
            else:
                tables = tables.filter(Q(kb__parent=kb) | Q(kb_id=kb.parent_id))
        return [table for table in tables if self.allowed(table.kb) and table.fields]

    def preview(self, kb_slug='', offset=0, field_offset=0, cap=RESPONSE_CHARS):
        integer(offset, 'offset', 0, 1000000)
        integer(field_offset, 'field_offset', 0, 10000)
        tables = self.tables(kb_slug)
        result = {'kind': 'tracker_preview', 'total_tables': len(tables), 'tables': [], 'next_offset': None,
                  'notice': '样例仅帮助理解字段，不代表全量数据。正式查询请使用 tracker_lookup；默认只查已完成记录。'}
        for table in tables[offset:offset+3]:
            rows = TrackerRow.objects.filter(tracker=table)
            stats = rows.aggregate(total=Count('pk'), completed=Count('pk', filter=Q(status='done')),
                                   data_from=Min('updated_at', filter=Q(status='done')),
                                   data_to=Max('updated_at', filter=Q(status='done')))
            sample = list(rows.filter(status='done').order_by('-updated_at', '-pk').values('id', 'values')[:2])
            fields = table.fields[field_offset:field_offset+40]
            info = {'kb_slug': table.kb.slug, 'name': table.kb.name, 'enabled': table.enabled,
                    'query_mode': 'table' if table.kb.table_query else 'tracker',
                    'row_count': stats['total'], 'completed_rows': stats['completed'], 'field_count': len(table.fields),
                    'data_from': stats['data_from'].isoformat() if stats['data_from'] else None,
                    'data_to': stats['data_to'].isoformat() if stats['data_to'] else None,
                    'fields': [{'name': f['label'], 'sample_types': sorted({type(row['values'][f['label']]).__name__
                        for row in sample if f['label'] in row['values']})} for f in fields],
                    'next_field_offset': field_offset+len(fields) if field_offset+len(fields)<len(table.fields) else None,
                    'samples': [{'row_id': row['id'], 'values': {f['label']: cell(row['values'].get(f['label']), 100)
                                 for f in fields[:6]}} for row in sample]}
            candidate = {**result, 'tables': result['tables']+[info]}
            if len(encode(candidate)) > cap-300:
                break
            result = candidate
        consumed = len(result['tables'])
        result['next_offset'] = offset+consumed if offset+consumed<len(tables) else None
        result['truncated'] = result['next_offset'] is not None
        if tables[offset:] and not consumed:
            raise ValueError('字段配置过长，请指定一个知识库或使用 field_offset 分段查看字段。')
        return result

    def lookup(self, kb_slug='', query='', columns=None, filters=None, order_by=None, metrics=None,
               group_by=None, limit=20, offset=0, status='done', cell_chars=CELL_CHARS, cap=RESPONSE_CHARS,
               _export=False, _matching_ids=False):
        integer(limit, 'limit', 1, 50000 if _export else 100)
        integer(offset, 'offset', 0, 1000000)
        integer(cell_chars, 'cell_chars', 80, 8000)
        if not isinstance(query, str) or len(query)>300:
            raise ValueError('query 最多300字符。')
        query = query.strip()
        if status not in ('done', 'proposed', 'pending', 'running', 'failed', 'all'):
            raise ValueError('status 无效，默认 done，仅检索已完成记录。')
        tables = self.tables(kb_slug)
        labels = list(dict.fromkeys(f['label'] for table in tables for f in table.fields))
        if not tables:
            raise ValueError('范围内没有追踪表，请先调用 tracker_preview。')
        rows = TrackerRow.objects.filter(tracker__in=tables)
        if self.row_scope is not None:
            rows = rows.filter(self.row_scope)
        if status != 'all':
            rows = rows.filter(status=status)
        aliases = {}

        def expression(field, kind='text'):
            nonlocal rows
            metadata = {'$row_id':'id', '$source_id':'external_id', '$updated_at':'updated_at',
                        '$status':'status', '$document_name':'document__original_name',
                        '$source_sheet':'source_sheet', '$source_row':'source_row'}
            if not isinstance(field,str) or field not in labels and field not in metadata:
                raise ValueError(f'未知字段「{str(field)[:100]}」，请先用 tracker_preview 查看实际字段。')
            if kind not in ('text', 'number', 'date'):
                raise ValueError('type 仅支持 text、number、date。')
            key = (field, kind)
            if key in aliases:
                return aliases[key]
            raw = f'tq_{len(aliases)}_raw'
            rows = rows.annotate(**{raw: Cast(F(metadata[field]),TextField()) if field in metadata else TrackerText(field, 'values')})
            alias = f'tq_{len(aliases)}_value'
            if kind == 'number':
                value = Case(When(**{raw+'__regex': NUMERIC}, then=Cast(F(raw), FloatField())), default=Value(None), output_field=FloatField())
            elif kind == 'date':
                value = Substr(F(raw), 1, 10)
            else:
                value = Cast(F(raw),TextField())
            rows = rows.annotate(**{alias: value})
            aliases[key] = alias
            return alias

        def scalar(value, kind):
            if kind == 'number':
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise ValueError('数值过滤需要有限数字。')
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError('数值过滤需要有限数字。')
            elif kind == 'date':
                if not isinstance(value, str) or date.fromisoformat(value).isoformat()!=value:
                    raise ValueError('日期过滤使用 YYYY-MM-DD。')
            elif not isinstance(value, (str, int, float, bool)) or len(str(value))>500:
                raise ValueError('文本过滤值过长或格式无效。')
            elif isinstance(value,bool):
                value = 'true' if value else 'false'
            return value

        if query.strip():
            if len(labels)>80:
                raise ValueError('跨库关键词查询涉及过多字段，请指定更小的 kb_slug 或使用 filters 精确筛选。')
            condition = Q(document__original_name__icontains=query) | Q(external_id__icontains=query)
            for label in labels:
                condition |= Q(**{expression(label)+'__icontains': query})
            rows = rows.filter(condition)
        if filters is not None and (not isinstance(filters, list) or len(filters)>12):
            raise ValueError('filters 最多12个条件，条件之间为 AND。')
        for rule in filters or []:
            if not isinstance(rule, dict) or not rule.keys()<={'field','op','value','type'}:
                raise ValueError('过滤条件使用 field/op/value/type。')
            op = rule.get('op', 'eq')
            kind = rule.get('type', 'number' if isinstance(rule.get('value'), (int, float)) and not isinstance(rule.get('value'), bool) else 'text')
            alias = expression(rule.get('field'), kind)
            value = rule.get('value')
            if op in ('in', 'between'):
                if not isinstance(value, list) or not 1<=len(value)<=50 or op=='between' and len(value)!=2:
                    raise ValueError('in 需要1至50个值，between 需要两个边界值。')
                values = [scalar(v, kind) for v in value]
                if op=='between' and values[0]>values[1]:
                    raise ValueError('between 的开始值不能大于结束值。')
                rows = rows.filter(**{alias+('__in' if op=='in' else '__range'): values})
            elif op=='is_empty':
                if not isinstance(value, bool):
                    raise ValueError('is_empty 的 value 必须为布尔值。')
                test = Q(**{alias+'__isnull':True}) | Q(**{alias:''}) if kind!='number' else Q(**{alias+'__isnull':True})
                rows = rows.filter(test if value else ~test)
            elif op in ('eq','ne','contains','gt','gte','lt','lte'):
                if op=='contains' and kind!='text':
                    raise ValueError('contains 仅用于文本。')
                test = Q(**{alias+'__'+{'eq':'exact','ne':'exact','contains':'icontains'}.get(op, op):scalar(value, kind)})
                rows = rows.filter(~test if op=='ne' else test)
            else:
                raise ValueError('op 支持 eq/ne/contains/in/between/gt/gte/lt/lte/is_empty。')
        if _matching_ids:
            # Internal SQL subquery for analysis filters. Never materialize IDs
            # or expose an unbounded row result to the model.
            return rows.order_by().values('pk')
        total = rows.count()
        applied = {'kb_slug':kb_slug, 'query':query, 'filters':filters or [], 'status':status,
                   'metrics':metrics or [], 'group_by':group_by or []}
        if metrics or group_by:
            return self.summary(expression, lambda: rows, total, metrics, group_by, limit, offset, cap, applied,
                                exporting=_export)
        if columns is None:
            columns = labels[:6]
            if '采集日期' in labels and '采集日期' not in columns:
                columns = columns+['采集日期']
        if not isinstance(columns, list) or not 1<=len(columns)<=12 or any(not isinstance(c,str) or c not in labels for c in columns):
            raise ValueError('columns 必须选择1至12个实际字段。')
        if order_by is not None and (not isinstance(order_by, list) or len(order_by)>3):
            raise ValueError('order_by 最多3个排序字段。')
        ordering = []
        for rule in order_by or []:
            if not isinstance(rule, dict) or not rule.keys()<={'field','type','direction'} or rule.get('direction','asc') not in ('asc','desc'):
                raise ValueError('排序使用 field、type 和 direction（asc/desc）。')
            alias = expression(rule.get('field'), rule.get('type','text'))
            ordering.append(F(alias).desc(nulls_last=True) if rule.get('direction')=='desc' else F(alias).asc(nulls_last=True))
        rows = rows.order_by(*(ordering or ['-updated_at']), '-pk')
        result = {'kind':'tracker_rows', 'applied_query':applied, 'total_matched':total, 'columns':columns, 'unselected_columns':len(set(labels)-set(columns)),
                  'offset':offset, 'rows':[], 'has_more':False, 'next_offset':None,
                  'notice':'只返回本页；truncated 标记表示单元格省略，不能据此声称拿到完整内容。待确认记录不是已批准事实。'}
        for row in rows.values('id','tracker__kb__slug','status','updated_at','external_id','document__original_name','source_sheet','source_row','values')[offset:offset+limit]:
            record = {'row_id':row['id'], 'kb_slug':row['tracker__kb__slug'], 'status':row['status'],
                      'updated_at':row['updated_at'].isoformat(), 'source_id':cell(row['external_id'],200),
                      'document_name':cell(row['document__original_name'] or '',200),
                      'source_sheet':row['source_sheet'], 'source_row':row['source_row'],
                      'values':{column:cell(row['values'].get(column), cell_chars) for column in columns}}
            if len(encode({**result, 'rows':result['rows']+[record]}))>cap-300:
                break
            result['rows'].append(record)
        consumed = len(result['rows'])
        result['has_more'] = offset+consumed<total
        result['next_offset'] = offset+consumed if consumed and result['has_more'] else None
        if not consumed and offset<total:
            result['notice'] = '本页字段过长，未返回记录；请选择更少 columns，或使用 metrics 在数据库统计。'
        return result

    def summary(self, expression, get_rows, total, metrics, group_by, limit, offset, cap, applied, *, exporting=False):
        """所有过滤后记录参与数据库汇总，分页仅限制返回的分组，不影响统计。"""
        metrics = metrics or [{'op':'count','as':'count'}]
        applied = {**applied,'metrics':metrics}
        group_by = group_by or []
        if not isinstance(metrics,list) or not 1<=len(metrics)<=6 or not isinstance(group_by,list) or len(group_by)>3:
            raise ValueError('metrics 支持1至6项，group_by 最多3项。')
        aggregates = {}; names = {}; groups = {}; numeric = {}
        for i, rule in enumerate(metrics):
            if not isinstance(rule,dict) or not rule.keys()<={'op','field','as'}:
                raise ValueError('metrics 使用 op/field/as。')
            op = rule.get('op'); name = rule.get('as', op)
            if not isinstance(name,str) or not 1<=len(name)<=80 or name in names.values():
                raise ValueError('统计结果名称需要唯一，最多80字符。')
            alias = f'metric_{i}'; names[alias] = name
            if op=='count' and not rule.get('field'):
                aggregates[alias] = Count('pk')
            elif op=='distinct_count':
                field = expression(rule.get('field'))
                aggregates[alias] = Count(field, distinct=True, filter=~Q(**{field:''}))
            elif op in ('sum','avg','min','max'):
                field = expression(rule.get('field'), 'number')
                aggregates[alias] = {'sum':Sum,'avg':Avg,'min':Min,'max':Max}[op](field)
                numeric[alias+'_valid'] = Count(field)
            else:
                raise ValueError('统计支持 count、distinct_count、sum、avg、min、max。')
        for i, rule in enumerate(group_by):
            if not isinstance(rule,dict) or not rule.keys()<={'field','period'} or rule.get('period','value') not in ('value','day','month','year'):
                raise ValueError('group_by 使用 field 和 period（value/day/month/year）。')
            if rule.get('field') in [label for label,_ in groups.values()]:
                raise ValueError('group_by 不要重复同一字段；按 month 分组的结果已包含年份。')
            field = expression(rule.get('field'), 'date' if rule.get('period','value')!='value' else 'text')
            groups[f'group_{i}'] = (rule['field'], Substr(F(field),1,{'day':10,'month':7,'year':4}[rule['period']])
                                  if rule.get('period','value')!='value' else F(field))
        rows = get_rows()
        result = {'kind':'tracker_summary', 'applied_query':applied, 'total_matched':total, 'offset':offset, 'groups':[],
                  'total_groups':1, 'has_more':False, 'next_offset':None,
                  'notice':'统计覆盖所有匹配记录；数值统计排除空值和非数值，valid_numeric_values 给出各指标实际参与条数。'}
        if not groups:
            value = rows.aggregate(**aggregates, **numeric)
            result['groups'] = [{'metrics':{name:cell(value[key]) for key,name in names.items()},
                                 'valid_numeric_values':{names[key[:-6]]:value[key] for key in numeric}}]
            return result
        grouped = rows.order_by().annotate(**{key:val[1] for key,val in groups.items()}).values(*groups).annotate(**aggregates,**numeric).order_by(*groups)
        result['total_groups'] = grouped.count()
        used = 0
        for value in grouped[offset:offset+limit].iterator(chunk_size=1000):
            record = {'by':{label:value[key] if exporting else cell(value[key]) for key,(label,_) in groups.items()},
                      'metrics':{name:cell(value[key]) for key,name in names.items()},
                      'valid_numeric_values':{names[key[:-6]]:value[key] for key in numeric}}
            # Full exports use a linear size check and preserve group labels.
            # Interactive/tool responses retain their small, paginated budget.
            used += len(encode(record))
            if exporting and used > cap:
                raise ValueError('导出数据过大，请缩小日期范围或减少分组。')
            if not exporting and len(encode({**result,'groups':result['groups']+[record]}))>cap-300:
                break
            result['groups'].append(record)
        consumed = len(result['groups'])
        result['has_more'] = offset+consumed<result['total_groups']
        result['next_offset'] = offset+consumed if consumed and result['has_more'] else None
        return result


def validate_snapshot(snapshot, user):
    """发布前在当前权限下重放相同只读查询；数据/统计/权限变化时拒绝旧结果。"""
    from .access import kb_accessible
    from .publication import digest
    if not getattr(user,'is_active',False) or not isinstance(snapshot, dict) or snapshot.get('kind')!='tracker':
        return False
    if 'analysis_query' in snapshot:
        from .analysis_exports import validate_analysis_snapshot
        return validate_analysis_snapshot(snapshot, user)
    params = snapshot.get('query')
    cap = snapshot.get('response_chars')
    text = snapshot.get('text')
    if (not isinstance(params,dict) or not params.get('kb_slug') or
            isinstance(cap,bool) or not isinstance(cap,int) or not 1200<=cap<=RESPONSE_CHARS or
            not isinstance(text,str) or len(text)>cap or digest(text)!=snapshot.get('result_digest')):
        return False
    try:
        result = TrackerQuery(lambda kb:kb_accessible(user,kb)).lookup(cap=cap, **params)
        return digest(encode(result))==snapshot['result_digest']
    except (ValueError, TypeError, OverflowError):
        return False
