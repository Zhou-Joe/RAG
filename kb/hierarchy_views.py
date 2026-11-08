from __future__ import annotations

"""对象属性树的只读浏览与逐条审核；权限以每条内容的来源库为准。"""

from itertools import islice

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from . import access
from .knowledge_hierarchy import (assertion_current, assertion_queryset, literal_text,
                                 path_label, review_assertion, value_text, visible_assertions, display_text, value_preview)
from .models import KnowledgeBase, KnowledgeNode


def libraries(user):
    rows=KnowledgeBase.objects.all()
    return rows.filter(access.kb_q(user,admin_all=True))


def scopes(request):
    allowed=libraries(request.user)
    kb=get_object_or_404(allowed,slug=request.GET.get('kb') or request.POST.get('kb',''))
    return kb, list(allowed.filter(Q(pk=kb.pk)|Q(parent=kb)).values_list('slug',flat=True))


def integer(value, label):
    if not value or not value.isascii() or not value.isdecimal() or not 0<int(value)<=9223372036854775807:
        raise ValueError('无效的'+label+'。')
    return int(value)


def value_tree(value, name=''):
    labels={'all_of':'同时满足','any_of':'满足任一项','not':'不满足','except':'例外',
            'lower':'下限','upper':'上限','lower_inclusive':'包含下限','upper_inclusive':'包含上限',
            'method':'检测方法','standard':'适用标准','version':'版本','variant':'适用情形',
            'parameter':'参数','operator':'比较关系','value':'取值','unit':'单位','material':'材料',
            'source_notes':'相关原文说明（保留其适用范围）','definitions':'关联判定依据'}
    if isinstance(value,dict):
        return {'name':name,'children':[value_tree(item,labels.get(key,key)) for key,item in value.items()]}
    if isinstance(value,list):
        return {'name':name,'children':[value_tree(item) for item in value]}
    return {'name':name,'text':display_text(literal_text(value))}


def assertion_card(item, user):
    current=assertion_current(item)
    evidence=[]
    from .keyword_index import get_chunks
    doc=item.document
    chunks=get_chunks(doc.kb.slug,doc.original_name) if current and doc.__class__.objects.filter(kb=doc.kb,original_name=doc.original_name).count()==1 else []
    if chunks:
        from .provenance import annotate_results
        annotate_results(chunks)
    for source in item.evidence.all():
        # Repeated table headers may occur on unrelated pages. Keep the exact
        # source identity that passed validation instead of matching every copy.
        source_ids={part.get('chunk_id') for part in source.chunks if isinstance(part,dict)}
        matching=[chunk for chunk in chunks if chunk.get('chunk_id') and source.quote in chunk.get('text','')]
        if source_ids:
            matching=[chunk for chunk in matching if chunk['chunk_id'] in source_ids]
        elif len(matching)!=1:
            matching=[]  # Old evidence without a unique identity cannot locate safely.
        locations=[{'chunk_id':chunk['chunk_id'],'page':chunk.get('page_label','查看原文圈注')}
                   for chunk in matching][:4]
        evidence.append({'quote':source.quote,'document':source.document.original_name,'locations':locations})
    def rule_display(value):
        if isinstance(value,dict):return {key:rule_display(child) for key,child in value.items()}
        if isinstance(value,list):return [rule_display(child) for child in value]
        return display_text(value) if isinstance(value,str) else value
    return {'assertion':item,'path':path_label(item),'current':current,
            'rule':rule_display(item.value['content']) if item.value['type']=='rule' else None,
            'value':value_tree(item.value['content']),'value_label':value_text(item.value),
            'conditions':value_tree(item.conditions),'scope':value_tree({key:value for key,value in item.node.scope.items() if key!='source_document'}),
            'evidence':evidence,'can_manage':access.can_manage_kb(user,doc.kb)}


@login_required
def structure(request):
    kb,slugs=scopes(request)
    rows=list(islice(visible_assertions(request.user,slugs),2001))
    truncated=len(rows)>2000
    rows=rows[:2000]
    visible_objects={item.node.object_id for item in rows}
    try:
        if request.GET.get('run'):
            run=integer(request.GET['run'],'抽取批次')
            rows=[item for item in rows if item.extraction_id==run]
        if request.GET.get('status'):
            rows=[item for item in rows if item.status==request.GET['status']]
        query=request.GET.get('q','').strip().casefold()[:1000]
        if query:
            rows=[item for item in rows if query in (path_label(item)+' '+value_text(item.value)).casefold()]
        if request.GET.get('node'):
            node_id=integer(request.GET['node'],'节点')
            node_rows=[item for item in rows if item.node_id==node_id]
            if not node_rows:
                raise Http404('当前节点没有可访问的内容')
            cards=[assertion_card(item,request.user) for item in node_rows[:50]]
            return JsonResponse({'html':render_to_string('kb/_knowledge_node.html',{'cards':cards,'truncated':len(node_rows)>50},request=request)})
        objects={}
        for item in rows:
            obj=item.node.object
            entry=objects.setdefault(obj.pk,{'id':obj.pk,'label':obj.label,'kind':obj.kind,'count':0,'pending':0})
            entry['count']+=1;entry['pending']+=int(item.status=='candidate')
        roots=sorted(objects.values(),key=lambda obj:obj['label'])
        selected=integer(request.GET['object'],'对象') if request.GET.get('object') else (roots[0]['id'] if roots else None)
        if selected is not None and selected not in objects:
            if selected not in visible_objects:raise Http404('对象不存在')
            selected=roots[0]['id'] if roots else None
    except ValueError as exc:
        return JsonResponse({'error':str(exc)},status=400)
    rows=[item for item in rows if item.node.object_id==selected]
    # Load only ancestors needed by visible assertions; invisible sibling nodes never enter the response.
    pool={item.node_id:item.node for item in rows}
    frontier={node.parent_id for node in pool.values() if node.parent_id}
    for _ in range(16):
        missing=frontier-pool.keys()
        if not missing:break
        parents=list(KnowledgeNode.objects.filter(pk__in=missing))
        pool.update({node.pk:node for node in parents})
        frontier={node.parent_id for node in parents if node.parent_id}
    nodes={pk:{'id':pk,'label':node.label,'kind':node.kind,'children':[],'assertions':[],
               'scope':{key:value for key,value in node.scope.items() if key!='source_document'}} for pk,node in pool.items()}
    for item in rows:
        nodes[item.node_id]['assertions'].append({'id':item.pk,'preview':value_preview(item.value),
                   'status':item.status,'current':assertion_current(item),'source':item.document.original_name})
    tree=[]
    for pk,node in pool.items():
        if node.parent_id in nodes:nodes[node.parent_id]['children'].append(nodes[pk])
        else:tree.append(nodes[pk])
    def sort(items):
        import re
        from .knowledge_hierarchy import node_key
        def rank(item):
            key=node_key(item['label'],'entry')
            if key.isdecimal():return int(key)
            if not re.fullmatch('[ivxlcdm]+',key):return None
            values={'i':1,'v':5,'x':10,'l':50,'c':100,'d':500,'m':1000}
            return sum(-values[ch] if i+1<len(key) and values[ch]<values[key[i+1]] else values[ch] for i,ch in enumerate(key))
        if items and all(item['kind']=='entry' and rank(item) is not None for item in items):
            items.sort(key=rank)
        else:items.sort(key=lambda item:item['id'])
        for item in items:sort(item['children'])
    sort(tree)
    return JsonResponse({'objects':roots,'selected':selected,'tree':tree,'truncated':truncated,
                         'scope':kb.name,'count':len(rows)})


@login_required
@require_POST
@transaction.atomic
def action(request):
    _,slugs=scopes(request)
    try:
        pk=integer(request.POST.get('assertion',''),'内容')
        item=get_object_or_404(assertion_queryset().select_for_update(),pk=pk,document__kb__slug__in=slugs)
        if not access.can_manage_kb(request.user,item.document.kb):
            raise Http404('内容不存在')
        review_assertion(item,request.POST.get('status',''),request.user)
    except ValueError as exc:
        return JsonResponse({'ok':False,'error':str(exc)},status=400)
    return JsonResponse({'ok':True,'message':'该条内容的审核状态已更新。','assertion_id':item.pk})


@login_required
def fact(request, assertion_id):
    from .knowledge_hierarchy import fact_snapshot, validate_snapshot
    item=get_object_or_404(assertion_queryset(),pk=assertion_id,document__kb__in=libraries(request.user))
    snapshot=fact_snapshot(item)
    if not snapshot or not validate_snapshot(snapshot,request.user) or request.GET.get('v',snapshot['fact_digest'])!=snapshot['fact_digest']:
        return HttpResponse('该内容已撤回或来源失效，请重新检索。',status=410)
    card=assertion_card(item,request.user);card['can_manage']=False
    return render(request,'kb/knowledge_fact.html',{'card':card})
