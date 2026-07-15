(() => {
  const tasksById = new Map();

  function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[char]));
  }

  function formatBytes(value) {
    let size = Number(value || 0);
    if (!size) return '未知大小';
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (size < 1024) return `${size.toFixed(1)} ${unit}`;
      size /= 1024;
    }
    return `${size.toFixed(1)} TB`;
  }

  async function getTasks() {
    const response = await fetch('/api/tasks', { cache: 'no-store' });
    if (!response.ok) return [];
    const tasks = await response.json();
    tasksById.clear();
    tasks.forEach(task => tasksById.set(task.id, task));
    return tasks;
  }

  function actionRow(card) {
    const rows = card.querySelectorAll(':scope > .row');
    return rows.length ? rows[rows.length - 1]