"""Knowledge-base permissions: shared by department, or individual and owner-only."""
from __future__ import annotations
from django.db.models import Q
from .models import DEPARTMENT_GENERAL,Document,KnowledgeBase


def user_department(user):
    from accounts.models import user_department as value
    return value(user)


def user_departments(user):
    """可读部门集合：「通用」（人人默认分配）∪ 账号已分配的全部部门。"""
    from accounts.models import user_departments as value
    return value(user)


def write_departments(user):
    """可写部门集合：去掉「通用」，除非主部门就是「通用」（旧行为，见 accounts.models）。"""
    from accounts.models import user_write_departments as value
    return value(user)


def manageable_departments(user):
    """可选的**具体**部门（不含「通用」）：新建/改派部门级知识库的下拉选项。"""
    return [d for d in write_departments(user) if d != DEPARTMENT_GENERAL]


def is_global_admin(user):
    return bool(getattr(user,'is_staff',False) or getattr(user,'is_superuser',False))


def is_manager(user):
    from accounts.models import user_role,UserProfile
    return is_global_admin(user) or user_role(user)==UserProfile.Role.DEPT_ADMIN


def can_use_kbs(user):
    return bool(user and getattr(user,'is_authenticated',False) and getattr(user,'is_active',False) and getattr(user,'pk',None) is not None)


def root_kb(kb):
    seen=set()
    while kb.parent_id and kb.pk not in seen:
        seen.add(kb.pk);kb=kb.parent
    return kb


def kb_accessible(user,kb):
    if not can_use_kbs(user):return False
    root=root_kb(kb)
    mine=set(user_departments(user))  # 已含「通用」
    if root.visibility=='individual':return root.created_by_id==user.pk
    if kb.visibility=='individual':return kb.created_by_id==user.pk and (is_global_admin(user) or root.department in mine)
    return is_global_admin(user) or all(value in mine for value in (root.department,kb.department))


def can_manage_kb(user,kb):
    if not can_use_kbs(user):return False
    root=root_kb(kb)
    if root.visibility=='individual':return root.created_by_id==user.pk
    if kb.visibility=='individual':return kb.created_by_id==user.pk and kb_accessible(user,kb)
    mine=set(write_departments(user))
    return is_global_admin(user) or (is_manager(user) and root.department in mine and kb.department in mine)


def _permission_q(user,prefix='',manage=False,admin_all=False):
    """两套范围：读（kb_q，搜索/问答）与写（managed_q，上传/改名/删除）。

    读：共享库 = 「通用」∪ 账号所属的**全部**部门（一个账号可有多个部门；
        admin_all=True 时全局管理员追加全部共享库）。
    写：共享库 = 仅本人**可写部门集合**内（全局管理员不限部门）。「通用」默认分配给
        所有账号，因此对多数人只读：库照常可见（见 visible_q），写操作由全局管理员
        或主部门就是「通用」的部门管理员执行（见 accounts.models.user_write_departments）。
    个人库永远只认创建者（全局管理员也无旁路）。
    """
    if not can_use_kbs(user):return Q(**{prefix+'pk__in':[]})
    def private(path):return Q(**{path+'visibility':'individual',path+'created_by_id':user.pk})
    def shared(path):
        if manage and not is_manager(user):return Q(**{path+'pk__in':[]})
        value=Q(**{path+'visibility':'shared'})
        if not (is_global_admin(user) and (manage or admin_all)):
            value &= Q(**{path+'department__in':write_departments(user)}) if manage \
                else Q(**{path+'department__in':user_departments(user)})
        return value
    top=Q(**{prefix+'parent__isnull':True})&(private(prefix)|shared(prefix))
    parent_private=Q(**{prefix+'parent__isnull':False})&private(prefix+'parent__')
    children=shared(prefix+'parent__')&(private(prefix)|shared(prefix))
    return top|parent_private|children


def kb_q(user,prefix='',*,admin_all=False):return _permission_q(user,prefix,admin_all=admin_all)


def managed_q(user,prefix=''):return _permission_q(user,prefix,manage=True)


def shared_q(prefix=''):
    return Q(**{prefix+'parent__isnull':True,prefix+'visibility':'shared'})|Q(**{prefix+'visibility':'shared',prefix+'parent__visibility':'shared'})


def managed_kbs(user):return KnowledgeBase.objects.filter(managed_q(user))


def visible_q(user,prefix=''):
    """浏览范围 = 可读 ∪ 可写。

    只靠读范围会让全局管理员丢掉本来能管理的别部门库（写范围对管理员不限部门）；
    只靠写范围就是本次 bug：「通用」库对其它部门彻底隐身。所以取并集。
    """
    if not can_use_kbs(user):return Q(**{prefix+'pk__in':[]})
    return kb_q(user,prefix) | managed_q(user,prefix)


def accessible_kbs(user):return KnowledgeBase.objects.filter(parent__isnull=True).filter(kb_q(user))


def visible_kbs(user,prefix=''):
    """顶层可见库（文件夹 + 独立文档库），并标出 `can_manage`。

    管理页用它列表：「通用」库对所有部门可见（只读），写入口由 can_manage 控制。
    """
    rows=list(KnowledgeBase.objects.filter(parent__isnull=True).filter(visible_q(user,prefix)).order_by('name'))
    writable=set(KnowledgeBase.objects.filter(managed_q(user,prefix)).filter(
        pk__in=[r.pk for r in rows]).values_list('pk',flat=True))
    for row in rows:
        row.can_manage = row.pk in writable
    return rows


def accessible_doc_slugs(user):return set(KnowledgeBase.objects.filter(kb_q(user)).values_list('slug',flat=True))


def accessible_docs(user,completed_only=True):
    if not hasattr(user,'is_authenticated'):user=getattr(user,'user',None)
    qs=Document.objects.select_related('kb').filter(kb_q(user,'kb__'))
    return qs.filter(status=Document.Status.COMPLETED) if completed_only else qs


def all_departments():
    """部门池：登记行 ∪ 主部门 ∪ 多选部门 ∪ 共享库部门；「通用」永远排第一。"""
    from accounts.models import Department,UserProfile
    values=set(Department.objects.values_list('name',flat=True))
    values.update(UserProfile.objects.exclude(department='').values_list('department',flat=True))
    values.update(UserProfile.objects.filter(departments__isnull=False).values_list('departments__name',flat=True))
    values.update(KnowledgeBase.objects.filter(visibility='shared').exclude(department='').values_list('department',flat=True))
    values.discard(DEPARTMENT_GENERAL)
    return [DEPARTMENT_GENERAL]+sorted(values)


def creation_fields(user,level=None,department=None):
    if not can_use_kbs(user):raise ValueError('请先登录。')
    own=user_department(user)
    mine=manageable_departments(user)  # 可选的具体部门
    if not level:level='individual' if not is_manager(user) else 'general' if (department if is_global_admin(user) else own) in (None,'',DEPARTMENT_GENERAL) else 'department'
    if level=='individual':return {'visibility':'individual','department':own}
    if level not in ('general','department'):raise ValueError('请选择有效的访问范围。')
    if not is_manager(user):raise ValueError('普通用户只能创建个人知识库。')
    if level=='general':
        # 「通用」人人可见，但只有全局管理员或主部门就是「通用」的账号能建通用库。
        if not is_global_admin(user) and own!=DEPARTMENT_GENERAL:raise ValueError('无权创建通用知识库。')
        return {'visibility':'shared','department':DEPARTMENT_GENERAL}
    if is_global_admin(user):department=department or own
    # 部门管理员只能落到自己所辖的部门；越权或留空时退回自己的主部门
    else:department=department if department in mine else (own if own in mine else (mine[0] if mine else own))
    if department==DEPARTMENT_GENERAL:raise ValueError('请选择具体部门，或使用通用访问范围。')
    if department not in all_departments():raise ValueError('请选择已有部门。')
    return {'visibility':'shared','department':department}


def set_kb_department(kb,department):
    kb.department=(department or '').strip() or DEPARTMENT_GENERAL
    kb.save(update_fields=['department','updated_at'])


def set_access(kb,user,level,department=None):
    if not can_manage_kb(user,kb):raise ValueError('没有管理该知识库的权限。')
    if level=='individual' and kb.created_by_id!=user.pk:raise ValueError('仅创建者可将知识库设为个人。')
    fields=creation_fields(user,level,department)
    kb.visibility=fields['visibility'];kb.department=fields['department']
    kb.save(update_fields=['visibility','department','updated_at'])


def can_edit_feed(user,feed):return bool(can_use_kbs(user) and user.pk==feed.created_by_id and can_manage_kb(user,feed.kb))


def require_feed_builder(user,feed):
    if not can_edit_feed(user,feed):raise ValueError('仅此自动任务的创建者可编辑配置。')


def conversation_q(user):
    """Unscoped chats are visible to their owner; retain legacy KB restrictions."""
    if not can_use_kbs(user):return Q(pk__in=[])
    return Q(user_id=user.pk)&(Q(kb__isnull=True)|kb_q(user,'kb__'))


def conversation_accessible(user,conversation):
    return bool(can_use_kbs(user) and conversation.user_id==user.pk and
                (conversation.kb_id is None or kb_accessible(user,conversation.kb)))
