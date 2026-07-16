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
            try:
                path = Path(raw_path).resolve()
            except (O