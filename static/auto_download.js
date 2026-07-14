(() => {
  const trackedKey = 'mediaHubTrackedTasks';
  const deliveredKey = 'mediaHubDeliveredTasks';
  const tracked = new Set(JSON.parse(sessionStorage.getItem(trackedKey) || '[]'));
  const delivered = new Set(JSON.parse(sessionStorage.getItem(deliveredKey) || '[]'));

  const save = () => {
    sessionStorage.setItem(trackedKey, JSON.stringify([...tracked]));
    sessionStorage.setItem(deliveredKey, JSON.stringify([...delivered]));
  };

  const originalFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    try {
      const input = args[0];
      const options = args[1] || {};
      const url = typeof input === 'string' ? input : input.url;
      if (url === '/api/tasks' && String(options.method || 'GET').toUpperCase() === 'POST' && response.ok) {
        const clone = response.clone();
        const task = await clone.json();
        if (task && task.id) {
          tracked.add(task.id);
          save();
        }
      }
    } catch (error) {
      console.debug('Task tracking skipped', error);
    }
    return response;
  };

  function triggerDownload(taskId) {
    const anchor = document.createElement('a');
    anchor.href = `/api/tasks/${encodeURIComponent(taskId)}/download`;
    anchor.download = '';
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  async function poll() {
    if (!tracked.size) return;
    try {
      const response = await originalFetch('/api/tasks', { cache: 'no-store' });
      if (!response.ok) return;
      const tasks = await response.json();
      for (const task of tasks) {
        if (tracked.has(task.id) && task.status === 'completed' && !delivered.has(task.id)) {
          delivered.add(task.id);
          save();
          triggerDownload(task.id);
        }
      }
    } catch (error) {
      console.debug('Automatic download polling failed', error);
    }
  }

  setInterval(poll, 1500);
  window.addEventListener('focus', poll);
})();
