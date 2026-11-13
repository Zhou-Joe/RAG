from __future__ import annotations

"""凭据录入、自动加密、主机绑定和运行时注入；公开接口只返回引用与状态。"""

import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from .feed_protocol import validate_url
from .models import FeedCredential
from .credential_routing import MAX_CREDENTIAL_PROFILES, PROFILE_NAME, origin_url, scoped_headers

LABELS = {'bearer':'Bearer Token','api_key':'API Key','basic':'Basic 用户名与密码','login':'网页登录账号与密码','headers':'自定义请求头','multiple':'多组访问凭据'}


def api_origin(url: str) -> str:
    """统一 scheme、主机与实际端口，凭据不跟随跨主机请求。"""
    validate_url(url,allow_fragment=True)
    parts = urlsplit(url)
    return json.dumps([parts.scheme.lower(),parts.hostname.lower(),parts.port or (443 if parts.scheme=='https' else 80)])


def _cipher(*, create: bool = False) -> Fernet:
    """首次自动创建独立随机密钥，后续进程复用；丢失密钥时不静默替换。"""
    path = Path(settings.DATA_DIR)/'secrets'/'feed-credentials.key'
    if create and not path.exists():
        if FeedCredential.objects.exists(): raise ValueError('凭据加密密钥不可用，请恢复部署备份中的密钥文件。')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        try:
            descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as stream: stream.write(Fernet.generate_key())
        except FileExistsError: pass
    for attempt in range(3):
        try: return Fernet(path.read_bytes())
        except ValueError:
            if attempt<2: time.sleep(0.02)
        except OSError: break
    raise ValueError('凭据加密密钥不可用，请恢复部署备份中的密钥文件。')


# 调用方（尤其是本地模型）经常省略 type：只有一种含义明确时才推断，否则报错，
# 绝不静默按「不认证」处理——那会导凭据从未保存而后续每次请求都 401。
INFER = {'token': 'bearer', 'value': 'api_key', 'header_name': 'api_key',
         'username': 'basic', 'password': 'basic', 'headers': 'headers', 'profiles':'multiple'}
AUTH_HELP = ('请明确给出 type：none / bearer(token) / api_key(header_name,value) / '
             'basic(username,password) / login(username,password) / headers([{name,value}]) / multiple(profiles)。')


def normalize_auth(auth: dict) -> dict:
    """补全缺失的 `type`；歧义结构直接报错，不猜。空字典＝显式不认证（页面默认）。"""
    kind = auth.get('type', None)
    if kind is not None and kind != '':
        return auth
    if not auth:
        return {'type': 'none'}
    guesses = {INFER[key] for key in auth if key in INFER}
    if len(guesses) != 1:
        raise ValueError('认证配置缺少 type，且无法安全推断，不会按「不认证」处理。' + AUTH_HELP)
    return {**auth, 'type': guesses.pop()}


def parse_auth(auth: dict) -> tuple[str,dict,list[str]]:
    """将易读表单转换为请求头，并拒绝控制字符、重复头与连接头覆盖。"""
    from .feed_http import validate_headers
    if not isinstance(auth,dict): raise ValueError('认证配置格式无效。')
    auth = normalize_auth(auth)
    kind = auth.get('type','none')
    def value(key: str, label: str) -> str:
        result = auth.get(key,'')
        if not isinstance(result,str) or not result or len(result)>8000 or any(ord(ch)<32 or ord(ch)==127 for ch in result):
            raise ValueError('请填写有效的'+label+'，不能包含换行。')
        return result
    extra = []
    if kind=='none': return kind,{},[]
    if kind=='multiple':
        profiles=_parse_profiles(auth)
        # A bundle has no unscoped header set. Call authentication_headers with
        # the destination URL to obtain only that service's credentials.
        return kind,{},list(dict.fromkeys(value for profile in profiles for value in profile['secrets']))
    if kind=='bearer':
        token = value('token','Token').strip()
        token = re.sub(r'^Bearer\s+','',token,flags=re.I)
        if not token or any(ch.isspace() for ch in token): raise ValueError('Token 格式无效，请粘贴完整令牌。')
        headers = {'Authorization':'Bearer '+token};extra=[token]
    elif kind=='api_key': headers={value('header_name','请求头名称').strip():value('value','API Key')}
    elif kind=='basic':
        username=value('username','用户名');password=value('password','密码')
        if ':' in username: raise ValueError('Basic 用户名不能包含冒号。')
        headers={'Authorization':'Basic '+base64.b64encode((username+':'+password).encode()).decode()};extra=[password]
    elif kind=='login':
        extra=[value('username','网页登录账号'),value('password','网页登录密码')];headers={}
    elif kind=='headers':
        rows=auth.get('headers')
        if not isinstance(rows,list) or not 1<=len(rows)<=10: raise ValueError('请填写1至10项请求头名称和值。')
        headers={}
        for row in rows:
            if not isinstance(row,dict): raise ValueError('请求头格式无效。')
            name=row.get('name');content=row.get('value')
            if not isinstance(name,str) or not isinstance(content,str) or not content or len(content)>8000 or any(ord(ch)<32 or ord(ch)==127 for ch in content): raise ValueError('请求头名称和值不能为空或包含换行。')
            if name.lower() in {key.lower() for key in headers}: raise ValueError('请求头名称不能重复。')
            headers[name]=content
    else: raise ValueError('请选择支持的认证方式。')
    validate_headers({key:'FOS_FEED_CHECK' for key in headers})
    return kind,headers,list(dict.fromkeys([*headers.values(),*extra]))


def bindings(credential: FeedCredential) -> dict:
    """返回脚本和模型可使用的非密钥引用；无需手动配置这些变量。"""
    return {name:f'FOS_FEED_CREDENTIAL_{credential.pk.hex.upper()}_{index}' for index,name in enumerate(credential.header_names)}


def store(user, url: str, auth: dict, *, previous=None) -> FeedCredential | None:
    """将页面中的认证值加密保存；永不写入 prompt、spec 或日志。"""
    if not isinstance(auth,dict):raise ValueError('认证配置格式无效。')
    auth=normalize_auth(auth)
    if auth.get('type')=='multiple':
        if previous and previous.owner_id!=user.pk:raise ValueError('只能保留本人保存的凭据；请填写完整的新凭据。')
        return _store_profiles(user,url,_parse_profiles(auth,previous=previous))
    kind,headers,secrets = parse_auth(auth)
    if kind=='none': return None
    origin = api_origin(url)
    payload={'owner':str(user.pk),'origin':origin,'headers':headers,'secrets':secrets,
             'login':{'username':auth['username'],'password':auth['password']} if kind=='login' else {}}
    encrypted=_cipher(create=True).encrypt(json.dumps(payload,ensure_ascii=False).encode()).decode()
    return FeedCredential.objects.create(owner=user,kind=kind,api_origin=origin,header_names=list(headers),encrypted_value=encrypted)


def unpack(credential: FeedCredential) -> dict:
    """校验密文完整性及归属后在内存中解密，异常不包含密钥或密文。"""
    if not credential.owner_id or not credential.owner.is_active: raise ValueError('凭据所属账号已停用。')
    try: data=json.loads(_cipher().decrypt(credential.encrypted_value.encode()))
    except (InvalidToken,ValueError,TypeError) as exc: raise ValueError('无法解密 API 凭据，请在页面重新保存。') from exc
    if data.get('owner')!=str(credential.owner_id) or data.get('origin')!=credential.api_origin: raise ValueError('API 凭据归属校验未通过。')
    return data


def secret_values(resource) -> tuple[str,...]:
    credential=getattr(resource,'credential',None)
    if credential: return tuple(unpack(credential)['secrets'])
    return tuple(os.environ.get(name,'') for name in resource.header_env.values())


def request_headers(resource, url: str) -> dict:
    """工具与定时任务共用凭据解析；只注入到用户确认的同一来源。"""
    from .feed_http import environment_headers
    credential=getattr(resource,'credential',None)
    if not credential: return environment_headers(resource.header_env)
    if resource.header_env!=bindings(credential): raise ValueError('认证引用已变化，请重新保存凭据。')
    if credential.kind=='multiple':
        profiles=credential_profiles(resource)
        if api_origin(url) not in {credential.api_origin,*[api_origin(profile['url']) for profile in profiles]}:
            raise ValueError('此服务没有配置访问凭据，请先保存该服务的凭据。')
        return scoped_headers(profiles,url)
    if api_origin(url)!=credential.api_origin: raise ValueError('API 主机发生变化，请在页面重新填写该主机的凭据。')
    return unpack(credential)['headers']


def summary(resource) -> dict:
    credential=getattr(resource,'credential',None)
    if credential:
        result={'configured':True,'type':credential.kind,'label':LABELS.get(credential.kind,'API 认证'),
                'headers':credential.header_names,'managed':True,'login_fields':['username','password'] if credential.kind=='login' else [],
                'url':_origin_from_saved(credential.api_origin)}
        if credential.kind=='multiple':
            result['profiles']=[{'name':profile['name'],'url':profile['url'],'configured':True,
                'type':profile['kind'],'label':LABELS.get(profile['kind'],'API 认证'),
                'headers':list(profile['headers']),'login_fields':['username','password'] if profile.get('login') else []}
                for profile in credential_profiles(resource)]
            result['profile_count']=len(result['profiles'])
        return result
    return {'configured':bool(resource.header_env),'type':'legacy' if resource.header_env else 'none',
            'label':'已有环境变量认证' if resource.header_env else '无需认证','headers':list(resource.header_env),'managed':False}


def _origin_from_saved(value):
    scheme,host,port=json.loads(value)
    host='['+host+']' if ':' in host else host
    return scheme+'://'+host+(':'+str(port) if port!=(443 if scheme=='https' else 80) else '')


def _profiles_from_credential(credential):
    data=unpack(credential)
    if credential.kind=='multiple':return data['profiles']
    return [{'name':'primary','url':_origin_from_saved(credential.api_origin),'kind':credential.kind,
        'headers':data['headers'],'secrets':data['secrets'],'login':data.get('login',{})}]


def credential_profiles(resource):
    credential=getattr(resource,'credential',None)
    if not credential:return []
    if resource.header_env!=bindings(credential):raise ValueError('认证引用已变化，请重新保存凭据。')
    return _profiles_from_credential(credential)


def credential_urls(resource):
    return [profile['url'] for profile in credential_profiles(resource)]


def _parse_profiles(auth, *, previous=None):
    rows=auth.get('profiles')
    if not isinstance(rows,list) or not 1<=len(rows)<=MAX_CREDENTIAL_PROFILES:
        raise ValueError('请配置1至8组访问凭据。')
    saved={profile['name']:profile for profile in _profiles_from_credential(previous)} if previous else {}
    profiles=[];seen=set()
    for row in rows:
        if not isinstance(row,dict) or not row.keys()<={'name','url','authentication'}:
            raise ValueError('每组凭据使用 name、url、authentication。')
        name=row.get('name')
        if not isinstance(name,str) or not PROFILE_NAME.fullmatch(name) or name in seen:
            raise ValueError('凭据名称须唯一，以字母开头，可用字母、数字、下划线或连字符，最多40字符。')
        seen.add(name)
        url=row.get('url');api_origin(url);url=origin_url(url)
        authentication=row.get('authentication')
        if not isinstance(authentication,dict):raise ValueError('请填写每组凭据的认证方式。')
        authentication=normalize_auth(authentication)
        if authentication.get('type')=='saved':
            profile=saved.get(name)
            if not profile or api_origin(profile['url'])!=api_origin(url):
                raise ValueError('无法复用这组凭据；名称或接收服务已改变，请重新填写。')
            profiles.append(dict(profile));continue
        if authentication.get('type') in ('multiple','none'):
            raise ValueError('每组凭据请选择一种实际认证方式，不嵌套凭据组。')
        kind,headers,secrets=parse_auth(authentication)
        profiles.append({'name':name,'url':url,'kind':kind,'headers':headers,'secrets':secrets,
            'login':{'username':authentication['username'],'password':authentication['password']} if kind=='login' else {}})
    for profile in profiles:scoped_headers(profiles,profile['url'])
    return profiles


def _store_profiles(user,url,profiles):
    if not profiles:return None
    if len(profiles)>MAX_CREDENTIAL_PROFILES:raise ValueError('每个任务最多保存8组访问凭据。')
    origin=api_origin(url)
    for profile in profiles:scoped_headers(profiles,profile['url'])
    names={}
    for profile in profiles:
        for name in profile['headers']:names.setdefault(name.lower(),name)
    if len(names)>20:raise ValueError('凭据组总共最多使用20个不同的认证请求头。')
    secrets=list(dict.fromkeys(value for profile in profiles for value in profile['secrets']))
    payload={'owner':str(user.pk),'origin':origin,'headers':scoped_headers(profiles,url),
        'secrets':secrets,'login':{},'profiles':profiles}
    encrypted=_cipher(create=True).encrypt(json.dumps(payload,ensure_ascii=False).encode()).decode()
    return FeedCredential.objects.create(owner=user,kind='multiple',api_origin=origin,
        header_names=list(names.values()),encrypted_value=encrypted)


def authentication_headers(auth,url,*,previous=None):
    """Test unsaved form input against only its configured target service."""
    if not isinstance(auth,dict):raise ValueError('认证配置格式无效。')
    auth=normalize_auth(auth)
    return scoped_headers(_parse_profiles(auth,previous=previous),url) if auth.get('type')=='multiple' else parse_auth(auth)[1]


def supplied_values(auth):
    """Raw input values, without synthesized Bearer/Basic encodings."""
    auth=normalize_auth(auth)
    if auth.get('type')=='multiple':
        return [value for row in auth.get('profiles',[]) for value in supplied_values(row.get('authentication',{}))]
    if auth.get('type')=='headers':return [row.get('value','') for row in auth.get('headers',[])]
    return [auth[key] for key in ('token','username','password','value') if auth.get(key)]


def upsert_access(resource,user,url,auth,*,name=''):
    """An additional service no longer replaces credentials already saved for another."""
    auth=normalize_auth(auth)
    previous=getattr(resource,'credential',None)
    if auth.get('type')=='multiple':return store(user,url,auth,previous=previous)
    parse_auth(auth)
    if not previous and auth.get('type')=='none':return None
    if not previous:return store(user,url,auth) if not name else store(user,url,{'type':'multiple','profiles':[{'name':name,'url':url,'authentication':auth}]})
    if previous.owner_id!=user.pk:raise ValueError('只能保留本人保存的凭据；请重新填写完整凭据组。')
    profiles=_profiles_from_credential(previous)
    target=api_origin(url)
    if name:
        if not PROFILE_NAME.fullmatch(name):raise ValueError('凭据名称格式无效。')
        old=next((profile for profile in profiles if profile['name']==name),None)
        if old and api_origin(old['url'])!=target:raise ValueError('不能将已保存的凭据名称移到另一个服务。')
    else:
        same=[profile for profile in profiles if api_origin(profile['url'])==target]
        if len(same)>1:raise ValueError('此服务有多组凭据，请用 name 指定要修改的组。')
        name=same[0]['name'] if same else next('service-'+str(i) for i in range(1,10) if 'service-'+str(i) not in {p['name'] for p in profiles})
    profiles=[profile for profile in profiles if profile['name']!=name]
    if auth.get('type')!='none':
        profiles.extend(_parse_profiles({'profiles':[{'name':name,'url':url,'authentication':auth}]}))
    primary=getattr(resource,'api_url','') or getattr(resource,'url','') or url
    return _store_profiles(user,primary,profiles)


def export_profiles(resource):
    credential=getattr(resource,'credential',None)
    if not credential or credential.kind!='multiple':return []
    result=[]
    for index,profile in enumerate(credential_profiles(resource)):
        prefix='FOS_FEED_CREDENTIAL_'+credential.pk.hex.upper()+'_'+str(index)
        result.append({'name':profile['name'],'url':profile['url'],
            'header_env':{name:prefix+'_HEADER_'+str(i) for i,name in enumerate(profile['headers'])},
            'login_env':{field:prefix+'_'+field.upper() for field in profile.get('login',{})}})
    return result


def discard_if_unused(credential_id) -> None:
    if credential_id:
        FeedCredential.objects.filter(pk=credential_id,feeds__isnull=True,drafts__isnull=True).delete()
