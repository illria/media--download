from __future__ import annotations

import os

import app as core
import youtube_execute
import youtube_reliability as yr

_original_error = core.err
core.base = lambda url, cookie=None, compat=False: yr.compatibility_base(core, url, cookie, compat)
core.err = lambda text, url='': yr.friendly_error(_original_error, text, url)
core.probe_sync = lambda url, cookie_id: yr.probe(core, url, cookie_id)
core.guest_probe_sync = lambda url, policy: yr.guest_probe(core, url, (policy or {}).get('request_sleep_seconds'))
core.execute = lambda task_id: youtube_execute.execute(core, _original_error, task_id)
core.youtube_strategies = lambda url, preferred=None: yr.strategies(core, url, preferred)
core.pot_available = yr.pot_available

app = core.app

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('APP_PORT', '19190')), workers=1)
