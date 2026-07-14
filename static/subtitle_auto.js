(() => {
  const subtitleButton = () => [...document.querySelectorAll('.quick button')]
    .find((button) => button.textContent.includes('字幕'));

  const renameButton = () => {
    const button = subtitleButton();
    if (button) button.textContent = '▤ 获取字幕';
  };

  const ensureUi = () => {
    const grid = document.querySelector('#page-settings .settings');
    if (!grid || document.getElementById('siliconflowApiKey')) return;

    const label = document.createElement('label');
    label.innerHTML = '硅基流动 API Key<input id="siliconflowApiKey" type="password" autocomplete="off" placeholder="留空保持现有 Key">';
    grid.appendChild(label);

    const panel = document.createElement('div');
    panel.style.gridColumn = '1 / -1';
    panel.innerHTML = `
      <div class="row" style="margin-top:2px">
        <span id="siliconflowStatus" class="muted">正在检查 AI 字幕配置</span>
        <button id="clearSiliconflowKey" type="button" class="btn danger">清除 API Key</button>
      </div>
      <p class="muted" style="margin:8px 0 0">获取字幕会优先使用平台字幕；没有字幕时才调用 FunAudioLLM/SenseVoiceSmall。AI 字幕时间轴为分段估算。</p>`;
    grid.appendChild(panel);

    document.getElementById('clearSiliconflowKey').onclick = async () => {
      if (!confirm('确定清除硅基流动 API Key？')) return;
      const response = await fetch('/api/transcription/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({clear: true}),
      });
      if (!response.ok) return alert('清除失败');
      document.getElementById('siliconflowApiKey').value = '';
      await loadState();
    };
  };

  async function loadState() {
    ensureUi();
    const status = document.getElementById('siliconflowStatus');
    if (!status) return;
    try {
      const response = await fetch('/api/transcription/settings', {cache: 'no-store'});
      if (!response.ok) throw new Error('读取失败');
      const data = await response.json();
      status.textContent = data.configured
        ? `AI 字幕已配置 · ${data.model}`
        : 'AI 字幕未配置：只能下载平台已有字幕';
      status.className = data.configured ? 'good' : 'muted';
    } catch {
      status.textContent = '无法读取 AI 字幕配置';
      status.className = 'error';
    }
  }

  const wireSave = () => {
    const button = document.getElementById('saveSettings');
    if (!button || button.dataset.siliconflowWired) return;
    button.dataset.siliconflowWired = '1';
    const original = button.onclick;
    button.onclick = async (event) => {
      if (original) await original.call(button, event);
      const input = document.getElementById('siliconflowApiKey');
      const apiKey = input?.value.trim() || '';
      if (apiKey) {
        const response = await fetch('/api/transcription/settings', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({api_key: apiKey}),
        });
        if (!response.ok) return alert('硅基流动 API Key 保存失败');
        input.value = '';
      }
      await loadState();
    };
  };

  renameButton();
  ensureUi();
  wireSave();
  document.querySelector('[data-page="settings"]')?.addEventListener('click', () => {
    setTimeout(() => {
      ensureUi();
      wireSave();
      loadState();
    }, 0);
  });
  loadState();
})();