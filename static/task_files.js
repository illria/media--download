(() => {
  window.renderTaskFileStatus = async function(taskId) {
    const box = document.getElementById('file-' + taskId);
    if (!box) return;
    try {
      const r = await fetch('/api/tasks/' + taskId + '/file-status');
      const d = await r.json();
      if (d.exists) {
        box.innerHTML = `<div>文件：${d.name}</div><div>${d.size} bytes</div><button onclick="downloadFile('${taskId}')">下载文件</button>`;
      } else if (d.reason === 'expired') {
        box.innerHTML = `<div>文件已自动清除</div><div>原解析链接：</div><a href="${d.source_url}" target="_blank">${d.source_url}</a>`;
      }
    } catch(e) {}
  };
})();
