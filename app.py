from __future__ import annotations
import asyncio, hashlib, hmac, ipaddress, json, os, re, secrets, shutil, signal, socket, sqlite3, subprocess, threading, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

PORT=int(os.getenv('APP_PORT','19190')); ROOT=Path(os.getenv('DATA_ROOT','/data')).resolve(); DB=ROOT/'database/media-hub.db'; DL=ROOT/'downloads'; TMP=ROOT/'temp'; CK=ROOT/'cookies'; STATIC=Path(__file__).parent/'static'
for p in (DB.parent,DL,TMP,CK): p.mkdir(parents=True,exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat()
def secret(path,make):
    if path.exists() and path.read_text().strip(): return path.read_text().strip()
    v=make(); path.write_text(v); os.chmod(path,0o600); return v
SECRET=os.getenv('SECRET_KEY') or secret(DB.parent/'secret.key',lambda:secrets.token_urlsafe(48)); FKEY=os.getenv('COOKIE_ENCRYPTION_KEY') or secret(DB.parent/'cookie.key',lambda:Fernet.generate_key().decode()); F=Fernet(FKEY.encode()); TOK=URLSafeTimedSerializer(SECRET,salt='media-hub')
DEFAULT={'max_file_size_gb':5,'retention_hours':24,'min_free_gb':5,'max_resolution':1080,'proxy_url':'','youtube_compatibility_mode':True,'request_sleep_seconds':1}
ACTIVE={}; LOCK=threading.RLock(); STOP=threading.Event()
def con():
    c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
    with con() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);CREATE TABLE IF NOT EXISTS cookies(id TEXT PRIMARY KEY,platform TEXT,label TEXT,path TEXT,name TEXT,created TEXT);CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,url TEXT,title TEXT,platform TEXT,status TEXT,options TEXT,progress REAL DEFAULT 0,speed TEXT,eta TEXT,output_path TEXT,output_size INTEGER,error_code TEXT,error_message TEXT,log_tail TEXT DEFAULT '',created TEXT,updated TEXT,finished TEXT);''')
        for k,v in DEFAULT.items(): c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        c.execute("UPDATE tasks SET status='queued',error_code=NULL,error_message=NULL WHERE status IN ('downloading','processing')")
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
def hpw(p):
    s=os.urandom(16); return s.hex()+':'+hashlib.scrypt(p.encode(),salt=s,n=2**14,r=8,p=1,dklen=32).hex()
def vpw(p,e):
    try:s,d=e.split(':');return hmac.compare_digest(hashlib.scrypt(p.encode(),salt=bytes.fromhex(s),n=2**14,r=8,p=1,dklen=32).hex(),d)
    except:return False
def auth(a:str|None=Header(None)):
    if not a or not a.startswith('Bearer '):raise HTTPException(401,'需要登录')
    try:
        if TOK.loads(a[7:],max_age=604800).get('sub')!='admin':raise HTTPException(401,'登录失效')
    except (BadSignature,SignatureExpired):raise HTTPException(401,'登录失效')
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
def row(i):
    with con() as c:r=c.execute('SELECT * FROM tasks WHERE id=?',(i,)).fetchone()
    if not r:return None
    d=dict(r);d['options']=json.loads(d.pop('options'));return d
def patch(i,**v):
    if not v:return
    v['updated']=now(); q=','.join(k+'=?' for k in v)
    with con() as c:c.execute('UPDATE tasks SET '+q+' WHERE id=?',(*v.values(),i))
def cmd(t,cp,compat=False):
    o=t['options']; work=TMP/t['id']; out=str(work/'%(title).180B [%(id)s].%(ext)s'); mode=o.get('mode','video')
    if mode=='live':
        a=['streamlink','--force','--output',str(work/'live.ts')];
        if settings().get('proxy_url'):a+=['--http-proxy',settings()['proxy_url']]
        return a+[t['url'],o.get('stream_quality','best')]
    a=base(t['url'],cp,compat)+['--newline','--restrict-filenames','--paths',f'temp:{work}','--output',out,'--max-filesize',f"{settings()['max_file_size_gb']}G",'--progress-template','download:PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s']
    fid=str(o.get('format_id') or '') if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(o.get('format_id') or '')) else ''
    if mode=='thumbnail':a+=['--skip-download','--write-thumbnail','--convert-thumbnails','jpg']
    elif mode=='audio':
        a+=['--format',fid or 'bestaudio/best'];fmt=o.get('audio_format','original');a+=(['--extract-audio','--audio-format',fmt] if fmt in ('mp3','m4a','opus','wav','flac') else [])
    elif mode=='subtitles':a+=['--skip-download','--write-subs','--write-auto-subs','--sub-langs',','.join(o.get('subtitle_languages') or ['zh-CN','zh','en']),'--convert-subs','srt']
    else:
        if fid:a+=['--format',fid if o.get('format_has_audio') else fid+'+bestaudio/best']
        else:a+=['--format',f"bv*[height<={min(int(o.get('resolution',1080)),int(settings()['max_resolution']))}]+ba/b"]
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
    try:
        if shutil.disk_usage(ROOT).free<int(settings()['min_free_gb'])*1024**3:raise RuntimeError('no space')
        cp=cpath(t['options'].get('cookie_id'));(TMP/i).mkdir(parents=True,exist_ok=True);patch(i,status='downloading',progress=0,error_code=None,error_message=None,log_tail='')
        rc=1
        for n,compat in enumerate([False,True] if yt(t['url']) else [False]):
            if n:logs.append('[Media Hub] 首次请求被拒绝，正在切换 YouTube 兼容客户端重试……')
            p=subprocess.Popen(cmd(t,cp,compat),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
            with LOCK:ACTIVE[i]=p
            for line in p.stdout:
                s=line.strip();logs=(logs+[s])[-120:] if s else logs;m=re.search(r'PROGRESS:\s*([0-9.]+)%\|([^|]*)\|([^|]*)',s)
                if m:patch(i,progress=float(m.group(1)),speed=m.group(2),eta=m.group(3),log_tail='\n'.join(logs))
                if row(i)['status']=='cancelled':os.killpg(p.pid,signal.SIGTERM);break
            rc=p.wait()
            if row(i)['status']=='cancelled':shutil.rmtree(TMP/i,ignore_errors=True);return
            if rc==0 or not denied('\n'.join(logs)):break
        if rc:code,msg=err('\n'.join(logs),t['url']);raise RuntimeError(json.dumps({'code':code,'message':msg},ensure_ascii=False))
        path,size=move(i);patch(i,status='completed',progress=100,output_path=path,output_size=size,finished=now(),log_tail='\n'.join(logs))
    except Exception as e:
        try:x=json.loads(str(e));code,msg=x['code'],x['message']
        except:code,msg=err(str(e),t['url'])
        patch(i,status='failed',error_code=code,error_message=msg,finished=now(),log_tail='\n'.join(logs))
    finally:
        if cp:cp.unlink(missing_ok=True)
        with LOCK:ACTIVE.pop(i,None)
def worker():
    while not STOP.is_set():
        if not ACTIVE:
            with con() as c:r=c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if r:execute(r['id']);continue
        STOP.wait(1)
def cleanup():
    while not STOP.wait(900):
        cut=datetime.now(timezone.utc)-timedelta(hours=int(settings()['retention_hours']))
        with con() as c:
            for r in c.execute("SELECT id,finished FROM tasks WHERE status='completed'"):
                if r['finished'] and datetime.fromisoformat(r['finished'])<cut:shutil.rmtree(DL/r['id'],ignore_errors=True);c.execute("UPDATE tasks SET status='expired',output_path=NULL WHERE id=?",(r['id'],))
class Pass(BaseModel):password:str=Field(min_length=1,max_length=256)
class Probe(BaseModel):url:str=Field(min_length=8,max_length=2048);cookie_id:str|None=None
class Task(BaseModel):url:str;title:str|None=None;platform:str|None=None;options:dict[str,Any]={}
@asynccontextmanager
async def life(_):
    init();p=os.getenv('ADMIN_PASSWORD')
    if p and not meta('password'):setmeta('password',hpw(p))
    STOP.clear();threading.Thread(target=worker,daemon=True).start();threading.Thread(target=cleanup,daemon=True).start();yield;STOP.set()
app=FastAPI(lifespan=life)
@app.get('/api/health')
def health():
    u=shutil.disk_usage(ROOT);tools={n:bool(shutil.which(n)) for n in ('yt-dlp','streamlink','ffmpeg','deno')};return {'ok':all(tools.values()),'port':PORT,'tools':tools,'disk':{'total':u.total,'used':u.used,'free':u.free,'percent':round(u.used/u.total*100,1)}}
@app.get('/api/auth/status')
def ast():return {'setup_required':not bool(meta('password'))}
@app.post('/api/auth/setup')
def setup(b:Pass):
    if meta('password'):raise HTTPException(409,'密码已设置')
    if len(b.password)<8:raise HTTPException(400,'密码至少 8 位')
    setmeta('password',hpw(b.password));return {'token':TOK.dumps({'sub':'admin'})}
@app.post('/api/auth/login')
def login(b:Pass):
    if not vpw(b.password,meta('password') or ''):raise HTTPException(401,'密码错误')
    return {'token':TOK.dumps({'sub':'admin'})}
@app.post('/api/probe',dependencies=[Depends(auth)])
async def probe(b:Probe):
    u=await validate(b.url)
    try:return await asyncio.to_thread(probe_sync,u,b.cookie_id)
    except Exception as e:code,msg=err(str(e),u);raise HTTPException(400,detail={'code':code,'message':msg})
@app.post('/api/tasks',dependencies=[Depends(auth)])
async def create(b:Task):
    u=await validate(b.url);i=uuid.uuid4().hex;n=now()
    with con() as c:c.execute('INSERT INTO tasks(id,url,title,platform,status,options,created,updated) VALUES(?,?,?,?,?,?,?,?)',(i,u,b.title,b.platform,'queued',json.dumps(b.options),n,n))
    return row(i)
@app.get('/api/tasks',dependencies=[Depends(auth)])
def tasks():
    with con() as c:ids=[x['id'] for x in c.execute('SELECT id FROM tasks ORDER BY created DESC LIMIT 200')]
    return [row(i) for i in ids]
@app.post('/api/tasks/{i}/cancel',dependencies=[Depends(auth)])
def cancel(i):
    t=row(i)
    if not t:raise HTTPException(404,'任务不存在')
    patch(i,status='cancelled',finished=now());p=ACTIVE.get(i)
    if p:
        try:os.killpg(p.pid,signal.SIGTERM)
        except:pass
    return row(i)
@app.post('/api/tasks/{i}/retry',dependencies=[Depends(auth)])
def retry(i):patch(i,status='queued',progress=0,error_code=None,error_message=None,output_path=None,output_size=None,finished=None,log_tail='');return row(i)
@app.delete('/api/tasks/{i}',dependencies=[Depends(auth)])
def delete(i):
    shutil.rmtree(DL/i,ignore_errors=True);shutil.rmtree(TMP/i,ignore_errors=True)
    with con() as c:c.execute('DELETE FROM tasks WHERE id=?',(i,))
    return {'ok':True}
@app.get('/api/tasks/{i}/download',dependencies=[Depends(auth)])
def download(i):
    t=row(i);p=Path(t['output_path']).resolve() if t and t.get('output_path') else None
    if not p or DL not in p.parents or not p.is_file():raise HTTPException(404,'文件不存在')
    return FileResponse(p,filename=p.name)
@app.get('/api/settings',dependencies=[Depends(auth)])
def gs():
    d=settings();p=d.get('proxy_url','');d['proxy_url']=p[:12]+'••••' if p else '';return d
@app.put('/api/settings',dependencies=[Depends(auth)])
def ss(d:dict):
    if 'proxy_url' in d and '••••' in str(d['proxy_url']):d.pop('proxy_url')
    return save_settings(d)
@app.get('/api/cookies',dependencies=[Depends(auth)])
def cookies():
    with con() as c:return [dict(x) for x in c.execute('SELECT id,platform,label,name,created FROM cookies ORDER BY created DESC')]
@app.post('/api/cookies',dependencies=[Depends(auth)])
async def upload(platform:str,label:str,file:UploadFile=File(...)):
    b=await file.read(2097153)
    if len(b)>2097152:raise HTTPException(413,'Cookie 文件过大')
    i=uuid.uuid4().hex;p=CK/(i+'.enc');p.write_bytes(F.encrypt(b))
    with con() as c:c.execute('INSERT INTO cookies VALUES(?,?,?,?,?,?)',(i,platform[:100],label[:100],str(p),(file.filename or 'cookies.txt')[:255],now()))
    return {'id':i}
@app.delete('/api/cookies/{i}',dependencies=[Depends(auth)])
def dc(i):
    with con() as c:r=c.execute('SELECT path FROM cookies WHERE id=?',(i,)).fetchone();Path(r['path']).unlink(missing_ok=True) if r else None;c.execute('DELETE FROM cookies WHERE id=?',(i,))
    return {'ok':True}
if STATIC.exists():app.mount('/assets',StaticFiles(directory=STATIC),name='assets')
@app.get('/{path:path}',response_class=HTMLResponse)
def front(path=''):return HTMLResponse((STATIC/'index.html').read_text() if (STATIC/'index.html').exists() else '<h1>Frontend missing</h1>')
if __name__=='__main__':
    import uvicorn;uvicorn.run(app,host='0.0.0.0',port=PORT,workers=1)
