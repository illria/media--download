from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

import youtube_reliability as yr

def build_command(core: Any, task: dict[str, Any], cookie: Path | None, strategy: yr.Strategy) -> list[str]:
    options=task['options']; work=core.TMP/task['id']; mode=options.get('mode','video')
    if mode=='live':
        command=['streamlink','--force','--output',str(work/'live.ts')]
        proxy=str(core.settings().get('proxy_url') or '').strip()
        if proxy: command += ['--http-proxy',proxy]
        return command+[task['url'],options.get('stream_quality','best')]
    command=yr.base(core,task['url'],cookie,strategy)+['--newline','--restrict-filenames','--paths',f'temp:{work}','--output',str(work/'%(title).180B [%(id)s].%(ext)s'),'--max-filesize',f"{core.settings()['max_file_size_gb']}G",'--progress-template','download:PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s']
    format_id=str(options.get('format_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',format_id): format_id=''
    same=not options.get('youtube_strategy') or options.get('youtube_strategy')==strategy.key
    if mode=='thumbnail': command += ['--skip-download','--write-thumbnail','--convert-thumbnails','jpg']
    elif mode=='audio':
        command += ['--format',format_id if format_id and same else 'bestaudio/best']
        audio_format=options.get('audio_format','original')
        if audio_format in {'mp3','m4a','opus','wav','flac'}: command += ['--extract-audio','--audio-format',audio_format]
    elif mode=='subtitles': command += ['--skip-download','--write-subs','--write-auto-subs','--sub-langs',','.join(options.get('subtitle_languages') or ['zh-CN','zh','en']),'--convert-subs','srt']
    else:
        height=min(int(options.get('resolution',1080)),int(core.settings()['max_resolution']))
        selector=(format_id if options.get('format_has_audio') else f'{format_id}+bestaudio/best') if format_id and same else f'bv*[height<={height}]+ba/b[height<={height}]/best[height<={height}]'
        command += ['--format',selector,'--merge-output-format','mp4']
    if mode not in {'thumbnail','subtitles'} and options.get('write_thumbnail'): command += ['--write-thumbnail']
    if mode not in {'thumbnail','subtitles'} and options.get('embed_metadata',True): command += ['--embed-metadata']
    return command+[task['url']]

def clean(work: Path) -> None:
    for path in work.rglob('*'):
        if path.is_file() and path.suffix in {'.part','.ytdl'}: path.unlink(missing_ok=True)

def execute(core: Any, original_error: Any, task_id: str) -> None:
    task=core.row(task_id)
    if not task: return
    task['url']=yr.canonical_url(core,task['url']); cookie=None; logs=[]
    try:
        if shutil.disk_usage(core.ROOT).free < int(core.settings()['min_free_gb'])*1024**3: raise RuntimeError('no space')
        cookie=core.cpath(task['options'].get('cookie_id')); work=core.TMP/task_id; work.mkdir(parents=True,exist_ok=True)
        core.patch(task_id,status='downloading',progress=0,error_code=None,error_message=None,log_tail='')
        items=yr.strategies(core,task['url'],str(task['options'].get('youtube_strategy') or '')); rc=1
        for index,strategy in enumerate(items):
            if index: clean(work)
            logs.append(f'[Media Hub] 路径 {index+1}/{len(items)}：{strategy.label}')
            process=subprocess.Popen(build_command(core,task,cookie,strategy),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
            with core.LOCK: core.ACTIVE[task_id]=process
            attempt=[]; assert process.stdout is not None
            for line in process.stdout:
                s=line.strip()
                if s: attempt=(attempt+[s])[-100:]; logs=(logs+[s])[-180:]
                m=re.search(r'PROGRESS:\s*([0-9.]+)%\|([^|]*)\|([^|]*)',s)
                if m: core.patch(task_id,status='downloading',progress=min(float(m.group(1)),98.0),speed=m.group(2),eta=m.group(3),log_tail='\n'.join(logs))
                if any(x in s for x in ('[Merger]','[VideoRemuxer]','[VideoConvertor]','[ExtractAudio]','[Metadata]','[ThumbnailsConvertor]')): core.patch(task_id,status='processing',progress=99,speed='',eta='正在合并/处理',log_tail='\n'.join(logs))
                current=core.row(task_id)
                if current and current['status']=='cancelled': os.killpg(process.pid,signal.SIGTERM); break
            rc=process.wait(); current=core.row(task_id)
            if current and current['status']=='cancelled': shutil.rmtree(work,ignore_errors=True); return
            if rc==0: break
            if not yr.retriable('\n'.join(attempt)): break
        if rc:
            code,message=yr.friendly_error(original_error,'ALL_YOUTUBE_STRATEGIES_FAILED\n'+'\n'.join(logs),task['url']); raise RuntimeError(json.dumps({'code':code,'message':message},ensure_ascii=False))
        core.patch(task_id,status='processing',progress=99,speed='',eta='正在整理文件',log_tail='\n'.join(logs))
        path,size=core.move(task_id); core.patch(task_id,status='completed',progress=100,speed='',eta='',output_path=path,output_size=size,finished=core.now(),log_tail='\n'.join(logs))
    except Exception as exc:
        try: parsed=json.loads(str(exc)); code,message=parsed['code'],parsed['message']
        except Exception: code,message=yr.friendly_error(original_error,str(exc),task['url'])
        core.patch(task_id,status='failed',error_code=code,error_message=message,finished=core.now(),log_tail='\n'.join(logs))
    finally:
        if cookie: cookie.unlink(missing_ok=True)
        with core.LOCK: core.ACTIVE.pop(task_id,None)
