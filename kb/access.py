"""Knowledge-base permissions: shared by department, or individual and owner-only."""
from __future__ import annotations
from django.db.models import Q
from .models import DEPARTMENT_GENERAL,Document,KnowledgeBase


def user_department(user):
    from accounts.models import user_department as value
    return value(user)


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
    if root.visibility=='individual':return root.created_by_id==user.pk
    if kb.visibility=='individual':return kb.created_by_id==user.pk and (is_global_admin(user) or root.department in (DEPARTMENT_GENERAL,user_department(user)))
    return is_global_admin(user) or all(value in (DEPARTMENT_GENERAL,user_department(user)) for value in (root.department,kb.department))


def can_manage_kb(user,kb):
    if not can_use_kbs(user):return False
    root=root_kb(kb)
    if root.visibility=='individual':return root.created_by_id==user.pk
    if kb.visibility=='individual':return kb.created_by_id==user.pk and kb_accessible(user,kb)
    return is_global_admin(user) or (is_manager(user) and root.department==user_department(user) and kb.department==user_department(user))


def _permission_q(user,prefix='',manage=False,admin_all=False):
    if not can_use_kbs(user):return Q(**{prefix+'pk__in':[]})
    def private(path):return Q(**{path+'visibility':'individual',path+'created_by_id':user.pk})
    def shared(path):
        if manage and not is_manager(user):return Q(**{path+'pk__in':[]})
        value=Q(**{path+'visibility':'shared'})
        if not (is_global_admin(user) and (manage or admin_all)):
            value &= Q(**{path+'department':user_department(user)}) if manage else Q(**{path+'department__in':[DEPARTMENT_GENERAL,user_department(user)]})
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


def accessible_kbs(user):return KnowledgeBase.objects.filter(parent__isnull=True).filter(kb_q(user))


def accessible_doc_slugs(user):return set(KnowledgeBase.objects.filter(kb_q(user)).values_list('slug',flat=True))


def accessible_docs(user,completed_only=True):
    if not hasattr(user,'is_authenticated'):user=getattr(user,'user',None)
    qs=Document.objects.select_related('kb').filter(kb_q(user,'kb__'))
    return qs.filter(status=Document.Status.COMPLETED) if completed_only else qs


def all_departments():
    from accounts.models import Department,UserProfile
    values=set(Department.objects.values_list('name',flat=True))
    values.update(UserProfile.objects.exclude(department='').values_list('department',flat=True))
    values.update(KnowledgeBase.objects.filter(visibility='shared').exclude(department='').values_list('department',flat=True))
    values.discard(DEPARTMENT_GENERAL)
    return [DEPARTMENT_GENERAL]+sorted(values)


def creation_fields(user,level=None,department=None):
    if not can_use_kbs(user):raise ValueError('请先登录。')
    own=user_department(user)
    if not level:level='individual' if not is_manager(user) else 'general' if (department if is_global_admin(user) else own) in (None,'',DEPARTMENT_GENERAL) else 'department'
    if level=='individual':return {'visibility':'individual','department':own}
    if level not in ('general','department'):raise ValueError('请选择有效的访问范围。')
    if not is_manager(user):raise ValueError('普通用户只能创建个人知识库。')
    if level=='general':
        if not is_global_admin(user) and own!=DEPARTMENT_GENERAL:raise ValueError('无权创建通用知识库。')
        return {'visibility':'shared','department':DEPARTMENT_GENERAL}
    department=(department or own) if is_global_admin(user) else own
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
