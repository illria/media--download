from __future__ import annotations
import asyncio, hashlib, hmac, ipaddress, json, os, re, secrets, shutil, signal, socket, sqlite3, subprocess, tempfile, threading, time, uuid, zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

PORT=int(os.getenv('APP_PORT','19190')); ROOT=Path(os.getenv('DATA_ROOT','/data')).resolve(); DB=ROOT/'database/media-hub.db'; DL=ROOT/'downloads'; TMP=ROOT/'temp'; CK=ROOT/'cookies'; STATIC=Path(__file__).parent/'static'
for p in (DB.parent,DL,TMP,CK): p.mkdir(parents=True,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat()
def secret(path,make):
    if path.exists() and path.read_text().strip(): return path.read_text().strip()
    v=make(); path.write_text(v); os.chmod(path,0o600); return v
SECRET=os.getenv('SECRET_KEY') or secret(DB.parent/'secret.key',lambda:secrets.token_urlsafe(48)); FKEY=os.getenv('COOKIE_ENCRYPTION_KEY') or secret(DB.parent/'cookie.key',lambda:Fernet.generate_key().decode()); F=Fernet(FKEY.encode()); TOK=URLSafeTimedSerializer(SECRET,salt='media-hub'); GUEST_TOK=URLSafeTimedSerializer(SECRET,salt='media-hub-guest')
GUEST_DEFAULT={'max_file_size_gb':1,'default_resolution':720,'max_resolution':1080,'retention_minutes':30,'min_free_gb':2,'emergency_free_gb':1,'request_sleep_seconds':5,'max_active_tasks_per_guest':1,'max_queued_tasks_per_guest':2,'global_guest_concurrency':1,'max_video_duration_minutes':60,'allow_ai_transcription':True,'ai_transcription_max_duration_minutes':20,'ai_transcription_global_concurrency':1,'ai_transcription_hourly_limit_per_guest':3,'allow_subtitle_translation':True,'subtitle_translation_max_duration_minutes':60,'subtitle_translation_hourly_limit_per_guest':3,'subtitle_translation_global_concurrency':1,'subtitle_translation_max_target_languages':1,'allow_cookie':False,'allow_koofr':False,'allow_live_download':False}
DEFAULT={'max_file_size_gb':5,'retention_hours':24,'min_free_gb':5,'max_resolution':1080,'proxy_url':'','youtube_compatibility_mode':True,'request_sleep_seconds':1,'guest_policy':GUEST_DEFAULT}
GUEST_SESSION_COOKIE='media_guest_session'; GUEST_SESSION_MAX_AGE=60*60*24*30; GUEST_PROBE_TTL=10*60; GUEST_AI_EVENT='guest_ai_transcription'; GUEST_TRANSLATION_EVENT='guest_subtitle_translation'
SUBTITLE_LANGUAGES={'zh-CN':'简体中文','zh-TW':'繁体中文','en':'英语','ja':'日语','ko':'韩语','vi':'越南语','th':'泰语','fr':'法语','de':'德语','es':'西班牙语','pt':'葡萄牙语','ru':'俄语','ar':'阿拉伯语','id':'印度尼西亚语','tr':'土耳其语','it':'意大利语'}
SUBTITLE_SOURCE_LANGUAGES={'auto':'自动选择',**SUBTITLE_LANGUAGES}
SUBTITLE_OUTPUT_MODES={'original':'仅原字幕','translated':'翻译字幕','bilingual':'双语字幕'}
ACTIVE={}; GUEST_PROBES={}; GUEST_AI_ACTIVE=set(); SUBTITLE_TRANSLATION_ACTIVE=set(); LOCK=threading.RLock(); STOP=threading.Event()
def con():
    c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
    with con() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);CREATE TABLE IF NOT EXISTS cookies(id TEXT PRIMARY KEY,platform TEXT,label TEXT,path TEXT,name TEXT,created TEXT);CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,url TEXT,title TEXT,platform TEXT,status TEXT,options TEXT,progress REAL DEFAULT 0,speed TEXT,eta TEXT,output_path TEXT,output_size INTEGER,error_code TEXT,error_message TEXT,log_tail TEXT DEFAULT '',created TEXT,updated TEXT,finished TEXT);CREATE TABLE IF NOT EXISTS guest_rate_events(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,event_type TEXT NOT NULL,task_id TEXT,created TEXT NOT NULL);''')
        columns={item['name'] for item in c.execute('PRAGMA table_info(tasks)')}
        for name,definition in {'owner_type':'TEXT','owner_id':'TEXT','policy_snapshot':'TEXT'}.items():
            if name not in columns:c.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
        c.execute("UPDATE tasks SET owner_type='admin',owner_id='admin' WHERE owner_type IS NULL OR owner_type='' OR owner_id IS NULL OR owner_id=''")
        c.execute('CREATE INDEX IF NOT EXISTS tasks_owner_status_created ON tasks(owner_type,owner_id,status,created)')
        c.execute('CREATE INDEX IF NOT EXISTS guest_rate_events_owner_type_created ON guest_rate_events(owner_id, event_type, created)')
        for k,v in DEFAULT.items(): c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        c.execute("INSERT OR IGNORE INTO meta VALUES('admin_session_version','1')")
def bootstrap_admin_password():
    if meta('password'):return
    env=(os.getenv('ADMIN_PASSWORD') or '').strip()
    if env:
        if len(env)<8:raise RuntimeError('ADMIN_PASSWORD must be at least 8 characters')
        setmeta('password',hpw(env));return
    setmeta('password',hpw('admin'))
def recover_interrupted_tasks():
    with con() as c:rows=[dict(item) for item in c.execute("SELECT id,owner_type,status FROM tasks WHERE status IN ('downloading','processing')")]
    for item in rows:
        task_id=str(item.get('id') or '')
        owner_type=str(item.get('owner_type') or 'unknown')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id):
            print(f'[Recovery] task={task_id or "invalid"} owner={owner_type} error=ValueError: invalid task id',flush=True)
            continue
        try:
            for directory in (TMP/task_id,DL/task_id):
                if directory.exists():shutil.rmtree(directory)
            stamp=now()
            with con() as c:
                c.execute("UPDATE tasks SET status='queued',progress=0,speed='',eta='',error_code=NULL,error_message=NULL,output_path=NULL,output_size=NULL,finished=NULL,log_tail='',updated=? WHERE id=? AND status IN ('downloading','processing')",(stamp,task_id))
        except Exception as exc:
            message='中断任务清理失败，请管理员重试'
            print(f'[Recovery] task={task_id} owner={owner_type} error={type(exc).__name__}: {str(exc)[:200]}',flush=True)
            try:
                with con() as c:
                    c.execute("UPDATE tasks SET status='failed',error_code=?,error_message=?,speed='',eta='',progress=0,updated=?,finished=? WHERE id=? AND status IN ('downloading','processing')",('RECOVERY_CLEANUP_FAILED',message,now(),now(),task_id))
            except Exception as mark_exc:
                print(f'[Recovery] task={task_id} owner={owner_type} error={type(mark_exc).__name__}: {str(mark_exc)[:200]}',flush=True)
def meta(k):
    with con() as c:r=c.execute('SELECT v FROM meta WHERE k=?',(k,)).fetchone();return r['v'] if r else None
def setmeta(k,v):
    with con() as c:c.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,v))
def settings():
    d=dict(DEFAULT)
    with con() as c:
        for r in c.execute('SELECT k,v FROM settings'):
            try:d[r['k']]=json.loads(r['v'])
            except:pass
    return d
def save_settings(d):
    with con() as c:
        for k,v in d.items():
            if k in DEFAULT:c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,json.dumps(v)))
    return settings()
def _guest_number(value,default,minimum,maximum,integer=False):
    try:number=int(value) if integer else float(value)
    except (TypeError,ValueError):number=default
    number=max(minimum,min(maximum,number));return int(number) if integer else number
def _guest_bool(value,default):
    if isinstance(value,bool):return value
    if isinstance(value,str) and value.lower() in {'true','1','yes'}:return True
    if isinstance(value,str) and value.lower() in {'false','0','no'}:return False
    return default
def gb_bytes(value):
    try:return int(float(value)*1024**3)
    except (TypeError,ValueError):return 0
def format_gb(value):
    try:number=float(value)
    except (TypeError,ValueError):return str(value)
    if number==int(number):return str(int(number))
    text=f'{number:.3f}'.rstrip('0').rstrip('.')
    return text or '0'
def normalize_guest_policy(raw=None):
    incoming=raw if isinstance(raw,dict) else {}; policy=dict(GUEST_DEFAULT)
    for key in policy:
        if key in incoming:policy[key]=incoming[key]
    policy['max_file_size_gb']=_guest_number(policy['max_file_size_gb'],1,.1,20)
    policy['max_resolution']=_guest_number(policy['max_resolution'],1080,144,2160,True)
    policy['default_resolution']=min(policy['max_resolution'],_guest_number(policy['default_resolution'],720,144,policy['max_resolution'],True))
    policy['retention_minutes']=_guest_number(policy['retention_minutes'],30,1,1440,True)
    policy['min_free_gb']=_guest_number(policy['min_free_gb'],2,.1,100)
    policy['emergency_free_gb']=min(policy['min_free_gb'],_guest_number(policy['emergency_free_gb'],1,.1,100))
    policy['request_sleep_seconds']=_guest_number(policy['request_sleep_seconds'],5,0,60)
    policy['max_active_tasks_per_guest']=_guest_number(policy['max_active_tasks_per_guest'],1,1,10,True)
    policy['max_queued_tasks_per_guest']=_guest_number(policy['max_queued_tasks_per_guest'],2,1,20,True)
    policy['global_guest_concurrency']=_guest_number(policy['global_guest_concurrency'],1,1,20,True)
    policy['max_video_duration_minutes']=_guest_number(policy['max_video_duration_minutes'],60,1,1440,True)
    policy['ai_transcription_max_duration_minutes']=_guest_number(policy['ai_transcription_max_duration_minutes'],20,1,240,True)
    policy['ai_transcription_global_concurrency']=_guest_number(policy['ai_transcription_global_concurrency'],1,1,10,True)
    policy['ai_transcription_hourly_limit_per_guest']=_guest_number(policy['ai_transcription_hourly_limit_per_guest'],3,1,100,True)
    policy['subtitle_translation_max_duration_minutes']=_guest_number(policy['subtitle_translation_max_duration_minutes'],60,1,240,True)
    policy['subtitle_translation_hourly_limit_per_guest']=_guest_number(policy['subtitle_translation_hourly_limit_per_guest'],3,1,100,True)
    policy['subtitle_translation_global_concurrency']=_guest_number(policy['subtitle_translation_global_concurrency'],1,1,5,True)
    policy['subtitle_translation_max_target_languages']=1
    for key in ('allow_ai_transcription','allow_subtitle_translation','allow_cookie','allow_koofr','allow_live_download'):policy[key]=_guest_bool(policy[key],GUEST_DEFAULT[key])
    return policy
def guest_policy():return normalize_guest_policy(settings().get('guest_policy'))
def normalize_subtitle_language(value,allow_auto=False,default='zh-CN'):
    code=str(value or default).strip()
    if allow_auto and code=='auto':return 'auto'
    if code in SUBTITLE_LANGUAGES:return code
    aliases={'zh':'zh-CN','zh-Hans':'zh-CN','zh-Hant':'zh-TW','cn':'zh-CN','jp':'ja','kr':'ko'}
    return aliases.get(code,default if default in SUBTITLE_LANGUAGES or (allow_auto and default=='auto') else 'zh-CN')
def normalize_subtitle_output_mode(value,default='translated'):
    mode=str(value or default).strip().lower()
    return mode if mode in SUBTITLE_OUTPUT_MODES else default
def subtitle_options_from_payload(incoming,policy=None,guest=False):
    policy=policy or guest_policy()
    source=normalize_subtitle_language(incoming.get('subtitle_source_language'),allow_auto=True,default='auto')
    target=normalize_subtitle_language(incoming.get('subtitle_target_language'),allow_auto=False,default='zh-CN')
    output_mode=normalize_subtitle_output_mode(incoming.get('subtitle_output_mode'),'translated')
    if guest and not policy.get('allow_subtitle_translation') and output_mode!='original':
        guest_error('guest_translation_disabled','当前未启用游客字幕翻译')
    if output_mode=='original':
        target=target if target in SUBTITLE_LANGUAGES else 'zh-CN'
    return {
        'subtitle_source_language':source,
        'subtitle_target_language':target,
        'subtitle_output_mode':output_mode,
        'subtitle_languages':[source] if source!='auto' else [target,'en','zh-CN','zh','ja','ko'],
    }
def is_guest_task(task):return bool(task and task.get('owner_type')=='guest')
def task_policy(task):
    if not is_guest_task(task):return settings()
    try:return normalize_guest_policy(json.loads(task.get('policy_snapshot') or '{}'))
    except (TypeError,ValueError,json.JSONDecodeError):return guest_policy()
def _guest_owner(raw):return hmac.new(SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
def _guest_cookie_secure(request:Request):
    forwarded=request.headers.get('x-forwarded-proto','').split(',',1)[0].strip().lower()
    return request.url.scheme=='https' or forwarded=='https'
def _issue_guest_session(response:Response,request:Request):
    sid=secrets.token_urlsafe(32)
    token=GUEST_TOK.dumps({'sid':sid})
    response.set_cookie(GUEST_SESSION_COOKIE,token,max_age=GUEST_SESSION_MAX_AGE,httponly=True,samesite='lax',secure=_guest_cookie_secure(request),path='/')
    return sid
def guest_identity(request:Request,response:Response):
    raw=request.cookies.get(GUEST_SESSION_COOKIE,'')
    sid=None
    if raw:
        try:
            payload=GUEST_TOK.loads(raw,max_age=GUEST_SESSION_MAX_AGE)
            candidate=payload.get('sid') if isinstance(payload,dict) else None
            if isinstance(candidate,str) and candidate:
                sid=candidate
        except (BadSignature,SignatureExpired,TypeError,ValueError):
            sid=None
    if not sid:sid=_issue_guest_session(response,request)
    return {'owner_type':'guest','owner_id':_guest_owner(sid)}
def guest_error(code,message,status=400):raise HTTPException(status,detail={'code':code,'message':message})
def guest_probe_key(owner_id,url):return (owner_id,url)
def cache_guest_probe(owner_id,url,result):
    expires=time.monotonic()+GUEST_PROBE_TTL
    with LOCK:
        GUEST_PROBES[guest_probe_key(owner_id,url)]={'expires':expires,'result':result}
        canonical=str(result.get('webpage_url') or url)
        GUEST_PROBES[guest_probe_key(owner_id,canonical)]={'expires':expires,'result':result}
        for key,value in list(GUEST_PROBES.items()):
            if value['expires']<=time.monotonic():GUEST_PROBES.pop(key,None)
def cached_guest_probe(owner_id,url):
    with LOCK:cached=GUEST_PROBES.get(guest_probe_key(owner_id,url))
    return cached.get('result') if cached and cached.get('expires',0)>time.monotonic() else None
def hpw(p):
    s=os.urandom(16); return s.hex()+':'+hashlib.scrypt(p.encode(),salt=s,n=2**14,r=8,p=1,dklen=32).hex()
def vpw(p,e):
    try:s,d=e.split(':');return hmac.compare_digest(hashlib.scrypt(p.encode(),salt=bytes.fromhex(s),n=2**14,r=8,p=1,dklen=32).hex(),d)
    except:return False
def admin_session_version():
    try:return max(1,int(meta('admin_session_version') or '1'))
    except (TypeError,ValueError):return 1
def using_default_password():return vpw('admin',meta('password') or '')
def admin_token():return TOK.dumps({'sub':'admin','role':'admin','session_version':admin_session_version()})
def auth(authorization:str|None=Header(None)):
    if not authorization or not authorization.startswith('Bearer '):raise HTTPException(401,'需要登录')
    try:
        payload=TOK.loads(authorization[7:],max_age=43200)
        if payload.get('sub')!='admin' or payload.get('role')!='admin' or int(payload.get('session_version') or 0)!=admin_session_version():raise HTTPException(401,'登录失效')
        return payload
    except (BadSignature,SignatureExpired,TypeError,ValueError):raise HTTPException(401,'登录失效')
def public_url(u):
    p=urlsplit(u.strip());
    if p.scheme not in ('http','https') or not p.hostname:raise HTTPException(400,'只允许 HTTP/HTTPS 链接')
    h=p.hostname.lower()
    if h=='localhost' or h.endswith('.local'):raise HTTPException(400,'禁止访问内网')
    return u.strip(),h,p.port or (443 if p.scheme=='https' else 80)
async def validate(u):
    u,h,port=public_url(u); loop=asyncio.get_running_loop()
    try: infos=await loop.run_in_executor(None,lambda:socket.getaddrinfo(h,port,type=socket.SOCK_STREAM))
    except socket.gaierror:raise HTTPException(400,'域名无法解析')
    for i in infos:
        ip=ipaddress.ip_address(i[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:raise HTTPException(400,'禁止访问内网地址')
    return u
def yt(u):
    h=(urlsplit(u).hostname or '').lower(); return h=='youtu.be' or h.endswith('youtube.com')
def cpath(cid):
    if not cid:return None
    with con() as c:r=c.execute('SELECT path FROM cookies WHERE id=?',(cid,)).fetchone()
    if not r:raise RuntimeError('Cookie 不存在')
    p=TMP/f'cookie-{uuid.uuid4().hex}.txt';p.write_bytes(F.decrypt(Path(r['path']).read_bytes()));os.chmod(p,0o600);return p
def base(u,cp=None,compat=False):
    s=settings(); a=['yt-dlp','--no-playlist','--force-ipv4','--socket-timeout','30','--retries','5','--fragment-retries','5','--extractor-retries','3','--js-runtimes','deno','--impersonate','chrome']
    if s.get('request_sleep_seconds'):a+=['--sleep-requests',str(s['request_sleep_seconds'])]
    if s.get('proxy_url'):a+=['--proxy',s['proxy_url']]
    if cp:a+=['--cookies',str(cp)]
    if yt(u):a+=['--extractor-args','youtube:player_client='+('web_safari,android_vr,web_embedded' if compat else 'android_vr,web_safari,web_embedded')]
    return a
def task_base(task,cp=None,compat=False):
    if not is_guest_task(task):return base(task['url'],cp,compat)
    policy=task_policy(task);a=['yt-dlp','--no-playlist','--force-ipv4','--socket-timeout','30','--retries','5','--fragment-retries','5','--extractor-retries','3','--js-runtimes','deno','--impersonate','chrome','--sleep-requests',str(policy['request_sleep_seconds'])]
    if yt(task['url']):a+=['--extractor-args','youtube:player_client='+('web_safari,android_vr,web_embedded' if compat else 'android_vr,web_safari,web_embedded')]
    return a
def denied(t):
    t=t.lower();return any(x in t for x in ('403','forbidden','sign in to confirm','not a bot','po token'))
def err(t,u=''):
    l=t.lower()
    if 'drm' in l:return 'DRM_PROTECTED','检测到 DRM，系统不会解密'
    if 'sign in to confirm' in l or 'not a bot' in l:return 'YOUTUBE_LOGIN_REQUIRED','YouTube 将服务器 IP 判定为异常，请上传有效 Cookie；仍失败时配置住宅代理'
    if 'requested format is not available' in l:return 'FORMAT_UNAVAILABLE','该清晰度不可用，请重新解析并选择其他分辨率'
    if '429' in l:return 'RATE_LIMITED','请求过多，请稍后重试或配置代理'
    if denied(l):return ('YOUTUBE_ACCESS_DENIED' if yt(u) else 'ACCESS_DENIED'),'平台拒绝媒体流请求；系统已自动切换兼容客户端，请上传 Cookie 或配置代理后重试'
    if 'unsupported url' in l:return 'UNSUPPORTED_URL','当前解析器不支持该链接'
    if 'cookies' in l or 'login' in l:return 'LOGIN_REQUIRED','内容需要登录，请上传 cookies.txt'
    return 'DOWNLOAD_FAILED',t[-1200:] or '下载失败'
def simplify(fs):
    g={}; aud=[]
    for x in fs:
        if not x.get('url'):continue
        if x.get('vcodec') not in (None,'none') and x.get('height'):g.setdefault(int(x['height']),[]).append(x)
        elif x.get('acodec') not in (None,'none'):aud.append(x)
    out=[]
    for h in sorted(g,reverse=True):
        pref='webm' if h>1080 else 'mp4';x=max(g[h],key=lambda z:(z.get('ext')==pref,z.get('acodec') not in (None,'none'),z.get('tbr') or 0))
        out.append({'format_id':str(x.get('format_id','')),'label':'4K' if h>=2160 else '2K' if h>=1440 else f'{h}p','height':h,'ext':x.get('ext','mp4'),'has_audio':x.get('acodec') not in (None,'none'),'filesize':x.get('filesize') or x.get('filesize_approx')})
    aud.sort(key=lambda x:x.get('abr') or x.get('tbr') or 0,reverse=True)
    return out[:12],[{'format_id':str(x.get('format_id','')),'ext':x.get('ext','m4a'),'abr':x.get('abr') or x.get('tbr')} for x in aud[:5]]
def probe_sync(u,cid):
    cp=cpath(cid); last='解析失败'; raw=None
    try:
        for compat in ([False,True] if yt(u) else [False]):
            p=subprocess.run(base(u,cp,compat)+['--dump-single-json','--no-warnings',u],capture_output=True,text=True,timeout=120)
            if p.returncode==0:raw=json.loads(p.stdout);break
            last=(p.stderr or p.stdout or last)[-4000:]
            if not denied(last):break
        if raw is None:raise RuntimeError(last)
    finally:
        if cp:cp.unlink(missing_ok=True)
    v,a=simplify(raw.get('formats') or [])
    return {'id':raw.get('id'),'title':raw.get('title'),'uploader':raw.get('uploader') or raw.get('channel'),'platform':raw.get('extractor_key') or raw.get('extractor'),'duration':raw.get('duration'),'thumbnail':raw.get('thumbnail'),'is_live':bool(raw.get('is_live') or raw.get('live_status')=='is_live'),'drm':bool(raw.get('has_drm')),'video_options':v,'audio_options':a,'subtitles':sorted(set((raw.get('subtitles') or {}))|set((raw.get('automatic_captions') or {}))),'webpage_url':raw.get('webpage_url') or u}
def guest_probe_sync(u,policy):
    if yt(u):raise RuntimeError('GUEST_YOUTUBE_PROBE_REQUIRES_RELIABILITY')
    raw=None;last='解析失败'
    command=['yt-dlp','--no-playlist','--force-ipv4','--socket-timeout','30','--retries','5','--fragment-retries','5','--extractor-retries','3','--js-runtimes','deno','--impersonate','chrome','--sleep-requests',str(policy['request_sleep_seconds'])]
    result=subprocess.run(command+['--dump-single-json','--no-warnings',u],capture_output=True,text=True,timeout=120)
    if result.returncode==0:raw=json.loads(result.stdout)
    else:last=(result.stderr or result.stdout or last)[-4000:]
    if raw is None:raise RuntimeError(last)
    videos,audios=simplify(raw.get('formats') or [])
    return {'id':raw.get('id'),'title':raw.get('title'),'uploader':raw.get('uploader') or raw.get('channel'),'platform':raw.get('extractor_key') or raw.get('extractor'),'duration':raw.get('duration'),'thumbnail':raw.get('thumbnail'),'is_live':bool(raw.get('is_live') or raw.get('live_status')=='is_live'),'drm':bool(raw.get('has_drm')),'video_options':videos,'audio_options':audios,'subtitles':sorted(set((raw.get('subtitles') or {}))|set((raw.get('automatic_captions') or {}))),'webpage_url':raw.get('webpage_url') or u,'download_strategy':'generic','download_strategy_label':'通用解析'}
def row(i):
    with con() as c:r=c.execute('SELECT * FROM tasks WHERE id=?',(i,)).fetchone()
    if not r:return None
    d=dict(r);d['options']=json.loads(d.pop('options'));return d
def guest_task(i,identity):
    task=row(i)
    if not task or task.get('owner_type')!='guest' or not hmac.compare_digest(str(task.get('owner_id') or ''),str(identity['owner_id'])):raise HTTPException(404,'任务不存在')
    return task
def guest_task_expires_at(task):
    if not task or task.get('status')!='completed' or not task.get('finished'):return None
    try:
        finished=datetime.fromisoformat(task['finished'])
        if finished.tzinfo is None:finished=finished.replace(tzinfo=timezone.utc)
        expires=finished+timedelta(minutes=int(task_policy(task)['retention_minutes']))
        return expires.isoformat()
    except (TypeError,ValueError,OSError):return None
def guest_task_view(task):
    options=task.get('options') if isinstance(task.get('options'),dict) else {}
    return {
        'id':task.get('id'),
        'title':task.get('title'),
        'platform':task.get('platform'),
        'status':task.get('status'),
        'progress':task.get('progress') or 0,
        'speed':task.get('speed') or '',
        'eta':task.get('eta') or '',
        'output_size':task.get('output_size') or 0,
        'error_code':task.get('error_code'),
        'error_message':task.get('error_message'),
        'created':task.get('created'),
        'updated':task.get('updated'),
        'finished':task.get('finished'),
        'expires_at':guest_task_expires_at(task),
        'mode':options.get('mode'),
        'resolution':options.get('resolution'),
        'subtitle_source_language':options.get('subtitle_source_language'),
        'subtitle_target_language':options.get('subtitle_target_language'),
        'subtitle_output_mode':options.get('subtitle_output_mode'),
        'download_available':guest_download_available(task),
    }
def task_directory_size(path):
    try:return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
    except OSError:return 0
def guest_task_size_exceeded(task,path):
    return is_guest_task(task) and task_directory_size(path)>gb_bytes(task_policy(task)['max_file_size_gb'])
def guest_limit_failure(task_or_policy=None):
    if isinstance(task_or_policy,dict) and task_or_policy.get('owner_type')=='guest':
        policy=task_policy(task_or_policy)
    elif isinstance(task_or_policy,dict):
        policy=normalize_guest_policy(task_or_policy)
    else:
        policy=guest_policy()
    return RuntimeError(json.dumps({'code':'guest_file_limit_exceeded','message':f'游客单任务文件最大为 {format_gb(policy["max_file_size_gb"])} GB'},ensure_ascii=False))
def guest_active_count(owner_id=None):
    query="SELECT COUNT(*) AS n FROM tasks WHERE owner_type='guest' AND status IN ('downloading','processing')";args=[]
    if owner_id is not None:query+=' AND owner_id=?';args.append(owner_id)
    with con() as c:return int(c.execute(query,args).fetchone()['n'])
def guest_queue_count(owner_id,connection=None):
    if connection is not None:return int(connection.execute("SELECT COUNT(*) AS n FROM tasks WHERE owner_type='guest' AND owner_id=? AND status='queued'",(owner_id,)).fetchone()['n'])
    with con() as c:return int(c.execute("SELECT COUNT(*) AS n FROM tasks WHERE owner_type='guest' AND owner_id=? AND status='queued'",(owner_id,)).fetchone()['n'])
def guest_ai_hourly_count(owner_id,connection=None):
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    query="SELECT COUNT(*) AS n FROM guest_rate_events WHERE owner_id=? AND event_type=? AND created>=?"
    args=(owner_id,GUEST_AI_EVENT,cutoff)
    if connection is not None:return int(connection.execute(query,args).fetchone()['n'])
    with con() as c:return int(c.execute(query,args).fetchone()['n'])
def guest_translation_hourly_count(owner_id,connection=None):
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    query="SELECT COUNT(*) AS n FROM guest_rate_events WHERE owner_id=? AND event_type=? AND created>=?"
    args=(owner_id,GUEST_TRANSLATION_EVENT,cutoff)
    if connection is not None:return int(connection.execute(query,args).fetchone()['n'])
    with con() as c:return int(c.execute(query,args).fetchone()['n'])
def consume_guest_ai_quota(task):
    if not is_guest_task(task):return
    policy=task_policy(task)
    if not policy['allow_ai_transcription']:
        raise RuntimeError(json.dumps({'code':'GUEST_AI_DISABLED','message':'当前未启用游客 AI 字幕。'},ensure_ascii=False))
    owner_id=str(task.get('owner_id') or '')
    c=con(); c.isolation_level=None
    try:
        c.execute('BEGIN IMMEDIATE')
        used=guest_ai_hourly_count(owner_id,c)
        if used>=policy['ai_transcription_hourly_limit_per_guest']:
            raise RuntimeError(json.dumps({'code':'GUEST_AI_HOURLY_LIMIT','message':f'游客 AI 字幕每小时最多 {int(policy["ai_transcription_hourly_limit_per_guest"])} 次。'},ensure_ascii=False))
        c.execute('INSERT INTO guest_rate_events(id,owner_id,event_type,task_id,created) VALUES(?,?,?,?,?)',(uuid.uuid4().hex,owner_id,GUEST_AI_EVENT,task.get('id'),now()))
        c.execute('COMMIT')
    except Exception:
        try:c.execute('ROLLBACK')
        except sqlite3.Error:pass
        raise
    finally:c.close()
def acquire_guest_ai_slot(task):
    if not is_guest_task(task):return False
    policy=task_policy(task)
    if not policy['allow_ai_transcription']:
        raise RuntimeError(json.dumps({'code':'GUEST_AI_DISABLED','message':'当前未启用游客 AI 字幕。'},ensure_ascii=False))
    with LOCK:
        if len(GUEST_AI_ACTIVE)>=policy['ai_transcription_global_concurrency']:
            raise RuntimeError(json.dumps({'code':'GUEST_AI_BUSY','message':'游客 AI 字幕正在处理中，请稍后重试。'},ensure_ascii=False))
        GUEST_AI_ACTIVE.add(task['id'])
    return True
def start_guest_ai(task):
    if not is_guest_task(task):return False
    acquire_guest_ai_slot(task)
    try:consume_guest_ai_quota(task)
    except Exception:
        release_guest_ai_slot(task['id']);raise
    return True
def release_guest_ai_slot(task_id):
    with LOCK:GUEST_AI_ACTIVE.discard(task_id)
def start_guest_translation(task):
    if not is_guest_task(task):return False
    policy=task_policy(task)
    if not policy.get('allow_subtitle_translation',True):
        raise RuntimeError(json.dumps({'code':'GUEST_TRANSLATION_DISABLED','message':'当前未启用游客字幕翻译。'},ensure_ascii=False))
    with LOCK:
        if len(SUBTITLE_TRANSLATION_ACTIVE)>=int(policy['subtitle_translation_global_concurrency']):
            raise RuntimeError(json.dumps({'code':'GUEST_TRANSLATION_BUSY','message':'字幕翻译正在处理中，请稍后重试。'},ensure_ascii=False))
        SUBTITLE_TRANSLATION_ACTIVE.add(task['id'])
    owner_id=str(task.get('owner_id') or '')
    c=con(); c.isolation_level=None
    try:
        c.execute('BEGIN IMMEDIATE')
        used=guest_translation_hourly_count(owner_id,c)
        limit=int(policy['subtitle_translation_hourly_limit_per_guest'])
        if used>=limit:
            raise RuntimeError(json.dumps({'code':'GUEST_TRANSLATION_HOURLY_LIMIT','message':f'游客字幕翻译每小时最多 {limit} 次。'},ensure_ascii=False))
        c.execute('INSERT INTO guest_rate_events(id,owner_id,event_type,task_id,created) VALUES(?,?,?,?,?)',(uuid.uuid4().hex,owner_id,GUEST_TRANSLATION_EVENT,task.get('id'),now()))
        c.execute('COMMIT')
    except Exception:
        try:c.execute('ROLLBACK')
        except sqlite3.Error:pass
        with LOCK:SUBTITLE_TRANSLATION_ACTIVE.discard(task['id'])
        raise
    finally:c.close()
    return True
def release_guest_translation_slot(task_id):
    with LOCK:SUBTITLE_TRANSLATION_ACTIVE.discard(task_id)
def patch(i,**v):
    if not v:return
    v['updated']=now(); q=','.join(k+'=?' for k in v)
    with con() as c:c.execute('UPDATE tasks SET '+q+' WHERE id=?',(*v.values(),i))
def cmd(t,cp,compat=False):
    o=t['options']; policy=task_policy(t); work=TMP/t['id']; out=str(work/'%(title).180B [%(id)s].%(ext)s'); mode=o.get('mode','video')
    if mode=='live':
        if is_guest_task(t):raise RuntimeError(json.dumps({'code':'guest_live_not_allowed','message':'游客不支持直播下载'},ensure_ascii=False))
        a=['streamlink','--force','--output',str(work/'live.ts')];
        if settings().get('proxy_url'):a+=['--http-proxy',settings()['proxy_url']]
        return a+[t['url'],o.get('stream_quality','best')]
    a=task_base(t,None if is_guest_task(t) else cp,compat)+['--newline','--restrict-filenames','--paths',f'temp:{work}','--output',out,'--max-filesize',f"{policy['max_file_size_gb']}G",'--progress-template','download:PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s']
    fid=str(o.get('format_id') or '') if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(o.get('format_id') or '')) else ''
    if mode=='thumbnail':a+=['--skip-download','--write-thumbnail','--convert-thumbnails','jpg']
    elif mode=='audio':
        a+=['--format',fid or 'bestaudio/best'];fmt=o.get('audio_format','original');a+=(['--extract-audio','--audio-format',fmt] if fmt in ('mp3','m4a','opus','wav','flac') else [])
    elif mode=='subtitles':a+=['--skip-download','--write-subs','--write-auto-subs','--sub-langs',','.join(o.get('subtitle_languages') or ['zh-CN','zh','en']),'--convert-subs','srt']
    else:
        if fid:a+=['--format',fid if o.get('format_has_audio') else fid+'+bestaudio/best']
        else:a+=['--format',f"bv*[height<={min(int(o.get('resolution',1080)),int(policy['max_resolution']))}]+ba/b"]
        a+=['--merge-output-format','mp4']
    if mode not in ('thumbnail','subtitles') and o.get('write_thumbnail'):a+=['--write-thumbnail']
    if mode not in ('thumbnail','subtitles') and o.get('embed_metadata',True):a+=['--embed-metadata']
    return a+[t['url']]
def move(i):
    src=TMP/i;fs=[p for p in src.rglob('*') if p.is_file()]
    if not fs:raise RuntimeError('任务完成但没有输出文件')
    dst=DL/i;dst.mkdir(parents=True,exist_ok=True)
    for p in fs:shutil.move(str(p),dst/p.name)
    shutil.rmtree(src,ignore_errors=True);p=max(dst.iterdir(),key=lambda x:x.stat().st_size);return str(p),sum(x.stat().st_size for x in dst.iterdir())
def execute(i):
    t=row(i);cp=None;logs=[]
    if not t or t.get('status')=='cancelled':return
    try:
        policy=task_policy(t);free=shutil.disk_usage(ROOT).free
        if free<gb_bytes(policy['min_free_gb']):
            if is_guest_task(t):raise RuntimeError(json.dumps({'code':'guest_disk_space_low','message':'游客任务暂不可用：服务器剩余空间不足'},ensure_ascii=False))
            raise RuntimeError('no space')
        cp=cpath(t['options'].get('cookie_id')) if not is_guest_task(t) else None;(TMP/i).mkdir(parents=True,exist_ok=True);patch(i,status='downloading',progress=0,error_code=None,error_message=None,log_tail='')
        rc=1
        for n,compat in enumerate([False,True] if yt(t['url']) else [False]):
            if n:logs.append('[Media Hub] 首次请求被拒绝，正在切换 YouTube 兼容客户端重试……')
            p=subprocess.Popen(cmd(t,cp,compat),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
            with LOCK:ACTIVE[i]=p
            for line in p.stdout:
                s=line.strip();logs=(logs+[s])[-120:] if s else logs;m=re.search(r'PROGRESS:\s*([0-9.]+)%\|([^|]*)\|([^|]*)',s)
                if m:patch(i,progress=float(m.group(1)),speed=m.group(2),eta=m.group(3),log_tail='\n'.join(logs))
                if guest_task_size_exceeded(t,TMP/i):
                    os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=10);raise guest_limit_failure(t)
                if row(i)['status']=='cancelled':os.killpg(p.pid,signal.SIGTERM);break
            rc=p.wait()
            if row(i)['status']=='cancelled':shutil.rmtree(TMP/i,ignore_errors=True);return
            if rc==0 or not denied('\n'.join(logs)):break
        if rc:code,msg=err('\n'.join(logs),t['url']);raise RuntimeError(json.dumps({'code':code,'message':msg},ensure_ascii=False))
        path,size=move(i)
        if (row(i) or {}).get('status')=='cancelled':shutil.rmtree(DL/i,ignore_errors=True);return
        if is_guest_task(t) and size>gb_bytes(policy['max_file_size_gb']):shutil.rmtree(DL/i,ignore_errors=True);raise guest_limit_failure(t)
        patch(i,status='completed',progress=100,output_path=path,output_size=size,finished=now(),log_tail='\n'.join(logs))
    except Exception as e:
        try:x=json.loads(str(e));code,msg=x['code'],x['message']
        except:code,msg=err(str(e),t['url'])
        if is_guest_task(t) and code not in {'guest_file_limit_exceeded','guest_disk_space_low','guest_live_not_allowed'}:msg='游客任务下载失败，请确认链接为公开可访问的媒体后重试。'
        if code=='guest_file_limit_exceeded':shutil.rmtree(TMP/i,ignore_errors=True);shutil.rmtree(DL/i,ignore_errors=True)
        patch(i,status='failed',error_code=code,error_message=msg,finished=now(),log_tail='\n'.join(logs))
    finally:
        if cp:cp.unlink(missing_ok=True)
        with LOCK:ACTIVE.pop(i,None)
def cleanup_expired(guest_only=False):
    with con() as c:rows=[dict(item) for item in c.execute("SELECT id,finished,owner_type,policy_snapshot FROM tasks WHERE status='completed'")]
    current=datetime.now(timezone.utc)
    for item in rows:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(item['id'])):continue
        if guest_only and item.get('owner_type')!='guest':continue
        try:
            finished=datetime.fromisoformat(item['finished']) if item.get('finished') else None
            if not finished:continue
            if finished.tzinfo is None:finished=finished.replace(tzinfo=timezone.utc)
            if item.get('owner_type')=='guest':
                task={'owner_type':'guest','policy_snapshot':item.get('policy_snapshot')};expires=finished+timedelta(minutes=task_policy(task)['retention_minutes'])
            else:expires=finished+timedelta(hours=int(settings()['retention_hours']))
            if current<expires:continue
            for directory in (DL/item['id'],TMP/item['id']):
                if directory.exists():shutil.rmtree(directory)
            with con() as c:c.execute("UPDATE tasks SET status='expired',output_path=NULL,output_size=NULL,updated=? WHERE id=?",(now(),item['id']))
        except Exception as exc:
            print(f"[Cleanup] task={item.get('id')} owner={item.get('owner_type') or 'unknown'} error={type(exc).__name__}: {str(exc)[:200]}",flush=True)
            continue
def cleanup_rate_events():
    cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
    try:
        with con() as c:c.execute('DELETE FROM guest_rate_events WHERE created<?',(cutoff,))
    except Exception as exc:
        print(f'[Cleanup] task=rate_events owner=guest error={type(exc).__name__}: {str(exc)[:200]}',flush=True)
def guest_emergency_cleanup():
    cleanup_expired(guest_only=True)
    with con() as c:rows=[dict(item) for item in c.execute("SELECT id,status FROM tasks WHERE owner_type='guest'")]
    with LOCK:active=set(ACTIVE)
    for item in rows:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(item['id'])):continue
        if item['id'] in active or item['status'] in {'downloading','processing'}:continue
        directory=TMP/item['id']
        try:
            if directory.is_dir():shutil.rmtree(directory)
        except OSError as exc:
            print(f"[Cleanup] task={item.get('id')} owner=guest error={type(exc).__name__}: {str(exc)[:200]}",flush=True)
            continue
def next_queued_task():
    with con() as c:
        admin=c.execute("SELECT id FROM tasks WHERE status='queued' AND owner_type='admin' ORDER BY created LIMIT 1").fetchone()
        guests=[item['id'] for item in c.execute("SELECT id FROM tasks WHERE status='queued' AND owner_type='guest' ORDER BY created")]
    if admin:return admin['id']
    for task_id in guests:
        task=row(task_id)
        if not task:continue
        policy=task_policy(task);free=shutil.disk_usage(ROOT).free
        if free<gb_bytes(policy['emergency_free_gb']):guest_emergency_cleanup();return None
        if free<gb_bytes(policy['min_free_gb']):return None
        if guest_active_count()>=policy['global_guest_concurrency'] or guest_active_count(task['owner_id'])>=policy['max_active_tasks_per_guest']:continue
        return task_id
    return None
def worker():
    while not STOP.is_set():
        if not ACTIVE:
            task_id=next_queued_task()
            if task_id:execute(task_id);continue
        STOP.wait(1)
def cleanup():
    while not STOP.is_set():
        try:
            cleanup_expired();cleanup_rate_events()
        except Exception as exc:
            print(f'[Cleanup] task=thread owner=system error={type(exc).__name__}: {str(exc)[:200]}',flush=True)
        STOP.wait(60)

class KoofrError(RuntimeError):
    def __init__(self,message,status_code=409):super().__init__(message);self.status_code=status_code

KOOFR_CATEGORIES={'video':'Videos','audio':'Audio','thumbnail':'Covers','live':'Live','subtitles':'Subtitles'}
KOOFR_MARKER='.media-download-complete'
KOOFR_CONTAINER_PATH=Path('/mnt/koofr').resolve()
KOOFR_REMOTE_FS={'fuse.rclone'}
KOOFR_ROOT_CACHE_TTL=8
KOOFR_ROOT_CACHE={'key':None,'expires':0.0,'root':None,'error':None,'status_code':503}
KOOFR_HEALTH_CACHE_TTL=60
KOOFR_HEALTH_CACHE={'key':None,'expires':0.0,'value':None}

def _invalidate_koofr_health():
    with LOCK:KOOFR_HEALTH_CACHE.update(key=None,expires=0.0,value=None)

def _inside(base:Path,path:Path):
    try:path.relative_to(base);return True
    except ValueError:return False
def guest_download_available(task):
    if not task or task.get('status')!='completed':return False
    path=Path(task['output_path']).resolve() if task.get('output_path') else None
    try:return bool(path and path.is_file() and _inside(DL.resolve(),path))
    except (OSError,ValueError,TypeError):return False
def _mount_unescape(value):
    return re.sub(r'\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),value)

def _koofr_mount_info(path):
    mountinfo=Path('/proc/self/mountinfo')
    if not mountinfo.is_file():return None
    target=path.resolve();best=None
    try:
        for line in mountinfo.read_text(encoding='utf-8').splitlines():
            if ' - ' not in line:continue
            left,right=line.split(' - ',1);fields=left.split();details=right.split()
            if len(fields)<6 or len(details)<2:continue
            mount_point=Path(_mount_unescape(fields[4])).resolve()
            if _inside(mount_point,target) and (best is None or len(mount_point.parts)>len(best[0].parts)):
                best=(mount_point,details[0],details[1])
    except (OSError,ValueError):return None
    return best

def _koofr_root_uncached():
    raw=os.getenv('KOOFR_ROOT','').strip() or '/mnt/koofr/Media-Download'
    host_path=os.getenv('KOOFR_HOST_PATH','').strip() or '/mnt/koofr'
    candidate=Path(raw).expanduser()
    if not candidate.is_absolute():raise KoofrError('KOOFR_ROOT 必须是容器内绝对路径',503)
    root=candidate.resolve()
    if not _inside(KOOFR_CONTAINER_PATH,root):raise KoofrError('KOOFR_ROOT 必须位于 /mnt/koofr 挂载目录内',503)
    mount=_koofr_mount_info(root)
    if not mount:raise KoofrError(f'Koofr 未挂载或未能检测到真实挂载，请确认宿主机路径 {host_path} 已挂载',503)
    _,fstype,source=mount
    remote=fstype in KOOFR_REMOTE_FS or any(x in source.lower() for x in ('koofr','rclone'))
    if not remote:raise KoofrError(f'检测到的是本地文件系统（{fstype}），不是 Koofr 挂载',503)
    try:root.mkdir(parents=True,exist_ok=True)
    except OSError as exc:raise KoofrError(f'Koofr 项目目录创建失败：{exc}',503)
    if not root.is_dir() or not os.access(root,os.R_OK|os.W_OK|os.X_OK):raise KoofrError(f'Koofr 根目录不可写：{root}',503)
    return root

def _koofr_root(force=False):
    key=(os.getenv('KOOFR_ROOT','').strip(),os.getenv('KOOFR_HOST_PATH','').strip())
    current=time.monotonic()
    if not force:
        with LOCK:
            if KOOFR_ROOT_CACHE['key']==key and KOOFR_ROOT_CACHE['expires']>current:
                if KOOFR_ROOT_CACHE['error']:raise KoofrError(KOOFR_ROOT_CACHE['error'],KOOFR_ROOT_CACHE['status_code'])
                return KOOFR_ROOT_CACHE['root']
    try:root=_koofr_root_uncached()
    except KoofrError as exc:
        with LOCK:KOOFR_ROOT_CACHE.update(key=key,expires=time.monotonic()+KOOFR_ROOT_CACHE_TTL,root=None,error=str(exc),status_code=exc.status_code)
        raise
    with LOCK:KOOFR_ROOT_CACHE.update(key=key,expires=time.monotonic()+KOOFR_ROOT_CACHE_TTL,root=root,error=None,status_code=503)
    return root

def _koofr_write_probe(root):
    probe=root/f'.media-download-health-{secrets.token_hex(8)}.tmp'
    try:
        with probe.open('xb') as handle:
            handle.write(b'ok');handle.flush();os.fsync(handle.fileno())
    except OSError as exc:
        try:probe.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            try:probe.unlink(missing_ok=True)
            except OSError as retry_exc:
                raise KoofrError(f'Koofr 实际写入测试失败：{exc}；测试文件清理失败，残留路径：{probe}（首次：{cleanup_exc}；重试：{retry_exc}）',503)
            raise KoofrError(f'Koofr 实际写入测试失败：{exc}；测试文件首次清理失败但重试成功：{probe}（{cleanup_exc}）',503)
        raise KoofrError(f'Koofr 实际写入测试失败：{exc}',503)
    try:
        probe.unlink(missing_ok=True)
    except OSError as exc:
        try:probe.unlink(missing_ok=True)
        except OSError as retry_exc:
            raise KoofrError(f'Koofr 测试文件删除失败，残留路径：{probe}（首次：{exc}；重试：{retry_exc}）',503)
        raise KoofrError(f'Koofr 测试文件首次删除失败但重试成功：{probe}（{exc}）',503)

def _koofr_health():
    key=(os.getenv('KOOFR_ROOT','').strip(),os.getenv('KOOFR_HOST_PATH','').strip())
    current=time.monotonic()
    with LOCK:
        if KOOFR_HEALTH_CACHE['key']==key and KOOFR_HEALTH_CACHE['expires']>current and KOOFR_HEALTH_CACHE['value'] is not None:return dict(KOOFR_HEALTH_CACHE['value'])
    try:
        root=_koofr_root(force=True)
    except (KoofrError,OSError) as exc:
        value={'mounted':False,'writable':False,'root':'','total':0,'free':0,'error':str(exc)}
    else:
        try:
            _koofr_write_probe(root);usage=shutil.disk_usage(root)
            value={'mounted':True,'writable':True,'root':str(root),'total':usage.total,'free':usage.free,'error':''}
        except (KoofrError,OSError) as exc:
            value={'mounted':True,'writable':False,'root':str(root),'total':0,'free':0,'error':str(exc)}
    with LOCK:KOOFR_HEALTH_CACHE.update(key=key,expires=time.monotonic()+KOOFR_HEALTH_CACHE_TTL,value=value)
    return dict(value)

def _koofr_target(task,force_mount_check=False):
    task_id=str(task.get('id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id):raise KoofrError('任务目录标识无效')
    mode=str((task.get('options') or {}).get('mode') or 'video')
    category=KOOFR_CATEGORIES.get(mode,KOOFR_CATEGORIES['video'])
    root=_koofr_root(force=force_mount_check);target=(root/category/task_id).resolve()
    if not _inside(root,target):raise KoofrError('Koofr 目标路径非法')
    return root,target

def _local_output_files(task_id):
    base=DL.resolve();source=(DL/str(task_id)).resolve()
    if not _inside(base,source) or not source.is_dir():raise KoofrError('本地任务文件不存在，无法保存到 Koofr',404)
    files=[]
    for path in source.rglob('*'):
        if not path.is_file():continue
        resolved=path.resolve()
        if not _inside(source,resolved):raise KoofrError('本地输出包含非法路径')
        files.append((path.relative_to(source),path))
    if not files:raise KoofrError('本地任务没有可保存的输出文件',404)
    return source,files

def _koofr_files(target):
    if not target.is_dir():return []
    files=[]
    for path in target.rglob('*'):
        if path.name in {KOOFR_MARKER,KOOFR_MARKER+'.tmp'} or not path.is_file():continue
        resolved=path.resolve()
        if not _inside(target,resolved):raise KoofrError('Koofr 副本包含非法路径')
        files.append((path.relative_to(target),path))
    return sorted(files,key=lambda item:str(item[0]))

def _koofr_payload(task):
    try:
        root,target=_koofr_target(task)
        files=_koofr_files(target)
        size=sum(path.stat().st_size for _,path in files)
        return {'saved':bool(files) and (target/KOOFR_MARKER).is_file(),'path':str(target) if files else '','files':[str(rel) for rel,_ in files],'size':size}
    except (KoofrError,OSError) as exc:
        return {'saved':False,'path':'','files':[],'size':0,'error':str(exc)}

def save_task_to_koofr(task_id,allow_processing_subtitles=False):
    task=row(task_id)
    if not task:raise KoofrError('任务不存在',404)
    if is_guest_task(task):raise KoofrError('游客任务不支持保存到 Koofr',403)
    processing_subtitles=task['status']=='processing' and (task.get('options') or {}).get('mode')=='subtitles'
    if task['status'] not in {'completed','expired'} and not (allow_processing_subtitles and processing_subtitles):raise KoofrError('只有 completed 或 expired 任务可以保存到 Koofr')
    _,sources=_local_output_files(task_id);root,target=_koofr_target(task,force_mount_check=True)
    total=sum(path.stat().st_size for _,path in sources)
    try:
        target.mkdir(parents=True,exist_ok=True);target=target.resolve()
        if not _inside(root,target):raise KoofrError('Koofr 目标路径非法')
        if shutil.disk_usage(root).free<total:raise KoofrError('Koofr 空间不足',507)
        for relative,source in sources:
            destination=(target/relative).resolve()
            if not _inside(target,destination):raise KoofrError('Koofr 目标路径非法')
            destination.parent.mkdir(parents=True,exist_ok=True)
            if not destination.exists() or destination.stat().st_size!=source.stat().st_size:shutil.copy2(source,destination)
        marker_tmp=target/(KOOFR_MARKER+'.tmp')
        marker_tmp.write_text('complete\n',encoding='utf-8');marker_tmp.replace(target/KOOFR_MARKER)
    except KoofrError:raise
    except OSError as exc:raise KoofrError(f'保存到 Koofr 失败：{exc}')
    return _koofr_payload(task)

def auto_save_subtitles_to_koofr(task_id):
    try:save_task_to_koofr(task_id,allow_processing_subtitles=True)
    except Exception as exc:
        task=row(task_id)
        if task:
            message=f'字幕下载完成，但保存到 Koofr 失败：{str(exc)}'
            old=str(task.get('log_tail') or '')
            if message not in old:patch(task_id,log_tail=(old+'\n[Koofr] '+message)[-12000:])
    finally:_invalidate_koofr_health()

def _koofr_zip_name(task):
    name=re.sub(r'[\\/:*?"<>|\r\n]+','_',str(task.get('title') or 'media'))
    return (name.strip(' ._')[:100] or 'media')+'-koofr.zip'

def insert_task(url,title,platform,options,owner_type='admin',owner_id='admin',policy_snapshot=None):
    task_id=uuid.uuid4().hex;created=now()
    with con() as c:c.execute('INSERT INTO tasks(id,url,title,platform,status,options,created,updated,owner_type,owner_id,policy_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(task_id,url,title,platform,'queued',json.dumps(options),created,created,owner_type,owner_id,json.dumps(policy_snapshot) if policy_snapshot else None))
    return row(task_id)
def insert_guest_task_atomic(url,title,platform,options,identity,policy):
    free=shutil.disk_usage(ROOT).free
    if free<gb_bytes(policy['emergency_free_gb']):guest_emergency_cleanup();free=shutil.disk_usage(ROOT).free
    if free<gb_bytes(policy['min_free_gb']):guest_error('guest_disk_space_low','服务器剩余空间不足，暂不接受游客任务',503)
    task_id=uuid.uuid4().hex;created=now();owner_id=identity['owner_id']
    c=con(); c.isolation_level=None
    try:
        c.execute('BEGIN IMMEDIATE')
        if guest_queue_count(owner_id,c)>=policy['max_queued_tasks_per_guest']:
            raise HTTPException(429,detail={'code':'guest_queue_limit_exceeded','message':f'游客最多保留 {int(policy["max_queued_tasks_per_guest"])} 个排队任务'})
        c.execute('INSERT INTO tasks(id,url,title,platform,status,options,created,updated,owner_type,owner_id,policy_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(task_id,url,title,platform,'queued',json.dumps(options),created,created,'guest',owner_id,json.dumps(policy)))
        c.execute('COMMIT')
    except Exception:
        try:c.execute('ROLLBACK')
        except sqlite3.Error:pass
        raise
    finally:c.close()
    return row(task_id)
def retry_guest_task_atomic(task_id,identity,policy):
    free=shutil.disk_usage(ROOT).free
    if free<gb_bytes(policy['min_free_gb']):guest_error('guest_disk_space_low','服务器剩余空间不足，暂不接受游客任务',503)
    stamp=now()
    c=con(); c.isolation_level=None
    try:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute("SELECT id,status,owner_type,owner_id FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not current or current['owner_type']!='guest' or not hmac.compare_digest(str(current['owner_id'] or ''),str(identity['owner_id'])):raise HTTPException(404,'任务不存在')
        if current['status']!='queued' and guest_queue_count(identity['owner_id'],c)>=policy['max_queued_tasks_per_guest']:
            raise HTTPException(429,detail={'code':'guest_queue_limit_exceeded','message':f'游客最多保留 {int(policy["max_queued_tasks_per_guest"])} 个排队任务'})
        c.execute("UPDATE tasks SET status='queued',progress=0,error_code=NULL,error_message=NULL,output_path=NULL,output_size=NULL,finished=NULL,log_tail='',speed='',eta='',updated=? WHERE id=? AND owner_type='guest' AND owner_id=?",(stamp,task_id,identity['owner_id']))
        c.execute('COMMIT')
    except Exception:
        try:c.execute('ROLLBACK')
        except sqlite3.Error:pass
        raise
    finally:c.close()
    return row(task_id)
def _probe_number(value):
    try:return int(value)
    except (TypeError,ValueError):return 0
def guest_task_options(payload,probed,policy):
    incoming=payload.options if isinstance(payload.options,dict) else {};mode=str(incoming.get('mode') or 'video')
    if mode not in {'video','audio','thumbnail','subtitles'}:guest_error('guest_mode_not_allowed','游客不支持该下载类型')
    if probed.get('is_live') or mode=='live':guest_error('guest_live_not_allowed','游客不支持直播下载')
    duration=_probe_number(probed.get('duration'))
    if not duration:guest_error('guest_duration_unknown','游客任务需要可确认的媒体时长')
    if duration>policy['max_video_duration_minutes']*60:
        guest_error('guest_duration_limit_exceeded',f'游客媒体最长支持 {int(policy["max_video_duration_minutes"])} 分钟')
    limit=gb_bytes(policy['max_file_size_gb']);video_options=probed.get('video_options') or [];audio_options=probed.get('audio_options') or []
    format_id=str(incoming.get('format_id') or '')
    strategy=str(probed.get('download_strategy') or '') or None
    file_limit_msg=f'预计文件超过游客 {format_gb(policy["max_file_size_gb"])} GB 限制'
    if mode=='video':
        selected=next((item for item in video_options if str(item.get('format_id') or '')==format_id),None) if format_id else None
        if format_id and not selected:guest_error('guest_format_not_allowed','所选视频格式不可用')
        requested=_probe_number(incoming.get('resolution')) or policy['default_resolution']
        height=_probe_number(selected.get('height')) if selected else requested
        if height>policy['max_resolution']:
            guest_error('guest_resolution_limit_exceeded',f'游客最高只支持 {int(policy["max_resolution"])}p')
        if selected and _probe_number(selected.get('filesize'))>limit:guest_error('guest_file_limit_exceeded',file_limit_msg)
        candidates=[_probe_number(item.get('filesize')) for item in video_options if _probe_number(item.get('height'))<=height and _probe_number(item.get('filesize'))]
        if not selected and candidates and min(candidates)>limit:
            guest_error('guest_file_limit_exceeded',f'当前清晰度预计文件超过游客 {format_gb(policy["max_file_size_gb"])} GB 限制')
        options={'mode':'video','resolution':height,'format_id':str(selected.get('format_id')) if selected else None,'format_has_audio':bool(selected.get('has_audio')) if selected else False,'write_thumbnail':False,'embed_metadata':True,'cookie_id':None,'youtube_strategy':strategy}
    elif mode=='audio':
        selected=next((item for item in audio_options if str(item.get('format_id') or '')==format_id),None) if format_id else None
        if format_id and not selected:guest_error('guest_format_not_allowed','所选音频格式不可用')
        if selected and _probe_number(selected.get('filesize'))>limit:guest_error('guest_file_limit_exceeded',file_limit_msg)
        audio_format=str(incoming.get('audio_format') or 'original');options={'mode':'audio','format_id':str(selected.get('format_id')) if selected else None,'audio_format':audio_format if audio_format in {'original','mp3','m4a','opus','wav','flac'} else 'original','write_thumbnail':False,'embed_metadata':True,'cookie_id':None,'youtube_strategy':strategy}
    elif mode=='thumbnail':options={'mode':'thumbnail','cookie_id':None,'youtube_strategy':strategy}
    else:
        subtitle_opts=subtitle_options_from_payload(incoming,policy,guest=True)
        ai_candidate=not bool(probed.get('subtitles'))
        if ai_candidate:
            if not policy['allow_ai_transcription']:
                guest_error('guest_ai_disabled','当前未启用游客 AI 字幕')
            if duration>policy['ai_transcription_max_duration_minutes']*60:
                guest_error('guest_ai_duration_limit_exceeded',f'无平台字幕时，游客 AI 字幕最长支持 {int(policy["ai_transcription_max_duration_minutes"])} 分钟')
        if subtitle_opts['subtitle_output_mode']!='original':
            if not policy.get('allow_subtitle_translation',True):
                guest_error('guest_translation_disabled','当前未启用游客字幕翻译')
            if duration>policy['subtitle_translation_max_duration_minutes']*60:
                guest_error('guest_translation_duration_limit',f'该视频超过游客字幕翻译时长限制（{int(policy["subtitle_translation_max_duration_minutes"])} 分钟）')
        options={'mode':'subtitles','cookie_id':None,'guest_ai_candidate':ai_candidate,'youtube_strategy':strategy,**subtitle_opts}
    return options
def admit_guest_disk(policy):
    free=shutil.disk_usage(ROOT).free
    if free<gb_bytes(policy['emergency_free_gb']):guest_emergency_cleanup();free=shutil.disk_usage(ROOT).free
    if free<gb_bytes(policy['min_free_gb']):guest_error('guest_disk_space_low','服务器剩余空间不足，暂不接受游客任务',503)

class Pass(BaseModel):password:str=Field(min_length=1,max_length=256)
class PasswordChange(BaseModel):current_password:str=Field(min_length=1,max_length=256);new_password:str=Field(min_length=8,max_length=256)
class Probe(BaseModel):url:str=Field(min_length=8,max_length=2048);cookie_id:str|None=None
class Task(BaseModel):url:str;title:str|None=None;platform:str|None=None;options:dict[str,Any]={}
@asynccontextmanager
async def life(_):
    init();bootstrap_admin_password();recover_interrupted_tasks();STOP.clear();threading.Thread(target=worker,daemon=True).start();threading.Thread(target=cleanup,daemon=True).start();yield;STOP.set()
app=FastAPI(lifespan=life)
@app.get('/api/admin/health',dependencies=[Depends(auth)])
@app.get('/api/health',dependencies=[Depends(auth)])
def health():
    u=shutil.disk_usage(ROOT);tools={n:bool(shutil.which(n)) for n in ('yt-dlp','streamlink','ffmpeg','deno')};return {'ok':all(tools.values()),'port':PORT,'tools':tools,'disk':{'total':u.total,'used':u.used,'free':u.free,'percent':round(u.used/u.total*100,1)},'koofr':_koofr_health()}
@app.get('/api/admin/auth/status')
@app.get('/api/auth/status')
def ast():return {'setup_required':False,'using_default_password':using_default_password(),'session_version':admin_session_version()}
@app.post('/api/auth/setup')
def setup(b:Pass):
    if meta('password'):raise HTTPException(409,'密码已设置')
    if len(b.password)<8:raise HTTPException(400,'密码至少 8 位')
    setmeta('password',hpw(b.password));return {'token':admin_token()}
@app.post('/api/admin/login')
@app.post('/api/auth/login')
def login(b:Pass):
    if not vpw(b.password,meta('password') or ''):raise HTTPException(401,'密码错误')
    return {'token':admin_token()}
@app.post('/api/admin/change-password',dependencies=[Depends(auth)])
def change_password(b:PasswordChange):
    current=meta('password') or ''
    if not vpw(b.current_password,current):raise HTTPException(401,'当前密码错误')
    if hmac.compare_digest(b.current_password,b.new_password):raise HTTPException(400,'新密码不能与当前密码相同')
    setmeta('password',hpw(b.new_password));setmeta('admin_session_version',str(admin_session_version()+1));return {'ok':True}
@app.post('/api/admin/probe',dependencies=[Depends(auth)])
@app.post('/api/probe',dependencies=[Depends(auth)])
async def probe(b:Probe):
    u=await validate(b.url)
    try:return await asyncio.to_thread(probe_sync,u,b.cookie_id)
    except Exception as e:code,msg=err(str(e),u);raise HTTPException(400,detail={'code':code,'message':msg})
@app.post('/api/admin/tasks',dependencies=[Depends(auth)])
@app.post('/api/tasks',dependencies=[Depends(auth)])
async def create(b:Task):
    u=await validate(b.url)
    options=b.options if isinstance(b.options,dict) else {}
    if str(options.get('mode') or '')=='subtitles':
        options={**options,**subtitle_options_from_payload(options,guest=False)}
        options['cookie_id']=options.get('cookie_id')
    return insert_task(u,b.title,b.platform,options,'admin','admin')
@app.get('/api/admin/tasks',dependencies=[Depends(auth)])
@app.get('/api/tasks',dependencies=[Depends(auth)])
def tasks(include_koofr:bool=False):
    with con() as c:ids=[x['id'] for x in c.execute('SELECT id FROM tasks ORDER BY created DESC LIMIT 200')]
    output=[]
    for i in ids:
        task=row(i)
        if task and include_koofr and task['status'] in {'completed','expired'}:task['koofr']=_koofr_payload(task)
        if task:output.append(task)
    return output
@app.get('/api/admin/tasks/{i}',dependencies=[Depends(auth)])
def admin_task(i):
    task=row(i)
    if not task:raise HTTPException(404,'任务不存在')
    return task
@app.get('/api/guest/health')
def guest_health(identity:dict=Depends(guest_identity)):
    policy=guest_policy();free=shutil.disk_usage(ROOT).free
    with con() as c:queued=int(c.execute("SELECT COUNT(*) AS n FROM tasks WHERE owner_type='guest' AND status='queued'").fetchone()['n'])
    return {
        'ok':True,
        'accepting_guest_tasks':free>=gb_bytes(policy['min_free_gb']) and guest_active_count()<policy['global_guest_concurrency'],
        'queue_length':queued,
        'limits':{
            'max_file_size_gb':policy['max_file_size_gb'],
            'default_resolution':policy['default_resolution'],
            'max_resolution':policy['max_resolution'],
            'retention_minutes':policy['retention_minutes'],
            'max_video_duration_minutes':policy['max_video_duration_minutes'],
            'allow_ai_transcription':policy['allow_ai_transcription'],
            'ai_transcription_max_duration_minutes':policy['ai_transcription_max_duration_minutes'],
            'allow_subtitle_translation':policy['allow_subtitle_translation'],
            'subtitle_translation_max_duration_minutes':policy['subtitle_translation_max_duration_minutes'],
            'subtitle_translation_hourly_limit_per_guest':policy['subtitle_translation_hourly_limit_per_guest'],
            'supported_subtitle_languages':dict(SUBTITLE_LANGUAGES),
        },
    }
@app.post('/api/guest/probe')
async def guest_probe(b:Probe,identity:dict=Depends(guest_identity)):
    u=await validate(b.url);policy=guest_policy()
    try:result=await asyncio.to_thread(guest_probe_sync,u,policy)
    except Exception:raise HTTPException(400,detail={'code':'guest_probe_failed','message':'游客解析失败，请确认链接为公开可访问的媒体。'})
    cache_guest_probe(identity['owner_id'],u,result)
    allowed=('id','title','uploader','platform','duration','thumbnail','is_live','drm','video_options','audio_options','subtitles','webpage_url','download_strategy','download_strategy_label')
    return {key:result.get(key) for key in allowed}
@app.post('/api/guest/tasks')
async def create_guest_task(b:Task,identity:dict=Depends(guest_identity)):
    u=await validate(b.url);probed=cached_guest_probe(identity['owner_id'],u)
    if not probed:guest_error('guest_probe_required','请先使用游客解析接口重新解析链接',409)
    policy=guest_policy();options=guest_task_options(b,probed,policy);admit_guest_disk(policy)
    task=insert_guest_task_atomic(u,probed.get('title'),probed.get('platform'),options,identity,policy)
    return guest_task_view(task)
@app.get('/api/guest/tasks')
def guest_tasks(identity:dict=Depends(guest_identity)):
    with con() as c:ids=[item['id'] for item in c.execute("SELECT id FROM tasks WHERE owner_type='guest' AND owner_id=? ORDER BY created DESC LIMIT 200",(identity['owner_id'],))]
    return [guest_task_view(task) for item in ids if (task:=row(item))]
@app.get('/api/guest/tasks/{i}')
def guest_task_detail(i,identity:dict=Depends(guest_identity)):return guest_task_view(guest_task(i,identity))
@app.post('/api/guest/tasks/{i}/cancel')
def guest_cancel(i,identity:dict=Depends(guest_identity)):
    task=guest_task(i,identity);patch(i,status='cancelled',finished=now());process=ACTIVE.get(i)
    if process:
        try:os.killpg(process.pid,signal.SIGTERM)
        except OSError:pass
    return guest_task_view(row(task['id']))
@app.post('/api/guest/tasks/{i}/retry')
def guest_retry(i,identity:dict=Depends(guest_identity)):
    task=guest_task(i,identity);policy=task_policy(task)
    return guest_task_view(retry_guest_task_atomic(task['id'],identity,policy))
@app.delete('/api/guest/tasks/{i}')
def guest_delete(i,identity:dict=Depends(guest_identity)):
    task=guest_task(i,identity);process=ACTIVE.get(task['id'])
    if process:
        try:os.killpg(process.pid,signal.SIGTERM)
        except OSError:pass
    shutil.rmtree(DL/task['id'],ignore_errors=True);shutil.rmtree(TMP/task['id'],ignore_errors=True)
    with con() as c:c.execute('DELETE FROM tasks WHERE id=? AND owner_type=? AND owner_id=?',(task['id'],'guest',identity['owner_id']))
    return {'ok':True}
@app.get('/api/guest/tasks/{i}/download')
def guest_download(i,identity:dict=Depends(guest_identity)):
    task=guest_task(i,identity)
    if not guest_download_available(task):raise HTTPException(404,'文件不存在')
    path=Path(task['output_path']).resolve()
    return FileResponse(path,filename=path.name)
@app.post('/api/admin/tasks/{i}/cancel',dependencies=[Depends(auth)])
@app.post('/api/tasks/{i}/cancel',dependencies=[Depends(auth)])
def cancel(i):
    t=row(i)
    if not t:raise HTTPException(404,'任务不存在')
    patch(i,status='cancelled',finished=now());p=ACTIVE.get(i)
    if p:
        try:os.killpg(p.pid,signal.SIGTERM)
        except:pass
    return row(i)
@app.post('/api/admin/tasks/{i}/retry',dependencies=[Depends(auth)])
@app.post('/api/tasks/{i}/retry',dependencies=[Depends(auth)])
def retry(i):
    if not row(i):raise HTTPException(404,'任务不存在')
    patch(i,status='queued',progress=0,error_code=None,error_message=None,output_path=None,output_size=None,finished=None,log_tail='');return row(i)
@app.delete('/api/admin/tasks/{i}',dependencies=[Depends(auth)])
@app.delete('/api/tasks/{i}',dependencies=[Depends(auth)])
def delete(i):
    task=row(i)
    if not task:raise HTTPException(404,'任务不存在')
    if task.get('status') in {'queued','downloading','processing'}:raise HTTPException(409,'请先取消任务')
    shutil.rmtree(DL/i,ignore_errors=True);shutil.rmtree(TMP/i,ignore_errors=True)
    with con() as c:c.execute('DELETE FROM tasks WHERE id=?',(i,))
    return {'ok':True}
@app.get('/api/admin/tasks/{i}/download',dependencies=[Depends(auth)])
@app.get('/api/tasks/{i}/download',dependencies=[Depends(auth)])
def download(i):
    t=row(i);p=Path(t['output_path']).resolve() if t and t.get('output_path') else None
    if not p or DL not in p.parents or not p.is_file():raise HTTPException(404,'文件不存在')
    return FileResponse(p,filename=p.name)
@app.post('/api/admin/tasks/{i}/save-to-koofr',dependencies=[Depends(auth)])
@app.post('/api/tasks/{i}/save-to-koofr',dependencies=[Depends(auth)])
def save_koofr(i):
    try:return save_task_to_koofr(i)
    except KoofrError as exc:raise HTTPException(exc.status_code,str(exc))
    finally:_invalidate_koofr_health()
@app.get('/api/admin/tasks/{i}/koofr-status',dependencies=[Depends(auth)])
@app.get('/api/tasks/{i}/koofr-status',dependencies=[Depends(auth)])
def koofr_status(i):
    t=row(i)
    if not t:raise HTTPException(404,'任务不存在')
    return _koofr_payload(t)
@app.get('/api/admin/tasks/{i}/koofr-download',dependencies=[Depends(auth)])
@app.get('/api/tasks/{i}/koofr-download',dependencies=[Depends(auth)])
def koofr_download(i):
    try:
        task=row(i)
        if not task:raise HTTPException(404,'任务不存在')
        try:root,target=_koofr_target(task,force_mount_check=True);files=_koofr_files(target)
        except KoofrError as exc:raise HTTPException(exc.status_code,str(exc))
        except OSError as exc:raise HTTPException(503,f'Koofr 连接中断，请检查挂载后重试：{exc}')
        if not files:raise HTTPException(404,'Koofr 副本不存在')
        if len(files)==1:return FileResponse(files[0][1],filename=files[0][1].name)
        fd,archive=tempfile.mkstemp(prefix=f'koofr-{i}-',suffix='.zip',dir=str(TMP));os.close(fd);archive_path=Path(archive)
        try:
            with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED) as bundle:
                for relative,path in files:
                    if not _inside(target,path.resolve()):raise KoofrError('Koofr 副本包含非法路径')
                    bundle.write(path,arcname=str(relative))
        except KoofrError as exc:
            archive_path.unlink(missing_ok=True);raise HTTPException(exc.status_code,str(exc))
        except OSError as exc:
            archive_path.unlink(missing_ok=True);raise HTTPException(503,f'Koofr 连接中断，请检查挂载后重试：{exc}')
        except Exception:
            archive_path.unlink(missing_ok=True);raise HTTPException(500,'Koofr 副本打包失败')
        return FileResponse(archive_path,filename=_koofr_zip_name(task),background=BackgroundTask(archive_path.unlink,missing_ok=True))
    finally:_invalidate_koofr_health()
@app.get('/api/admin/settings',dependencies=[Depends(auth)])
@app.get('/api/settings',dependencies=[Depends(auth)])
def gs():
    d=settings();p=d.get('proxy_url','');d['proxy_url']=p[:12]+'••••' if p else '';return d
@app.put('/api/admin/settings',dependencies=[Depends(auth)])
@app.put('/api/settings',dependencies=[Depends(auth)])
def ss(d:dict):
    if 'proxy_url' in d and '••••' in str(d['proxy_url']):d.pop('proxy_url')
    return save_settings(d)
@app.get('/api/admin/cookies',dependencies=[Depends(auth)])
@app.get('/api/cookies',dependencies=[Depends(auth)])
def cookies():
    with con() as c:return [dict(x) for x in c.execute('SELECT id,platform,label,name,created FROM cookies ORDER BY created DESC')]
@app.post('/api/admin/cookies',dependencies=[Depends(auth)])
@app.post('/api/cookies',dependencies=[Depends(auth)])
async def upload(platform:str,label:str,file:UploadFile=File(...)):
    b=await file.read(2097153)
    if len(b)>2097152:raise HTTPException(413,'Cookie 文件过大')
    i=uuid.uuid4().hex;p=CK/(i+'.enc');p.write_bytes(F.encrypt(b))
    with con() as c:c.execute('INSERT INTO cookies VALUES(?,?,?,?,?,?)',(i,platform[:100],label[:100],str(p),(file.filename or 'cookies.txt')[:255],now()))
    return {'id':i}
@app.delete('/api/admin/cookies/{i}',dependencies=[Depends(auth)])
@app.delete('/api/cookies/{i}',dependencies=[Depends(auth)])
def dc(i):
    with con() as c:r=c.execute('SELECT path FROM cookies WHERE id=?',(i,)).fetchone();Path(r['path']).unlink(missing_ok=True) if r else None;c.execute('DELETE FROM cookies WHERE id=?',(i,))
    return {'ok':True}
def _html_page(name):
    path=STATIC/name
    if not path.is_file():raise HTTPException(404,'页面不存在')
    return HTMLResponse(path.read_text(encoding='utf-8'))
if STATIC.exists():app.mount('/assets',StaticFiles(directory=STATIC),name='assets')
@app.get('/',response_class=HTMLResponse)
@app.get('/index.html',response_class=HTMLResponse)
def guest_page():return _html_page('index.html')
@app.get('/admin',response_class=HTMLResponse)
@app.get('/admin/',response_class=HTMLResponse)
@app.get('/admin.html',response_class=HTMLResponse)
def admin_page():return _html_page('admin.html')
if __name__=='__main__':
    import uvicorn;uvicorn.run(app,host='0.0.0.0',port=PORT,workers=1)
