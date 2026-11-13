from __future__ import annotations

"""向模型提供必填配置结构，避免把浏览器动作误当完整连接器。"""

def calculation_schema():
    from .connector_calculations import ARITY
    return {'type':'object','required':['op'],'description':'可嵌套业务计算。args是字段引用{"field":"路径"}、常量或嵌套表达式。计算值不要从网页CSS提取。',
        'properties':{'op':{'type':'string','enum':list(ARITY)},'args':{'type':'array','maxItems':20,'items':{},'description':'例如count: [{"field":"show_times"}]；multiply: [{"field":"qty"},{"field":"price"}]；if: [条件,真值,假值]。'},
            'start':{'type':'string'},'end':{'type':'string'},'format':{'type':'string','enum':['hhmm','iso8601','unix_seconds','unix_millis']},'unit':{'type':'string','enum':['seconds','minutes','hours','days']},
            'pattern':{'type':'string'},'group':{'type':'integer','minimum':0,'maximum':8},'separator':{'type':'string'},'distinct':{'type':'boolean'},'digits':{'type':'integer','minimum':0,'maximum':8},
            'path':{'type':'string','description':'仅group_by：每条子记录的分组路径。'},'aggregate':{'type':'object','description':'在每组数组上执行的嵌套表达式；{"field":""}指当前组数组。'},'part':{'type':'string','enum':['date','year','month','day','weekday','hour']}}}


def digest_chart_schema():
    """A declarative data binding; render code and values remain server-owned."""
    return {'type': 'array', 'maxItems': 8,
        'description': 'Optional field-bound visualizations. Omit for an automatic overview: one numeric result uses a value card, otherwise four chart views. [] disables visualizations. Never supply raw data, code, HTML, URLs or callbacks.',
        'items': {'type': 'object', 'required': ['title', 'type', 'x', 'series'], 'additionalProperties': False,
            'properties': {
                'title': {'type': 'string', 'maxLength': 120},
                'type': {'type': 'string', 'enum': ['metric', 'line', 'bar', 'pie', 'combo', 'area', 'scatter', 'doughnut']},
                'x': {'anyOf': [{'type': 'string'}, {'type': 'null'}],
                    'description': 'Actual final report output column, or null for source row order. Metric value cards require null and aggregate the entire final result.'},
                'series': {'type': 'array', 'minItems': 1, 'maxItems': 6,
                    'description': 'Use exactly one series for metric/pie/doughnut. Metric displays one title and highlighted aggregate value, without grouping. Combo needs bar and line series. Values are calculated over all selected rows.',
                    'items': {'type': 'object', 'required': ['aggregate'], 'additionalProperties': False,
                        'properties': {
                            'field': {'type': 'string', 'description': 'Actual final report output column; omit only for row-count aggregate.'},
                            'aggregate': {'type': 'string', 'enum': ['count', 'sum', 'avg', 'min', 'max']},
                            'label': {'type': 'string', 'maxLength': 80},
                            'type': {'type': 'string', 'enum': ['line', 'bar']},
                            'axis': {'type': 'string', 'enum': ['left', 'right']}}}},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 30},
                'sort': {'type': 'string', 'enum': ['source', 'label', 'value_desc']},
                'options': {'type': 'object', 'additionalProperties': False, 'properties': {
                    'legend': {'type': 'boolean', 'default': True},
                    'stacked': {'type': 'boolean', 'default': False},
                    'horizontal': {'type': 'boolean', 'default': False},
                    'smooth': {'type': 'boolean', 'default': False},
                    'y_label': {'type': 'string', 'maxLength': 80},
                    'right_y_label': {'type': 'string', 'maxLength': 80}}}}}}


def digest_schema():
    return {'type': 'object', 'description': 'Optional LLM Digest only when explicitly requested or deterministic processing cannot express the requested explanation. Use report aggregates and comma-joined Teams fields for lists/statistics without a Digest.',
        'properties': {
            'enabled': {'type': 'boolean'},
            'instructions': {'type': 'string', 'maxLength': 2000, 'description': 'User-requested focus, comparisons and presentation; never instructions to fetch external data.'},
            'language': {'type': 'string', 'enum': ['en', 'zh-hans']},
            'include_source_links': {'type': 'boolean', 'default': False,
                'description': 'Add clickable source links extracted deterministically from selected report fields; never invent URLs.'},
            'email_format': {'type': 'string', 'enum': ['body', 'html_link', 'html_attachment'], 'default': 'body',
                'description': 'Delivery presentation: body keeps the digest in the message; html_link sends a generated HTML dashboard link in email or Teams; html_attachment is email only. Never provide raw HTML or invent the report URL.'},
            'charts': digest_chart_schema(),
            'group_by': {'type': 'array', 'maxItems': 2, 'uniqueItems': True, 'items': {'type': 'string'}, 'description': 'Actual final report output column names.'},
            'metrics': {'type': 'array', 'maxItems': 12, 'items': {'type': 'object', 'required': ['op', 'label'],
                'properties': {'op': {'type': 'string', 'enum': ['count', 'sum', 'avg', 'min', 'max', 'distinct_count']},
                    'field': {'type': 'string', 'description': 'Actual final report output column; may be omitted for row count.'},
                    'label': {'type': 'string', 'minLength': 1, 'maxLength': 80}}}}}}


def normalize_spec(spec):
    """兼容工具常见的确定性参数错位，不猜测任何网页选择器或业务字段。"""
    from copy import deepcopy
    value=deepcopy(spec)
    if not isinstance(value,dict):return value
    browser=value.get('browser')
    if isinstance(browser,dict):
        value.setdefault('source_type','browser');value.setdefault('records_path','records')
        output=browser.get('output')
        if isinstance(output,dict) and 'max_records' in output:
            if 'max_records' in browser and browser['max_records']!=output['max_records']:raise ValueError('browser.max_records与browser.output.max_records冲突。')
            browser['max_records']=output.pop('max_records')
    return value

def model_tools(tools):
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from .browser_runtime import MAX_RECORDS, MAX_PAGES
    result=[]
    for tool in tools:
        schema=convert_to_openai_tool(tool)
        props=schema['function']['parameters'].get('properties',{})
        if 'spec' in props:
            props['spec']={'type':'object','required':['name','url','id_paths','body_fields'],
                'description':'完整配置，保留已有正确字段。网页 source_type=browser，必须包含 browser.output；不能只提交动作。',
                'properties':{'name':{'type':'string'},'url':{'type':'string'},'source_type':{'enum':['api','browser'],'type':'string'},
                    'ingest_mode':{'type':'string','enum':['tracker','documents'],'description':'默认 tracker 直接写追踪表；documents 将每条采集记录作为文档入库，供后续追踪表 AI 抽取长文本。'},
                    'records_path':{'type':'string','description':'从 probe_api.record_arrays 选真实数组的 records_path；根数组为 ""，可用 JSON Pointer。'},
                    'single_record':{'type':'boolean','description':'只有真实响应在 records_path 处是单个对象时才设 true。'},
                    'query':{'type':'object','description':'只填用户或上游文档确认的 GET 查询参数；变更后重新 probe_api。'},
                    'title_path':{'type':'string','description':'相对单条记录的已观察字段路径。'},
                    'id_paths':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'string'},'description':'相对单条记录的真实稳定 ID 路径，从 probe_api.item_fields 选择。'},
                    'body_fields':{'type':'array','minItems':1,'items':{'type':'object','required':['label'],'properties':{'label':{'type':'string'},'path':{'type':'string','description':'相对单条记录的已观察路径；不要猜 API 字段。'},'computed':{'type':'object','required':['op','start','end'],'properties':{'op':{'enum':['duration']},'start':{'type':'string'},'end':{'type':'string'},'format':{'enum':['hhmm','iso8601','unix_seconds','unix_millis']},'unit':{'enum':['hours','minutes','seconds']}}}}}},
                    'next_cursor_path':{'type':'string','description':'仅上游响应中确有游标时填写观察到的路径。'},
                    'cursor_param':{'type':'string','description':'仅上游文档确认如何请求下一页时填写参数名。'},
                    'browser':{'type':'object','required':['output'],'properties':{'actions':{'type':'array','items':{'type':'object'}},'output':{'type':'object','description':'dom 必须有 rows 和 fields；network 必须有 url_contains 和 path','properties':{'kind':{'enum':['dom','network']},'rows':{'type':'string'},'fields':{'type':'object'},'detail':{'type':'object'},'url_contains':{'type':'string'},'path':{'type':'string'}}}}}}}
            field_props=props['spec']['properties']['body_fields']['items']['properties']
            field_props['computed']=calculation_schema()
            field_props['unit']={'type':'string','description':'结果的显示单位，例如元、场、分钟；属于body_fields字段，不是计算规则参数。'}
            field_props['required_unless']={'type':'object','required':['path','regex'],'properties':{'path':{'type':'string'},'regex':{'type':'string'}},'description':'只在真实来源状态明确符合此条件时允许字段不适用；不能掩盖正常营业项目的缺失值。'}
            field_props['required']={'type':'boolean'}
            field_props['source']={'type':'string','enum':['today','now'],'description':'由系统生成采集日期或采集时间，所有模式都支持；不从网站取值，不再询问日期来源。'}
            props['spec']['properties']['on_error']={'type':'string','enum':['raw','fail'],'default':'raw','description':'raw保留真实原文与解析提示；fail才对格式问题失败。'}
            props['spec']['properties']['write_mode']={'type':'string','enum':['append','upsert'],'default':'append','description':'默认append：每次采集追加新行，保留历史，适合运营时间、价格、指标、同比环比。仅用户明确只要最新状态/覆盖时用upsert，按稳定ID更新；切换不删除历史。'}
            props['spec']['properties']['url']['description']='API 使用已探测的准确 GET 端点；浏览器使用真实起始 URL 并保留单页应用的完整 #/... 路由。'
            props['spec']['properties']['browser']['properties']['output']['description']='network读取实际JSON数组，支持fields注入today/now及detail.url_field逐条取详情；dom通过rows和fields取数。'
            detail=props['spec']['properties']['browser']['properties']['output']['properties']['detail']
            detail['properties']={'url_field':{'type':'string'},'click_selector':{'type':'string'},'fields':{'type':'object'},'render':{'type':'string','enum':['browser','html'],'description':'已确认HTML有完整数据时选html；动态数据用browser。'}}
            detail['properties']['render']={'type':'string','enum':['auto','html','browser'],'default':'auto','description':'auto先读原始HTML，缺数据才加载脚本；html强制只读HTML；browser用于已确认依赖JS的页面。'}
            detail['properties']['actions']={'type':'array','items':{'type':'object'}}
            browser=props['spec']['properties']['browser']['properties']
            browser['max_records']={'type':'integer','minimum':1,'maximum':MAX_RECORDS,'description':'放在browser内，与output平级，不能放进output。首次拉取历史时按用户需要放宽，最多50000条；不能以较小条数冒充已取全。'}
            output=browser['output']['properties']
            output['max_pages']={'type':'integer','minimum':1,'maximum':MAX_PAGES};output['next_selector']={'type':'string'}
            mapping={'type':'object','additionalProperties':{'type':'object','properties':{'selector':{'type':'string'},'attribute':{'type':'string'},'regex':{'type':'string'},'group':{'type':'integer'},'all':{'type':'boolean'},'required':{'type':'boolean'},'source':{'type':'string','enum':['url','today','now']},'prefix':{'type':'string'},'default':{'type':'string'},'path':{'type':'string'}}}}
            output['fields']=mapping;detail['properties']['fields']=mapping
        if 'computed' in props:props['computed']=calculation_schema()
        if 'digest' in props:props['digest']=digest_schema()
        if 'report' in props and props['report'].get('type') == 'object':
            report_props=props['report'].setdefault('properties', {})
            report_props['title']={'type':'string','minLength':1,'maxLength':120,
                'description':'Concise, purpose-specific email subject / Teams heading, independent of the KB name. Infer it from the delivery request unless the user supplies a title.'}
            report_props['teams_card']={'type':'object','additionalProperties':False,'required':['fields'],
                'properties':{'fields':{'type':'array','minItems':1,'maxItems':3,'uniqueItems':True,
                    'items':{'type':'string'},'description':'Select up to three final output columns. The system comma-joins every matching item in row order. Use report aggregates first for statistical requests.'}}}
            report_props['digest']=digest_schema()
        result.append(schema)
    return result


def configuration_error(spec,error):
    missing=[]
    if isinstance(spec,dict):
        missing=[key for key in ('name','url','id_paths','body_fields') if not spec.get(key)]
        if spec.get('source_type')=='browser' and (not isinstance(spec.get('browser'),dict) or not isinstance(spec['browser'].get('output'),dict)):missing.append('browser.output')
    else:missing=['spec（完整对象）']
    resource='采集上限' in str(error)
    return {'error':str(error)[:1000],'missing_fields':missing,'error_type':'resource_budget' if resource else 'configuration_or_capture',
        'correction':('详情使用render:auto或html，避免每条加载整套脚本；不改小记录数来掩盖未完成采集。保留已正确的字段与选择器。' if resource else '按报错路径修正，max_records在browser内与output平级。browser.output需kind及dom(rows,fields)或network(url_contains,path)。计算字段放body_fields.computed，不放网页fields。不要重复原参数。')}
