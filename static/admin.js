(() => {
  const TOKEN_KEY = 'mediaToken';
  const KOOFR_TTL_MS = 45000;
  const PAGE_TITLES = {
    dashboard: '仪表盘',
    probe: '解析下载',
    tasks: '全部任务',
    cookies: 'Cookie',
    koofr: 'Koofr',
    settings: '系统设置',
    'guest-policy': '游客策略',
    security: '安全设置',
  };
  const STATUS_LABEL = {
    queued: '排队中',
    downloading: '下载中',
    processing: '处理中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    expired: '已过期',
  };

  const state = {
    token: localStorage.getItem(TOKEN_KEY) || '',
    page: 'dashboard',
    probe: null,
    selectedVideo: null,
    settingsProxyMasked: '',
    clearProxy: false,
    taskTimer: null,
    healthTimer: null,
    taskInFlight: false,
    healthInFlight: false,
    koofrCache: new Map(),
    koofrInFlight: new Set(),
    cookies: [],
    tasks: [],
  };

  const loginView = document.getElementById('loginView');
  const appView = document.getElementById('appView');
  const loginForm = document.getElementById('loginForm');
  const loginError = document.getElementById('loginError');
  const adminPassword = document.getElementById('adminPassword');
  const pageTitle = document.getElementById('pageTitle');
  const adminHealthText = document.getElementById('adminHealthText');
  const adminHealth = document.getElementById('adminHealth');

  function text(value) {
    return value == null ? '' : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>"']/g, (char) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[char]));
  }

  function formatSize(bytes) {
    let n = Number(bytes || 0);
    if (!n && n !== 0) return '-';
    if (!n) return '0 B';
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (n < 1024) return `${n.toFixed(n >= 10 || unit === 'B' ? 0 : 1)} ${unit}`;
      n /= 1024;
    }
    return `${n.toFixed(1)} TB`;
  }

  function formatTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString();
  }

  function fileName(path) {
    return text(path).split(/[\\/]/).pop() || '下载文件';
  }

  function clearSession(message) {
    state.token = '';
    localStorage.removeItem(TOKEN_KEY);
    stopPolling();
    appView.classList.add('hidden');
    loginView.classList.remove('hidden');
    if (message) loginError.textContent = message;
  }

  function setSession(token) {
    state.token = token;
    localStorage.setItem(TOKEN_KEY, token);
    loginView.classList.add('hidden');
    appView.classList.remove('hidden');
    loginError.textContent = '';
    adminPassword.value = '';
  }

  function detailMessage(data, fallback = '请求失败') {
    if (!data) return fallback;
    if (typeof data === 'string') return data;
    if (data.message) return data.message;
    if (data.detail != null) return detailMessage(data.detail, fallback);
    return fallback;
  }

  async function adminApi(path, options = {}) {
    if (!state.token) throw new Error('需要登录');
    const opts = {
      cache: 'no-store',
      ...options,
      headers: {
        ...(options.headers || {}),
        Authorization: `Bearer ${state.token}`,
      },
    };
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    let data = null;
    const raw = await response.text();
    try { data = raw ? JSON.parse(raw) : null; } catch { data = { detail: raw }; }
    if (response.status === 401) {
      clearSession('登录已失效，请重新登录');
      throw new Error('登录已失效');
    }
    if (!response.ok) throw new Error(detailMessage(data));
    return data;
  }

  function setHealthPill(ok, message, level = 'ok') {
    adminHealthText.textContent = message;
    const dot = adminHealth.querySelector('.dot');
    dot.className = `dot ${ok ? level : 'bad'}`;
  }

  function stopPolling() {
    if (state.taskTimer) clearInterval(state.taskTimer);
    if (state.healthTimer) clearInterval(state.healthTimer);
    state.taskTimer = null;
    state.healthTimer = null;
  }

  function schedulePolling() {
    stopPolling();
    if (!state.token) return;
    const visible = document.visibilityState === 'visible';
    const active = state.tasks.some((task) => ['queued', 'downloading', 'processing'].includes(task.status));
    const taskMs = !visible ? 60000 : (active ? 5000 : 15000);
    const healthMs = !visible ? 120000 : 30000;
    state.taskTimer = setInterval(() => {
      if (state.page === 'tasks' || active) loadTasks(false);
    }, taskMs);
    state.healthTimer = setInterval(() => { loadHealth(false); }, healthMs);
  }

  async function loadHealth(updateDash = true) {
    if (state.healthInFlight) return null;
    state.healthInFlight = true;
    try {
      const health = await adminApi('/api/admin/health');
      const koofr = health.koofr || {};
      let koofrLabel = 'Koofr 未挂载';
      let level = 'warn';
      if (koofr.mounted && koofr.writable) {
        koofrLabel = 'Koofr 已挂载且可写';
        level = 'ok';
      } else if (koofr.mounted) {
        koofrLabel = 'Koofr 已挂载但不可写';
        level = 'warn';
      }
      setHealthPill(!!health.ok, health.ok ? `系统正常 · ${koofrLabel}` : '组件缺失', health.ok ? level : 'bad');
      if (updateDash) renderDashboard(health);
      return health;
    } catch (error) {
      setHealthPill(false, error.message || '连接失败', 'bad');
      return null;
    } finally {
      state.healthInFlight = false;
    }
  }

  function renderDashboard(health) {
    const tools = health.tools || {};
    const disk = health.disk || {};
    const koofr = health.koofr || {};
    const stats = [
      ['系统状态', health.ok ? '正常' : '异常'],
      ['磁盘使用率', `${disk.percent != null ? disk.percent : '-'}%`],
      ['磁盘剩余', formatSize(disk.free)],
      ['yt-dlp', tools['yt-dlp'] ? '可用' : '缺失'],
      ['ffmpeg', tools.ffmpeg ? '可用' : '缺失'],
      ['Deno', tools.deno ? '可用' : '缺失'],
      ['streamlink', tools.streamlink ? '可用' : '缺失'],
      ['Koofr 挂载', koofr.mounted ? '已挂载' : '未挂载'],
      ['Koofr 可写', koofr.writable ? '可写' : '不可写'],
    ];
    document.getElementById('dashStats').innerHTML = stats.map(([label, value]) => `
      <div class="stat"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div></div>
    `).join('');
    let koofrState = '未挂载';
    if (koofr.mounted && koofr.writable) koofrState = '已挂载且可写';
    else if (koofr.mounted) koofrState = '已挂载但不可写';
    document.getElementById('dashKoofr').innerHTML = `
      <strong>Koofr</strong>
      <div class="meta-list" style="margin-top:8px">
        <div>状态：<strong>${escapeHtml(koofrState)}</strong></div>
        <div>剩余空间：<strong>${escapeHtml(formatSize(koofr.free))}</strong></div>
        ${koofr.error ? `<div class="error">${escapeHtml(koofr.error)}</div>` : ''}
      </div>
    `;
    document.getElementById('koofrPanel').innerHTML = document.getElementById('dashKoofr').innerHTML;
  }

  async function loadCookies() {
    state.cookies = await adminApi('/api/admin/cookies') || [];
    const select = document.getElementById('probeCookie');
    const current = select.value;
    select.innerHTML = '<option value="">不使用 Cookie</option>' + state.cookies.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.platform || '-')} · ${escapeHtml(item.label || item.name || item.id)}</option>`
    )).join('');
    if (current) select.value = current;
    document.getElementById('cookieList').innerHTML = state.cookies.map((item) => `
      <div class="task">
        <div class="task-top">
          <strong>${escapeHtml(item.platform || '-')}</strong>
          <span class="muted">${escapeHtml(formatTime(item.created))}</span>
        </div>
        <div class="row muted">
          <span>${escapeHtml(item.label || '-')}</span>
          <span>${escapeHtml(item.name || '-')}</span>
        </div>
        <div class="task-actions">
          <button type="button" class="btn btn-danger btn-quiet" data-cookie-delete="${escapeHtml(item.id)}">删除</button>
        </div>
      </div>
    `).join('') || '<p class="muted">暂无 Cookie</p>';
  }

  function chooseDefaultVideo(options) {
    const list = (options || []).slice().sort((a, b) => Number(b.height || 0) - Number(a.height || 0));
    return list.find((item) => Number(item.height) === 1080) || list[0] || null;
  }

  function renderAdminFormats() {
    const box = document.getElementById('adminFormats');
    const videos = state.probe.video_options || [];
    box.innerHTML = videos.map((item) => {
      const selected = state.selectedVideo && String(state.selectedVideo.format_id) === String(item.format_id);
      return `<button type="button" class="format-btn${selected ? ' active' : ''}" data-admin-format="${escapeHtml(item.format_id)}">
        ${escapeHtml(item.label || `${item.height}p`)}
        <small>${escapeHtml(item.ext || '')}${item.has_audio ? ' · 含音频' : ''}${item.filesize ? ` · ${escapeHtml(formatSize(item.filesize))}` : ''}</small>
      </button>`;
    }).join('') || '<p class="muted">没有可用视频格式</p>';
  }

  async function probeAdmin(event) {
    event.preventDefault();
    const url = document.getElementById('adminUrl').value.trim();
    const cookieId = document.getElementById('probeCookie').value || null;
    const status = document.getElementById('adminProbeStatus');
    const button = document.getElementById('adminProbeBtn');
    if (!url) {
      status.textContent = '请输入媒体链接';
      status.className = 'live-region error';
      return;
    }
    button.disabled = true;
    status.textContent = '正在解析…';
    status.className = 'live-region muted';
    try {
      const result = await adminApi('/api/admin/probe', {
        method: 'POST',
        body: { url, cookie_id: cookieId },
      });
      state.probe = result;
      state.selectedVideo = chooseDefaultVideo(result.video_options || []);
      document.getElementById('adminResult').classList.remove('hidden');
      document.getElementById('adminResultTitle').textContent = result.title || '未命名媒体';
      document.getElementById('adminResultMeta').textContent = `平台：${result.platform || '-'} · 作者：${result.uploader || '-'} · 时长：${result.duration ? Math.round(result.duration / 60) + ' 分钟' : '未知'} · 策略：${result.download_strategy_label || result.download_strategy || '-'}`;
      renderAdminFormats();
      status.textContent = '解析成功';
      status.className = 'live-region good';
    } catch (error) {
      status.textContent = error.message || '解析失败';
      status.className = 'live-region error';
    } finally {
      button.disabled = false;
    }
  }

  async function createAdminTask(mode) {
    if (!state.probe) return;
    const status = document.getElementById('adminCreateStatus');
    const selected = state.selectedVideo;
    const cookieId = document.getElementById('probeCookie').value || null;
    const options = {
      mode,
      write_thumbnail: false,
      embed_metadata: true,
      cookie_id: cookieId,
      youtube_strategy: state.probe.download_strategy || null,
    };
    if (mode === 'video') {
      options.engine = 'auto';
      options.resolution = Number((selected && selected.height) || 1080);
      options.format_id = selected && selected.format_id ? String(selected.format_id) : null;
      options.format_has_audio = !!(selected && selected.has_audio);
      options.audio_format = 'original';
    } else if (mode === 'audio') {
      options.engine = 'auto';
      options.format_id = null;
      options.format_has_audio = false;
      options.audio_format = 'mp3';
    } else if (mode === 'thumbnail') {
      options.engine = 'auto';
      options.format_id = null;
      options.format_has_audio = false;
      options.audio_format = 'original';
    } else if (mode === 'subtitles') {
      options.engine = 'auto';
      options.format_id = null;
      options.format_has_audio = false;
      options.audio_format = 'original';
      options.subtitle_languages = ['zh-CN', 'zh', 'en'];
    } else if (mode === 'live') {
      options.engine = 'streamlink';
      options.format_id = null;
      options.format_has_audio = false;
      options.audio_format = 'original';
      options.stream_quality = 'best';
    } else {
      status.textContent = '不支持的下载类型';
      status.className = 'live-region error';
      return;
    }
    status.textContent = '正在创建任务…';
    status.className = 'live-region muted';
    try {
      await adminApi('/api/admin/tasks', {
        method: 'POST',
        body: {
          url: state.probe.webpage_url,
          title: state.probe.title,
          platform: state.probe.platform,
          options,
        },
      });
      status.textContent = '任务已创建';
      status.className = 'live-region good';
      showPage('tasks');
      await loadTasks(true);
    } catch (error) {
      status.textContent = error.message || '创建失败';
      status.className = 'live-region error';
    }
  }

  function cachedKoofr(taskId) {
    const item = state.koofrCache.get(taskId);
    if (!item) return null;
    if (Date.now() - item.at > KOOFR_TTL_MS) return null;
    return item.data;
  }

  async function fetchKoofrStatus(taskId, force = false) {
    if (!force) {
      const cached = cachedKoofr(taskId);
      if (cached) return cached;
    }
    if (state.koofrInFlight.has(taskId)) return cachedKoofr(taskId);
    state.koofrInFlight.add(taskId);
    try {
      const data = await adminApi(`/api/admin/tasks/${encodeURIComponent(taskId)}/koofr-status`);
      state.koofrCache.set(taskId, { at: Date.now(), data });
      return data;
    } catch (error) {
      const data = { saved: false, path: '', files: [], size: 0, error: error.message };
      state.koofrCache.set(taskId, { at: Date.now(), data });
      return data;
    } finally {
      state.koofrInFlight.delete(taskId);
    }
  }

  function taskActionsHtml(task) {
    const id = escapeHtml(task.id);
    const guest = task.owner_type === 'guest';
    const parts = [];
    if (['queued', 'downloading', 'processing'].includes(task.status)) {
      parts.push(`<button type="button" class="btn btn-danger btn-quiet" data-task-action="cancel" data-id="${id}">取消</button>`);
    }
    if (['failed', 'cancelled'].includes(task.status)) {
      parts.push(`<button type="button" class="btn btn-secondary btn-quiet" data-task-action="retry" data-id="${id}">重试</button>`);
    }
    if (task.status === 'completed' && task.output_path) {
      parts.push(`<button type="button" class="btn btn-primary btn-quiet" data-task-action="download" data-id="${id}">下载文件</button>`);
    }
    if (!guest && (task.status === 'completed' || task.status === 'expired')) {
      parts.push(`<button type="button" class="btn btn-secondary btn-quiet" data-task-action="koofr-status" data-id="${id}">查看 Koofr 状态</button>`);
      if (task.status === 'completed') {
        parts.push(`<button type="button" class="btn btn-secondary btn-quiet" data-task-action="koofr-save" data-id="${id}">保存到 Koofr</button>`);
      }
      parts.push(`<button type="button" class="btn btn-ghost btn-quiet" data-task-action="koofr-download" data-id="${id}">从 Koofr 下载</button>`);
    }
    if (['completed', 'failed', 'cancelled', 'expired'].includes(task.status)) {
      parts.push(`<button type="button" class="btn btn-ghost btn-quiet" data-task-action="delete" data-id="${id}">删除</button>`);
    }
    return parts.join('');
  }

  function renderTasks() {
    const box = document.getElementById('adminTaskList');
    if (!state.tasks.length) {
      box.innerHTML = '<p class="muted">暂无任务</p>';
      return;
    }
    box.innerHTML = state.tasks.map((task) => {
      const guest = task.owner_type === 'guest';
      const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
      const koofr = cachedKoofr(task.id);
      const koofrHtml = koofr
        ? `<div class="details"><div class="${koofr.saved ? 'good' : 'muted'}">${koofr.saved ? '已保存到 Koofr' : (koofr.error ? escapeHtml(koofr.error) : 'Koofr 无副本')}</div>${koofr.saved && koofr.path ? `<div class="muted mono">${escapeHtml(koofr.path)}</div>` : ''}</div>`
        : '';
      return `<article class="task">
        <div class="task-top">
          <strong>${escapeHtml(task.title || task.url || '未命名任务')}</strong>
          <span class="tag ${guest ? 'guest' : 'admin'}">${guest ? '游客任务' : '管理员任务'}</span>
        </div>
        <div class="row muted">
          <span>${escapeHtml(STATUS_LABEL[task.status] || task.status || '-')}</span>
          <span>${escapeHtml(task.platform || '-')}</span>
          <span>${escapeHtml(formatSize(task.output_size))}</span>
          <span>${progress.toFixed(1)}%</span>
        </div>
        <div class="progress-bar" aria-hidden="true"><span style="width:${progress}%"></span></div>
        <div class="row muted">
          <span>创建：${escapeHtml(formatTime(task.created))}</span>
          <span>完成：${escapeHtml(formatTime(task.finished))}</span>
          ${task.output_path ? `<span>本地：${escapeHtml(fileName(task.output_path))}</span>` : '<span>本地：无文件</span>'}
        </div>
        ${task.error_message ? `<p class="error">${escapeHtml(task.error_code || '')}${task.error_code ? '：' : ''}${escapeHtml(task.error_message)}</p>` : ''}
        ${task.log_tail ? `<details class="details"><summary>诊断日志</summary><pre>${escapeHtml(task.log_tail)}</pre></details>` : ''}
        ${koofrHtml}
        <div class="task-actions">${taskActionsHtml(task)}</div>
      </article>`;
    }).join('');
  }

  async function loadTasks(force = true) {
    if (state.taskInFlight && !force) return;
    state.taskInFlight = true;
    try {
      state.tasks = await adminApi('/api/admin/tasks') || [];
      renderTasks();
      schedulePolling();
    } catch (error) {
      document.getElementById('adminTaskList').innerHTML = `<p class="error">${escapeHtml(error.message || '加载任务失败')}</p>`;
    } finally {
      state.taskInFlight = false;
    }
  }

  function filenameFromDisposition(header, fallback) {
    if (!header) return fallback;
    const utf8 = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
    if (utf8) {
      try { return decodeURIComponent(utf8[1].trim().replace(/^"|"$/g, '')); } catch { /* ignore */ }
    }
    const plain = /filename\s*=\s*("?)([^";]+)\1/i.exec(header);
    if (plain) return plain[2].trim();
    return fallback;
  }

  async function downloadAuthorized(path, fallbackName) {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (response.status === 401) {
      clearSession('登录已失效，请重新登录');
      throw new Error('登录已失效');
    }
    if (!response.ok) {
      let data = null;
      try { data = await response.json(); } catch { data = null; }
      throw new Error(detailMessage(data, '下载失败'));
    }
    const filename = filenameFromDisposition(response.headers.get('Content-Disposition'), fallbackName);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename || fallbackName;
    link.rel = 'noopener';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
  }

  async function handleTaskAction(action, id, button) {
    try {
      if (action === 'cancel') await adminApi(`/api/admin/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      else if (action === 'retry') await adminApi(`/api/admin/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' });
      else if (action === 'delete') {
        if (!confirm('确定删除该任务？')) return;
        await adminApi(`/api/admin/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
      } else if (action === 'download') {
        await downloadAuthorized(`/api/admin/tasks/${encodeURIComponent(id)}/download`, 'download.bin');
        return;
      } else if (action === 'koofr-status') {
        if (button) button.disabled = true;
        await fetchKoofrStatus(id, true);
        renderTasks();
        return;
      } else if (action === 'koofr-save') {
        if (button) button.disabled = true;
        const data = await adminApi(`/api/admin/tasks/${encodeURIComponent(id)}/save-to-koofr`, { method: 'POST' });
        state.koofrCache.set(id, { at: Date.now(), data });
        renderTasks();
        return;
      } else if (action === 'koofr-download') {
        await downloadAuthorized(`/api/admin/tasks/${encodeURIComponent(id)}/koofr-download`, 'koofr-download.zip');
        return;
      }
      await loadTasks(true);
    } catch (error) {
      alert(error.message || '操作失败');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function loadSettings() {
    const data = await adminApi('/api/admin/settings');
    document.getElementById('maxFile').value = data.max_file_size_gb ?? '';
    document.getElementById('maxRes').value = data.max_resolution ?? '';
    document.getElementById('retention').value = data.retention_hours ?? '';
    document.getElementById('minFree').value = data.min_free_gb ?? '';
    document.getElementById('sleep').value = data.request_sleep_seconds ?? '';
    state.settingsProxyMasked = data.proxy_url || '';
    state.clearProxy = false;
    document.getElementById('proxy').value = '';
    document.getElementById('proxy').placeholder = state.settingsProxyMasked
      ? `当前代理：${state.settingsProxyMasked}（留空保持）`
      : '未设置代理';
    fillGuestPolicy(data.guest_policy || {});
  }

  function fillGuestPolicy(policy) {
    const map = {
      max_file_size_gb: 'gp_max_file_size_gb',
      default_resolution: 'gp_default_resolution',
      max_resolution: 'gp_max_resolution',
      retention_minutes: 'gp_retention_minutes',
      min_free_gb: 'gp_min_free_gb',
      emergency_free_gb: 'gp_emergency_free_gb',
      request_sleep_seconds: 'gp_request_sleep_seconds',
      max_active_tasks_per_guest: 'gp_max_active_tasks_per_guest',
      max_queued_tasks_per_guest: 'gp_max_queued_tasks_per_guest',
      global_guest_concurrency: 'gp_global_guest_concurrency',
      max_video_duration_minutes: 'gp_max_video_duration_minutes',
      ai_transcription_max_duration_minutes: 'gp_ai_transcription_max_duration_minutes',
      ai_transcription_global_concurrency: 'gp_ai_transcription_global_concurrency',
      ai_transcription_hourly_limit_per_guest: 'gp_ai_transcription_hourly_limit_per_guest',
      allow_ai_transcription: 'gp_allow_ai_transcription',
    };
    Object.entries(map).forEach(([key, id]) => {
      const node = document.getElementById(id);
      if (!node) return;
      if (node.tagName === 'SELECT') node.value = String(!!policy[key]);
      else node.value = policy[key] ?? '';
    });
    ['gp_allow_cookie', 'gp_allow_koofr', 'gp_allow_live_download'].forEach((id) => {
      const node = document.getElementById(id);
      if (node) {
        node.value = 'false';
        node.disabled = true;
      }
    });
  }

  function readNumber(id, { min = null, integer = false } = {}) {
    const raw = document.getElementById(id).value;
    if (raw === '' || raw == null) throw new Error('请填写所有必填项');
    const value = Number(raw);
    if (!Number.isFinite(value) || Number.isNaN(value)) throw new Error('存在无效数字');
    if (min != null && value < min) throw new Error('存在小于允许范围的数值');
    return integer ? Math.trunc(value) : value;
  }

  function readGuestPolicy() {
    return {
      max_file_size_gb: readNumber('gp_max_file_size_gb', { min: 0.1 }),
      default_resolution: readNumber('gp_default_resolution', { min: 144, integer: true }),
      max_resolution: readNumber('gp_max_resolution', { min: 144, integer: true }),
      retention_minutes: readNumber('gp_retention_minutes', { min: 1, integer: true }),
      min_free_gb: readNumber('gp_min_free_gb', { min: 0.1 }),
      emergency_free_gb: readNumber('gp_emergency_free_gb', { min: 0.1 }),
      request_sleep_seconds: readNumber('gp_request_sleep_seconds', { min: 0 }),
      max_active_tasks_per_guest: readNumber('gp_max_active_tasks_per_guest', { min: 1, integer: true }),
      max_queued_tasks_per_guest: readNumber('gp_max_queued_tasks_per_guest', { min: 1, integer: true }),
      global_guest_concurrency: readNumber('gp_global_guest_concurrency', { min: 1, integer: true }),
      max_video_duration_minutes: readNumber('gp_max_video_duration_minutes', { min: 1, integer: true }),
      allow_ai_transcription: document.getElementById('gp_allow_ai_transcription').value === 'true',
      ai_transcription_max_duration_minutes: readNumber('gp_ai_transcription_max_duration_minutes', { min: 1, integer: true }),
      ai_transcription_global_concurrency: readNumber('gp_ai_transcription_global_concurrency', { min: 1, integer: true }),
      ai_transcription_hourly_limit_per_guest: readNumber('gp_ai_transcription_hourly_limit_per_guest', { min: 1, integer: true }),
      allow_cookie: false,
      allow_koofr: false,
      allow_live_download: false,
    };
  }

  async function saveSettings(event) {
    if (event) event.preventDefault();
    const form = document.getElementById('settingsForm');
    const msg = document.getElementById('settingsMsg');
    if (!form.reportValidity()) return;
    let body;
    try {
      body = {
        max_file_size_gb: readNumber('maxFile', { min: 0.1 }),
        max_resolution: readNumber('maxRes', { min: 144, integer: true }),
        retention_hours: readNumber('retention', { min: 1, integer: true }),
        min_free_gb: readNumber('minFree', { min: 0.1 }),
        request_sleep_seconds: readNumber('sleep', { min: 0 }),
      };
    } catch (error) {
      msg.textContent = error.message || '设置校验失败';
      msg.className = 'live-region error';
      return;
    }
    const proxyInput = document.getElementById('proxy').value.trim();
    if (proxyInput) {
      body.proxy_url = proxyInput;
      state.clearProxy = false;
    } else if (state.clearProxy) {
      body.proxy_url = '';
    }
    msg.textContent = '正在保存…';
    msg.className = 'live-region muted';
    try {
      await adminApi('/api/admin/settings', { method: 'PUT', body });
      state.clearProxy = false;
      await loadSettings();
      msg.textContent = '系统设置已保存';
      msg.className = 'live-region good';
    } catch (error) {
      msg.textContent = error.message || '保存失败';
      msg.className = 'live-region error';
    }
  }

  async function saveGuestPolicy(event) {
    if (event) event.preventDefault();
    const form = document.getElementById('guestPolicyForm');
    const msg = document.getElementById('guestPolicyMsg');
    if (!form.reportValidity()) return;
    let policy;
    try {
      policy = readGuestPolicy();
    } catch (error) {
      msg.textContent = error.message || '游客策略校验失败';
      msg.className = 'live-region error';
      return;
    }
    msg.textContent = '正在保存游客策略…';
    msg.className = 'live-region muted';
    try {
      await adminApi('/api/admin/settings', {
        method: 'PUT',
        body: { guest_policy: policy },
      });
      await loadSettings();
      msg.textContent = '游客策略已保存';
      msg.className = 'live-region good';
    } catch (error) {
      msg.textContent = error.message || '保存失败';
      msg.className = 'live-region error';
    }
  }

  async function loadAiSettings() {
    const status = document.getElementById('aiStatus');
    try {
      const data = await adminApi('/api/admin/transcription/settings');
      status.textContent = data.configured
        ? `AI 字幕已配置 · 模型 ${data.model || '-'} · 来源 ${data.source || '-'}`
        : 'AI 字幕未配置：只能下载平台已有字幕';
      status.className = data.configured ? 'good' : 'muted';
    } catch {
      status.textContent = '无法读取 AI 字幕配置';
      status.className = 'error';
    }
  }

  async function saveAiKey() {
    const input = document.getElementById('aiKey');
    const apiKey = input.value.trim();
    if (!apiKey) {
      alert('请输入 API Key，或使用清除按钮');
      return;
    }
    await adminApi('/api/admin/transcription/settings', {
      method: 'PUT',
      body: { api_key: apiKey },
    });
    input.value = '';
    await loadAiSettings();
  }

  async function clearAiKey() {
    if (!confirm('确定清除硅基流动 API Key？')) return;
    await adminApi('/api/admin/transcription/settings', {
      method: 'PUT',
      body: { clear: true },
    });
    document.getElementById('aiKey').value = '';
    await loadAiSettings();
  }

  async function loadAuthStatus() {
    const data = await adminApi('/api/admin/auth/status');
    const alertBox = document.getElementById('defaultPasswordAlert');
    const textBox = document.getElementById('authStatusText');
    if (data.using_default_password) {
      alertBox.classList.remove('hidden');
      textBox.textContent = '检测到默认密码风险，请尽快修改管理员密码。';
    } else {
      alertBox.classList.add('hidden');
      textBox.textContent = '当前未使用默认密码。';
    }
  }

  async function changePassword() {
    const currentPassword = document.getElementById('currentPassword').value;
    const newPassword = document.getElementById('newPassword').value;
    const confirmPassword = document.getElementById('confirmPassword').value;
    const msg = document.getElementById('passwordMsg');
    if (newPassword.length < 8) {
      msg.textContent = '新密码至少 8 位';
      msg.className = 'live-region error';
      return;
    }
    if (newPassword !== confirmPassword) {
      msg.textContent = '两次输入的新密码不一致';
      msg.className = 'live-region error';
      return;
    }
    if (currentPassword === newPassword) {
      msg.textContent = '新密码不能与当前密码相同';
      msg.className = 'live-region error';
      return;
    }
    try {
      await adminApi('/api/admin/change-password', {
        method: 'POST',
        body: { current_password: currentPassword, new_password: newPassword },
      });
      document.getElementById('currentPassword').value = '';
      document.getElementById('newPassword').value = '';
      document.getElementById('confirmPassword').value = '';
      clearSession('密码已修改，请重新登录');
    } catch (error) {
      msg.textContent = error.message || '修改失败';
      msg.className = 'live-region error';
    }
  }

  async function uploadCookie(event) {
    event.preventDefault();
    const status = document.getElementById('cookieStatus');
    const button = document.getElementById('cookieUploadBtn');
    const file = document.getElementById('cookieFile').files[0];
    if (!file) {
      status.textContent = '请选择 .txt Cookie 文件';
      status.className = 'live-region error';
      return;
    }
    if (!/\.txt$/i.test(file.name)) {
      status.textContent = '仅接受 .txt 文件';
      status.className = 'live-region error';
      return;
    }
    const form = new FormData();
    form.append('file', file);
    const platform = encodeURIComponent(document.getElementById('cookiePlatform').value.trim() || 'youtube');
    const label = encodeURIComponent(document.getElementById('cookieLabel').value.trim() || file.name);
    button.disabled = true;
    status.textContent = '正在上传…';
    status.className = 'live-region muted';
    try {
      await adminApi(`/api/admin/cookies?platform=${platform}&label=${label}`, {
        method: 'POST',
        body: form,
      });
      document.getElementById('cookieFile').value = '';
      status.textContent = '上传成功';
      status.className = 'live-region good';
      await loadCookies();
    } catch (error) {
      status.textContent = error.message || '上传失败';
      status.className = 'live-region error';
    } finally {
      button.disabled = false;
    }
  }

  function showPage(page) {
    state.page = page;
    pageTitle.textContent = PAGE_TITLES[page] || page;
    document.querySelectorAll('.nav button').forEach((button) => {
      button.classList.toggle('active', button.getAttribute('data-page') === page);
    });
    document.querySelectorAll('.section').forEach((section) => {
      section.classList.toggle('active', section.id === `page-${page}`);
    });
    if (page === 'dashboard') loadHealth(true);
    if (page === 'probe') loadCookies().catch(() => {});
    if (page === 'tasks') loadTasks(true);
    if (page === 'cookies') loadCookies().catch(() => {});
    if (page === 'koofr') loadHealth(true);
    if (page === 'settings') {
      loadSettings().catch((error) => {
        document.getElementById('settingsMsg').textContent = error.message || '读取设置失败';
      });
      loadAiSettings().catch(() => {});
    }
    if (page === 'guest-policy') {
      loadSettings().catch((error) => {
        document.getElementById('guestPolicyMsg').textContent = error.message || '读取策略失败';
      });
    }
    if (page === 'security') {
      loadAuthStatus().catch((error) => {
        document.getElementById('authStatusText').textContent = error.message || '读取状态失败';
      });
    }
  }

  async function boot() {
    setSession(state.token);
    await loadHealth(true);
    await loadTasks(true);
    schedulePolling();
    showPage('dashboard');
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    loginError.textContent = '';
    try {
      const response = await fetch('/api/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminPassword.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(data, '密码错误'));
      setSession(data.token);
      await boot();
    } catch (error) {
      loginError.textContent = error.message || '登录失败';
    }
  });

  document.getElementById('logoutBtn').addEventListener('click', () => clearSession('已退出登录'));
  document.getElementById('logoutBtn2').addEventListener('click', () => clearSession('已退出登录'));
  document.getElementById('refreshDashBtn').addEventListener('click', () => loadHealth(true));
  document.getElementById('refreshKoofrBtn').addEventListener('click', () => loadHealth(true));
  document.getElementById('refreshTasksBtn').addEventListener('click', () => loadTasks(true));
  document.getElementById('adminProbeForm').addEventListener('submit', probeAdmin);
  document.getElementById('settingsForm').addEventListener('submit', saveSettings);
  document.getElementById('proxy').addEventListener('input', () => {
    if (document.getElementById('proxy').value.trim()) state.clearProxy = false;
  });
  document.getElementById('clearProxyBtn').addEventListener('click', () => {
    state.clearProxy = true;
    document.getElementById('proxy').value = '';
    document.getElementById('proxy').placeholder = '将在保存后清除代理';
    document.getElementById('settingsMsg').textContent = '已标记清除代理，请点击保存';
    document.getElementById('settingsMsg').className = 'live-region warn';
  });
  document.getElementById('guestPolicyForm').addEventListener('submit', saveGuestPolicy);
  document.getElementById('saveAiBtn').addEventListener('click', () => {
    saveAiKey().catch((error) => alert(error.message || '保存失败'));
  });
  document.getElementById('clearAiBtn').addEventListener('click', () => {
    clearAiKey().catch((error) => alert(error.message || '清除失败'));
  });
  document.getElementById('changePasswordBtn').addEventListener('click', changePassword);
  document.getElementById('cookieForm').addEventListener('submit', uploadCookie);

  document.querySelectorAll('.nav button').forEach((button) => {
    button.addEventListener('click', () => showPage(button.getAttribute('data-page')));
  });

  document.getElementById('adminResult').addEventListener('click', (event) => {
    const modeBtn = event.target.closest('[data-admin-mode]');
    if (modeBtn) {
      createAdminTask(modeBtn.getAttribute('data-admin-mode'));
      return;
    }
    const formatBtn = event.target.closest('[data-admin-format]');
    if (formatBtn && state.probe) {
      const id = formatBtn.getAttribute('data-admin-format');
      state.selectedVideo = (state.probe.video_options || []).find((item) => String(item.format_id) === id) || null;
      renderAdminFormats();
    }
  });

  document.getElementById('adminTaskList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-task-action][data-id]');
    if (!button) return;
    handleTaskAction(button.getAttribute('data-task-action'), button.getAttribute('data-id'), button);
  });

  document.getElementById('cookieList').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-cookie-delete]');
    if (!button) return;
    const id = button.getAttribute('data-cookie-delete');
    if (!confirm('确定删除该 Cookie？')) return;
    try {
      await adminApi(`/api/admin/cookies/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await loadCookies();
    } catch (error) {
      alert(error.message || '删除失败');
    }
  });

  document.addEventListener('visibilitychange', () => {
    schedulePolling();
    if (document.visibilityState === 'visible' && state.token) {
      loadHealth(false);
      if (state.page === 'tasks') loadTasks(false);
    }
  });

  window.addEventListener('beforeunload', stopPolling);

  if (state.token) {
    boot().catch(() => clearSession('登录已失效，请重新登录'));
  } else {
    loginView.classList.remove('hidden');
    appView.classList.add('hidden');
  }
})();
