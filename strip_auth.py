from pathlib import Path
import re

app_path = Path('/app/app.py')
html_path = Path('/app/static/index.html')

app = app_path.read_text(encoding='utf-8')
app = re.sub(
    r"def auth\(a:str\|None=Header\(None\)\):\n(?:    .*\n){1,6}?def public_url",
    "def auth(a=None):\n    return None\ndef public_url",
    app,
)
app = app.replace(",dependencies=[Depends(auth)]", "")
app = re.sub(r"\nclass Pass\(BaseModel\):password:str=Field\(min_length=1,max_length=256\)", "", app)
app = app.replace(
    "    init();p=os.getenv('ADMIN_PASSWORD')\n    if p and not meta('password'):setmeta('password',hpw(p))\n    STOP.clear();",
    "    init()\n    with con() as c:c.execute(\"DELETE FROM meta WHERE k='password'\")\n    STOP.clear();",
)
app = re.sub(
    r"\n@app\.get\('/api/auth/status'\).*?(?=\n@app\.post\('/api/probe')",
    "\n",
    app,
    flags=re.S,
)
app_path.write_text(app, encoding='utf-8')

html = html_path.read_text(encoding='utf-8')
html = re.sub(r'<div id="auth" class="modal">.*?</div></div>\s*', '', html, count=1, flags=re.S)
html = re.sub(r'<button class="btn ghost" onclick="logout\(\)">退出</button>', '', html)
html = re.sub(
    r"const \$=x=>document\.getElementById\(x\);let token=.*?;",
    "const $=x=>document.getElementById(x);let probe=null,timer=null;",
    html,
    count=1,
)
html = re.sub(
    r"async function api\(p,o=\{\}\)\{.*?return d\}",
    "async function api(p,o={}){if(o.body&&!(o.body instanceof FormData)){o.headers={...(o.headers||{}),'Content-Type':'application/json'};o.body=JSON.stringify(o.body)}const r=await fetch(p,o);let d;try{d=await r.json()}catch{d={detail:await r.text()}}if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:(d.detail?.message||JSON.stringify(d.detail)));return d}",
    html,
    count=1,
    flags=re.S,
)
html = re.sub(
    r"async function init\(\)\{.*?function logout\(\)\{.*?\}",
    "async function init(){await boot()}",
    html,
    count=1,
    flags=re.S,
)
html = html.replace("if(!token)return;", "")
html = html.replace("headers:{Authorization:`Bearer ${token}`}", "")
html = html.replace("init().catch(e=>$('authError').textContent=e.message)", "init().catch(e=>console.error(e))")
html = html.replace(
    "format_has_audio:!!opt.has_audio,audio_format:",
    "format_has_audio:!!opt.has_audio,youtube_strategy:opt.strategy||probe.download_strategy||null,audio_format:",
)
html_path.write_text(html, encoding='utf-8')
