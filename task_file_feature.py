from pathlib import Path


def install(core):
    @core.app.get(
        '/api/tasks/{task_id}/file-status',
        dependencies=[core.Depends(core.auth)],
    )
    def file_status(task_id: str):
        task = core.row(task_id)
        if not task:
            raise core.HTTPException(404, '任务不存在')

        source_url = task.get('url') or ''
        raw_path = task.get('output_path')

        if raw_path:
            path = Path(raw_path).resolve()
            if path.exists() and path.is_file():
                return {
                    'exists': True,
                    'name': path.name,
                    'size': path.stat().st_size,
                    'url': f'/api/tasks/{task_id}/download',
                }

        return {
            'exists': False,
            'reason': 'expired',
            'message': '文件已自动清除',
            'source_url': source_url,
        }
