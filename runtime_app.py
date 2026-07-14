from __future__ import annotations

import os

import app as core
import youtube_execute
import youtube_reliability as yr
from starlette.responses import Response

_original_error = core.err
core.base = lambda url, cookie=None, compat=False: yr.compatibility_base(core, url, cookie, compat)
core.err = lambda text, url='': yr.friendly_error(_original_error, text, url)
core.probe_sync = lambda url, cookie_id: yr.probe(core, url, cookie_id)
core.execute = lambda task_id: youtube_execute.execute(core, _original_error, task_id)
core.youtube_strategies = lambda url, preferred=None: yr.strategies(core, url, preferred)
core.pot_available = yr.pot_available

@core.app.middleware('http')
async def inject_auto_download(request, call_next):
    response = await call_next(request)
    if request.url.path not in {'/', '/index.html'} or 'text/html' not in response.headers.get('content-type', ''):
        return response
    body = b''
    async for chunk in response.body_iterator:
        body += chunk
    html = body.decode('utf-8', errors='replace')
    if '/assets/auto_download.js' not in html:
        html = html.replace('</body>', '<script src="/assets/auto_download.js"></script></body>')
    headers = dict(response.headers); headers.pop('content-length', None)
    return Response(content=html, status_code=response.status_code, headers=headers, media_type='text/html')

app = core.app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('APP_PORT', '19190')), workers=1)
