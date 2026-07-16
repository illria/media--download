from pathlib import Path


def install(core):
    @core.app.get('/api/tasks/{task_id}/file-status')
    def file_status(task_id: str):
        task = core.row(task_id)
        if not task:
            return {'exists': False, 'reason': 'not_found'}
        path = task.get('output_path')
        if path and Path(path).exists():
            p = Path(path)
            return {
                'exists': True,
                'name': p.name,
                'size': p.stat().st_size,
                'url': f'/api/tasks/{task_id}/download'
            }
        return {
            'exists': False,
            'reason': 'expired',
            'source_url': task.get('url')
        }
