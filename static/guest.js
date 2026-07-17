(() => {
  const DEFAULT_LIMITS = {
    max_file_size_gb: 1,
    default_resolution: 720,
    max_resolution: 1080,
    retention_minutes: 30,
    max_video_duration_minutes: 60,
    allow_ai_transcription: true,
    ai_transcription_max_duration_minutes: 20,
    allow_subtitle_translation: true,
    subtitle_translation_max_duration_minutes: 60,
    subtitle_translation_hourly_limit_per_guest: 3,
    supported_subtitle_languages: {
      'zh-CN': '简体中文', 'zh-TW': '繁体中文', en: '英语', ja: '日语', ko: '韩语',
      vi: '越南语', th: '泰语', fr: '法语', de: '德语', es: '西班牙语',
      pt: '葡萄牙语', ru: '俄语', ar: '阿拉伯语', id: '印度尼西亚语', tr: '土耳其语', it: '意大利语',
      bn: '孟加拉语',
    },
  };
  const ERROR_MAP = {
    guest_probe_required: '请先解析链接，再创建下载任务。',
    guest_probe_failed: '游客解析失败，请确认链接为公开可访问的媒体。',
    guest_disk_space_low: '服务器当前空间或队列不足，请稍后重试。',
    guest_queue_limit_exceeded: '游客排队任务已达上限，请等待后再试。',
    guest_file_limit_exceeded: '该选项预计超过游客文件大小限制。',
    guest_resolution_limit_exceeded: '所选清晰度超过游客最高限制。',
    guest_duration_limit_exceeded: '媒体时长超过游客允许范围。',
    guest_duration_unknown: '无法确认媒体时长，游客暂不支持该链接。',
    guest_live_not_allowed: '游客不支持直播下载。',
    guest_format_not_allowed: '所选格式不可用，请重新解析。',
    guest_ai_duration_limit_exceeded: '无平台字幕时，媒体超过游客 AI 字幕允许时长。',
    guest_ai_disabled: '当前未启用游客 AI 字幕。',
    guest_translation_disabled: '当前未启用游客字幕翻译。',
    guest_translation_hourly_limit: '游客字幕翻译已达到每小时次数上限。',
    guest_translation_busy: '字幕翻译正在处理中，请稍后重试。',
    guest_translation_duration_limit: '该视频超过游客字幕翻译时长限制。',
    subtitle_source_language_unsupported: '不支持的源语言。',
    subtitle_target_language_unsupported: '不支持的目标语言。',
    subtitle_output_mode_unsupported: '不支持的字幕输出模式。',
    GUEST_AI_DURATION_LIMIT: '媒体超过游客 AI 字幕允许时长。',
    GUEST_AI_HOURLY_LIMIT: '游客 AI 字幕已达到每小时次数上限，请稍后再试。',
    GUEST_AI_BUSY: '游客 AI 字幕正在处理中，请稍后重试。',
    GUEST_AI_DISABLED: '当前未启用游客 AI 字幕。',
    GUEST_TRANSLATION_DISABLED: '当前未启用游客字幕翻译。',
    GUEST_TRANSLATION_HOURLY_LIMIT: '游客字幕翻译已达到每小时次数上限。',
    GUEST_TRANSLATION_BUSY: '字幕翻译正在处理中，请稍后重试。',
    GUEST_TRANSLATION_DURATION_LIMIT: '该视频超过游客字幕翻译时长限制。',
    ASR_QUALITY_FAILED: '语音识别结果质量过低，未生成字幕。',
    SUBTITLE_SOURCE_QUALITY_FAILED: '平台字幕质量异常，未继续翻译。',
    SUBTITLE_TRANSLATION_QUALITY_FAILED: '翻译质量未达标，已保留原字幕。',
    SUBTITLE_TRANSLATION_STRUCTURE_FAILED: '翻译结果结构异常，已保留原字幕。',
    SUBTITLE_SOURCE_LANGUAGE_UNCERTAIN: '无法可靠识别源语言，未继续翻译。',
    SUBTITLE_TRANSLATION_FAILED: '翻译失败，已保留原字幕。',
  };
  const OUTPUT_LABEL = { original: '仅原字幕', translated: '翻译字幕', bilingual: '双语字幕' };
  const STATUS_LABEL = {
    queued: '排队中',
    downloading: '下载中',
    processing: '处理中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    expired: '已过期',
  };
  const MODE_LABEL = {
    video: '视频',
    audio: '音频',
    thumbnail: '封面',
    subtitles: '字幕',
  };

  const state = {
    probe: null,
    selectedVideo: null,
    selectedAudio: null,
    tasks: [],
    limits: { ...DEFAULT_LIMITS },
    taskTimer: null,
    healthTimer: null,
    countdownTimer: null,
    taskInFlight: false,
    healthInFlight: false,
    pollKey: '',
  };

  const el = {
    url: document.getElementById('guestUrl'),
    probeForm: document.getElementById('probeForm'),
    probeBtn: document.getElementById('probeBtn'),
    probeStatus: document.getElementById('probeStatus'),
    health: document.getElementById('guestHealth'),
    healthText: document.getElementById('guestHealthText'),
    resultCard: document.getElementById('resultCard'),
    resultTitle: document.getElementById('resultTitle'),
    resultThumb: document.getElementById('resultThumb'),
    resultMeta: document.getElementById('resultMeta'),
    videoFormats: document.getElementById('videoFormats'),
    audioFormats: document.getElementById('audioFormats'),
    createStatus: document.getElementById('createStatus'),
    taskList: document.getElementById('taskList'),
    refreshTasksBtn: document.getElementById('refreshTasksBtn'),
    manualRefreshBtn: document.getElementById('manualRefreshBtn'),
    chipFileLimit: document.getElementById('chipFileLimit'),
    chipMaxRes: document.getElementById('chipMaxRes'),
    chipRetention: document.getElementById('chipRetention'),
    chipAiLimit: document.getElementById('chipAiLimit'),
    subtitleOptions: document.getElementById('subtitleOptions'),
    subtitleOutputMode: document.getElementById('subtitleOutputMode'),
    subtitleSourceLanguage: document.getElementById('subtitleSourceLanguage'),
    subtitleTargetLanguage: document.getElementById('subtitleTargetLanguage'),
    subtitleHelp: document.getElementById('subtitleHelp'),
  };

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
    if (!n) return '';
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (n < 1024) return `${n.toFixed(n >= 10 || unit === 'B' ? 0 : 1)} ${unit}`;
      n /= 1024;
    }
    return `${n.toFixed(1)} TB`;
  }

  function formatDuration(seconds) {
    const total = Number(seconds || 0);
    if (!total) return '未知';
    const mins = Math.floor(total / 60);
    const secs = Math.round(total % 60);
    if (mins >= 60) {
      const hours = Math.floor(mins / 60);
      const rem = mins % 60;
      return `${hours} 小时 ${rem} 分`;
    }
    return secs ? `${mins} 分 ${secs} 秒` : `${mins} 分钟`;
  }

  function formatTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString();
  }

  function friendlyError(payload, fallback = '请求失败') {
    if (!payload) return fallback;
    if (typeof payload === 'string') return payload;
    if (payload.message) return payload.message;
    if (payload.code) {
      const limits = state.limits || DEFAULT_LIMITS;
      if (payload.code === 'guest_file_limit_exceeded') {
        return `该选项预计超过游客 ${limits.max_file_size_gb} GB 限制。`;
      }
      if (payload.code === 'guest_resolution_limit_exceeded') {
        return `游客最高只支持 ${limits.max_resolution}p。`;
      }
      if (payload.code === 'guest_duration_limit_exceeded') {
        return `游客媒体最长支持 ${limits.max_video_duration_minutes} 分钟。`;
      }
      if (payload.code === 'guest_ai_duration_limit_exceeded' || payload.code === 'GUEST_AI_DURATION_LIMIT') {
        return `无平台字幕时，游客 AI 字幕最长支持 ${limits.ai_transcription_max_duration_minutes} 分钟。`;
      }
      if (payload.code === 'guest_ai_disabled' || payload.code === 'GUEST_AI_DISABLED') {
        return '当前未启用游客 AI 字幕。';
      }
      if (ERROR_MAP[payload.code]) return ERROR_MAP[payload.code];
    }
    if (payload.detail) return friendlyError(payload.detail, fallback);
    return fallback;
  }

  function limitBytes() {
    return Number(state.limits.max_file_size_gb || DEFAULT_LIMITS.max_file_size_gb) * (1024 ** 3);
  }

  function maxResolution() {
    return Number(state.limits.max_resolution || DEFAULT_LIMITS.max_resolution);
  }

  function defaultResolution() {
    return Number(state.limits.default_resolution || DEFAULT_LIMITS.default_resolution);
  }

  function validLanguageMap(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const entries = Object.entries(value).filter(([code, label]) => (
      typeof code === 'string'
      && code.trim()
      && code !== 'auto'
      && typeof label === 'string'
      && label.trim()
    ));
    return entries.length ? Object.fromEntries(entries) : null;
  }

  function fillLanguageSelects() {
    if (!el.subtitleSourceLanguage || !el.subtitleTargetLanguage || !el.subtitleOutputMode) return;
    const langs = validLanguageMap(state.limits.supported_subtitle_languages)
      || validLanguageMap(DEFAULT_LIMITS.supported_subtitle_languages)
      || { 'zh-CN': '简体中文' };
    const sourceValue = el.subtitleSourceLanguage.value || 'auto';
    const targetValue = el.subtitleTargetLanguage.value || 'zh-CN';
    const modeValue = el.subtitleOutputMode.value || 'translated';
    const languageOptions = Object.entries(langs).map(([code, label]) => (
      `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`
    )).join('');
    el.subtitleSourceLanguage.innerHTML = `<option value="auto">自动选择</option>${languageOptions}`;
    el.subtitleTargetLanguage.innerHTML = languageOptions || '<option value="zh-CN">简体中文</option>';
    if (!el.subtitleTargetLanguage.options.length) {
      el.subtitleTargetLanguage.innerHTML = '<option value="zh-CN" selected>简体中文</option>';
    }
    el.subtitleSourceLanguage.value = sourceValue === 'auto' || Object.prototype.hasOwnProperty.call(langs, sourceValue)
      ? sourceValue
      : 'auto';
    if (Object.prototype.hasOwnProperty.call(langs, targetValue)) {
      el.subtitleTargetLanguage.value = targetValue;
    } else if (Object.prototype.hasOwnProperty.call(langs, 'zh-CN')) {
      el.subtitleTargetLanguage.value = 'zh-CN';
    } else {
      el.subtitleTargetLanguage.selectedIndex = 0;
    }
    if (!state.limits.allow_subtitle_translation) {
      el.subtitleOutputMode.innerHTML = '<option value="original" selected>仅原字幕</option>';
      el.subtitleTargetLanguage.disabled = true;
      if (el.subtitleHelp) el.subtitleHelp.textContent = '当前未启用游客字幕翻译，仅可下载原字幕。';
    } else {
      el.subtitleOutputMode.innerHTML = `
        <option value="original">仅原字幕</option>
        <option value="translated">翻译字幕</option>
        <option value="bilingual">双语字幕</option>`;
      el.subtitleOutputMode.value = ['original', 'translated', 'bilingual'].includes(modeValue) ? modeValue : 'translated';
      el.subtitleTargetLanguage.disabled = el.subtitleOutputMode.value === 'original';
      if (el.subtitleHelp) {
        el.subtitleHelp.textContent = `优先使用平台已有目标语言字幕；没有目标语言字幕时使用 AI 翻译；平台完全没有字幕时会先尝试 AI 语音转写。翻译最长 ${state.limits.subtitle_translation_max_duration_minutes} 分钟。`;
      }
    }
  }

  function applyLimits(limits) {
    if (!limits || typeof limits !== 'object') return;
    state.limits = {
      max_file_size_gb: Number(limits.max_file_size_gb) || DEFAULT_LIMITS.max_file_size_gb,
      default_resolution: Number(limits.default_resolution) || DEFAULT_LIMITS.default_resolution,
      max_resolution: Number(limits.max_resolution) || DEFAULT_LIMITS.max_resolution,
      retention_minutes: Number(limits.retention_minutes) || DEFAULT_LIMITS.retention_minutes,
      max_video_duration_minutes: Number(limits.max_video_duration_minutes) || DEFAULT_LIMITS.max_video_duration_minutes,
      allow_ai_transcription: limits.allow_ai_transcription !== false,
      ai_transcription_max_duration_minutes: Number(limits.ai_transcription_max_duration_minutes) || DEFAULT_LIMITS.ai_transcription_max_duration_minutes,
      allow_subtitle_translation: limits.allow_subtitle_translation !== false,
      subtitle_translation_max_duration_minutes: Number(limits.subtitle_translation_max_duration_minutes) || DEFAULT_LIMITS.subtitle_translation_max_duration_minutes,
      subtitle_translation_hourly_limit_per_guest: Number(limits.subtitle_translation_hourly_limit_per_guest) || DEFAULT_LIMITS.subtitle_translation_hourly_limit_per_guest,
      supported_subtitle_languages: validLanguageMap(limits.supported_subtitle_languages)
        || DEFAULT_LIMITS.supported_subtitle_languages,
    };
    if (el.chipFileLimit) el.chipFileLimit.textContent = `单文件最大 ${state.limits.max_file_size_gb} GB`;
    if (el.chipMaxRes) el.chipMaxRes.textContent = `最高 ${state.limits.max_resolution}p`;
    if (el.chipRetention) el.chipRetention.textContent = `完成后保留 ${state.limits.retention_minutes} 分钟`;
    if (el.chipAiLimit) {
      el.chipAiLimit.textContent = state.limits.allow_ai_transcription
        ? `AI 字幕最长 ${state.limits.ai_transcription_max_duration_minutes} 分钟`
        : 'AI 字幕未启用';
    }
    fillLanguageSelects();
    if (state.probe) {
      state.selectedVideo = chooseDefaultVideo(state.probe.video_options || []);
      renderFormats();
    }
  }

  async function guestApi(path, options = {}) {
    const opts = {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: { ...(options.headers || {}) },
    };
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    let data = null;
    const raw = await response.text();
    try { data = raw ? JSON.parse(raw) : null; } catch { data = { detail: raw }; }
    if (!response.ok) {
      throw new Error(friendlyError(data && data.detail != null ? data.detail : data));
    }
    return data;
  }

  function setHealth(ok, message) {
    const dot = el.health.querySelector('.dot');
    el.healthText.textContent = message;
    dot.className = `dot ${ok ? 'ok' : 'warn'}`;
  }

  async function loadHealth() {
    if (state.healthInFlight) return;
    state.healthInFlight = true;
    try {
      const data = await guestApi('/api/guest/health');
      if (data.limits) applyLimits(data.limits);
      if (data.accepting_guest_tasks) {
        setHealth(true, `服务可用 · 排队 ${Number(data.queue_length || 0)} 个`);
      } else {
        setHealth(false, `暂停接收新任务 · 排队 ${Number(data.queue_length || 0)} 个`);
      }
    } catch {
      setHealth(false, '暂时无法获取服务状态');
    } finally {
      state.healthInFlight = false;
    }
  }

  function chooseDefaultVideo(options) {
    const maxRes = maxResolution();
    const preferred = defaultResolution();
    const list = (options || [])
      .filter((item) => Number(item.height || 0) > 0 && Number(item.height || 0) <= maxRes)
      .slice()
      .sort((a, b) => Number(b.height || 0) - Number(a.height || 0));
    if (!list.length) return null;
    const exact = list.find((item) => Number(item.height) === preferred);
    if (exact) return exact;
    const below = list.filter((item) => Number(item.height) <= preferred);
    return below[0] || list[list.length - 1];
  }

  function renderFormats() {
    const maxRes = maxResolution();
    const maxBytes = limitBytes();
    const videos = (state.probe.video_options || [])
      .filter((item) => Number(item.height || 0) <= maxRes);
    const audios = state.probe.audio_options || [];
    el.videoFormats.innerHTML = videos.map((item) => {
      const size = Number(item.filesize || 0);
      const over = size > maxBytes;
      const selected = state.selectedVideo && String(state.selectedVideo.format_id) === String(item.format_id);
      return `<button type="button" class="format-btn${selected ? ' active' : ''}" data-video-format="${escapeHtml(item.format_id)}" ${over ? 'disabled' : ''}>
        ${escapeHtml(item.label || `${item.height}p`)}
        <small>${escapeHtml(item.ext || '')}${item.has_audio ? ' · 含音频' : ' · 无音频'}${size ? ` · ${escapeHtml(formatSize(size))}` : ''}${over ? ' · 预计超过游客限制' : ''}</small>
      </button>`;
    }).join('') || `<p class="muted">没有可用视频格式，可直接使用默认 ${escapeHtml(defaultResolution())}p 创建。</p>`;

    el.audioFormats.innerHTML = audios.map((item) => {
      const size = Number(item.filesize || 0);
      const over = size > maxBytes;
      const selected = state.selectedAudio && String(state.selectedAudio.format_id) === String(item.format_id);
      return `<button type="button" class="format-btn${selected ? ' active' : ''}" data-audio-format="${escapeHtml(item.format_id)}" ${over ? 'disabled' : ''}>
        音频 ${escapeHtml(item.ext || 'm4a')}
        <small>${item.abr ? `${escapeHtml(item.abr)} kbps` : '自动码率'}${size ? ` · ${escapeHtml(formatSize(size))}` : ''}${over ? ' · 预计超过游客限制' : ''}</small>
      </button>`;
    }).join('') || '<p class="muted">没有列出音频格式时，可直接创建默认音频任务。</p>';
  }

  function showProbeResult(probe) {
    state.probe = probe;
    state.selectedVideo = chooseDefaultVideo(probe.video_options || []);
    state.selectedAudio = (probe.audio_options || [])[0] || null;
    el.resultCard.classList.remove('hidden');
    el.resultTitle.textContent = probe.title || '未命名媒体';
    if (probe.thumbnail) {
      el.resultThumb.src = probe.thumbnail;
      el.resultThumb.alt = `${probe.title || '媒体'} 缩略图`;
      el.resultThumb.classList.remove('hidden');
    } else {
      el.resultThumb.removeAttribute('src');
      el.resultThumb.alt = '暂无缩略图';
    }
    const hasSubs = Array.isArray(probe.subtitles) && probe.subtitles.length > 0;
    const aiNote = state.limits.allow_ai_transcription
      ? `无（将尝试 AI，最长 ${state.limits.ai_transcription_max_duration_minutes} 分钟）`
      : '无（当前未启用 AI）';
    el.resultMeta.innerHTML = `
      <div>平台：<strong>${escapeHtml(probe.platform || '-')}</strong></div>
      <div>作者：<strong>${escapeHtml(probe.uploader || '-')}</strong></div>
      <div>时长：<strong>${escapeHtml(formatDuration(probe.duration))}</strong></div>
      <div>平台字幕：<strong>${hasSubs ? '有' : aiNote}</strong></div>
      <div>解析策略：<strong>${escapeHtml(probe.download_strategy_label || probe.download_strategy || '自动')}</strong></div>
    `;
    renderFormats();
    el.createStatus.textContent = '';
  }

  async function probeUrl() {
    const url = el.url.value.trim();
    if (!url) {
      el.probeStatus.textContent = '请先粘贴媒体链接。';
      el.probeStatus.className = 'live-region error';
      return;
    }
    el.probeBtn.disabled = true;
    el.probeStatus.textContent = '正在解析…';
    el.probeStatus.className = 'live-region muted';
    try {
      const result = await guestApi('/api/guest/probe', {
        method: 'POST',
        body: { url },
      });
      showProbeResult(result);
      el.probeStatus.textContent = '解析成功，可选择下载类型。';
      el.probeStatus.className = 'live-region good';
    } catch (error) {
      el.probeStatus.textContent = error.message || ERROR_MAP.guest_probe_failed;
      el.probeStatus.className = 'live-region error';
      el.resultCard.classList.add('hidden');
      state.probe = null;
    } finally {
      el.probeBtn.disabled = false;
    }
  }

  async function createTask(mode) {
    if (!state.probe) {
      el.createStatus.textContent = ERROR_MAP.guest_probe_required;
      el.createStatus.className = 'live-region error';
      return;
    }
    const options = { mode };
    if (mode === 'video') {
      const selected = state.selectedVideo;
      options.resolution = Number((selected && selected.height) || defaultResolution());
      if (selected && selected.format_id) {
        options.format_id = String(selected.format_id);
        options.format_has_audio = !!selected.has_audio;
      }
    } else if (mode === 'audio') {
      const selected = state.selectedAudio;
      if (selected && selected.format_id) options.format_id = String(selected.format_id);
      options.audio_format = 'mp3';
    } else if (mode === 'subtitles') {
      if (!el.subtitleTargetLanguage || !el.subtitleTargetLanguage.options.length || !el.subtitleTargetLanguage.value) {
        el.createStatus.textContent = '请选择目标语言';
        el.createStatus.className = 'live-region error';
        return;
      }
      const outputMode = el.subtitleOutputMode?.value || 'translated';
      options.subtitle_output_mode = state.limits.allow_subtitle_translation ? outputMode : 'original';
      options.subtitle_source_language = el.subtitleSourceLanguage?.value || 'auto';
      options.subtitle_target_language = el.subtitleTargetLanguage.value;
    }
    el.createStatus.textContent = '正在创建任务…';
    el.createStatus.className = 'live-region muted';
    try {
      await guestApi('/api/guest/tasks', {
        method: 'POST',
        body: {
          url: state.probe.webpage_url || el.url.value.trim(),
          options,
        },
      });
      el.createStatus.textContent = '任务已创建。';
      el.createStatus.className = 'live-region good';
      await loadTasks(true);
    } catch (error) {
      el.createStatus.textContent = error.message || '创建任务失败';
      el.createStatus.className = 'live-region error';
    }
  }

  function countdownText(task) {
    if (task.status !== 'completed') return '';
    const expiresAt = task.expires_at || null;
    if (!expiresAt) return '文件会按游客保留策略自动清理';
    const expires = new Date(expiresAt).getTime();
    if (Number.isNaN(expires)) return '文件会按游客保留策略自动清理';
    const remain = expires - Date.now();
    if (remain <= 0) return '文件即将清理或已过期';
    const mins = Math.max(1, Math.ceil(remain / 60000));
    return `文件将在 ${mins} 分钟后自动清理`;
  }

  function taskActions(task) {
    const id = escapeHtml(task.id);
    if (task.status === 'queued') {
      return `<button type="button" class="btn btn-danger btn-quiet" data-action="cancel" data-id="${id}">取消任务</button>
              <button type="button" class="btn btn-ghost btn-quiet" data-action="delete" data-id="${id}">删除记录</button>`;
    }
    if (task.status === 'downloading' || task.status === 'processing') {
      return `<button type="button" class="btn btn-danger btn-quiet" data-action="cancel" data-id="${id}">取消任务</button>`;
    }
    if (task.status === 'completed') {
      const download = task.download_available
        ? `<button type="button" class="btn btn-primary btn-quiet" data-action="download" data-id="${id}">下载文件</button>`
        : '';
      return `${download}<button type="button" class="btn btn-ghost btn-quiet" data-action="delete" data-id="${id}">删除任务</button>`;
    }
    if (task.status === 'failed' || task.status === 'cancelled') {
      return `<button type="button" class="btn btn-secondary btn-quiet" data-action="retry" data-id="${id}">重试</button>
              <button type="button" class="btn btn-ghost btn-quiet" data-action="delete" data-id="${id}">删除任务</button>`;
    }
    if (task.status === 'expired') {
      return `<span class="muted">文件已过期，请重新解析原链接</span>
              <button type="button" class="btn btn-ghost btn-quiet" data-action="delete" data-id="${id}">删除记录</button>`;
    }
    return '';
  }

  function renderTasks() {
    if (!state.tasks.length) {
      el.taskList.innerHTML = '<p class="muted">暂无任务。解析成功后可创建下载。</p>';
      return;
    }
    el.taskList.innerHTML = state.tasks.map((task) => {
      const progress = Math.max(0, Math.min(100, Number(task.progress || 0)));
      const blocks = Math.round(progress / 5);
      const bar = Array.from({ length: 20 }, (_, index) => (
        index < blocks
          ? '<span style="grid-column:span 1;background:linear-gradient(90deg,var(--gemini-blue),var(--gemini-cyan))"></span>'
          : '<span style="grid-column:span 1;background:transparent"></span>'
      )).join('');
      const countdown = countdownText(task);
      return `<article class="task">
        <div class="task-top">
          <strong>${escapeHtml(task.title || '未命名任务')}</strong>
          <span class="tag">${escapeHtml(STATUS_LABEL[task.status] || task.status || '-')}</span>
        </div>
        <div class="row muted">
          <span>${escapeHtml(task.platform || '-')}</span>
          <span>${escapeHtml(MODE_LABEL[task.mode] || task.mode || '-')}</span>
          ${task.resolution ? `<span>${escapeHtml(task.resolution)}p</span>` : ''}
          ${task.mode === 'subtitles' ? `<span>${escapeHtml(OUTPUT_LABEL[task.subtitle_output_mode] || task.subtitle_output_mode || '字幕')}</span>` : ''}
          ${task.mode === 'subtitles' && task.subtitle_source_language ? `<span>${escapeHtml(task.subtitle_source_language)} → ${escapeHtml(task.subtitle_target_language || '-')}</span>` : ''}
          <span>${escapeHtml(formatSize(task.output_size) || '大小未知')}</span>
        </div>
        <div class="progress" aria-hidden="true">${bar}</div>
        <div class="row">
          <span>${progress.toFixed(1)}%</span>
          <span class="muted">${escapeHtml(task.speed || '')}</span>
          <span class="muted">${escapeHtml(task.eta || '')}</span>
        </div>
        <div class="row muted">
          <span>创建：${escapeHtml(formatTime(task.created))}</span>
          <span>完成：${escapeHtml(formatTime(task.finished))}</span>
        </div>
        ${countdown ? `<p class="help">${escapeHtml(countdown)}</p>` : ''}
        ${task.error_message ? `<p class="error">${escapeHtml(task.error_message)}</p>` : ''}
        <div class="task-actions">${taskActions(task)}</div>
      </article>`;
    }).join('');
  }

  function hasActiveTasks() {
    return state.tasks.some((task) => ['queued', 'downloading', 'processing'].includes(task.status));
  }

  function clearTimers() {
    if (state.taskTimer) clearInterval(state.taskTimer);
    if (state.healthTimer) clearInterval(state.healthTimer);
    if (state.countdownTimer) clearInterval(state.countdownTimer);
    state.taskTimer = null;
    state.healthTimer = null;
    state.countdownTimer = null;
    state.pollKey = '';
  }

  function pollProfile() {
    const visible = document.visibilityState === 'visible';
    const active = hasActiveTasks();
    return {
      key: `${visible ? 'vis' : 'hid'}:${active ? 'active' : 'idle'}`,
      taskMs: !visible ? 60000 : (active ? 5000 : 15000),
      healthMs: !visible ? 120000 : (active ? 30000 : 60000),
    };
  }

  function schedulePolling(force = false) {
    const profile = pollProfile();
    if (!force && profile.key === state.pollKey && state.taskTimer && state.healthTimer) return;
    if (state.taskTimer) clearInterval(state.taskTimer);
    if (state.healthTimer) clearInterval(state.healthTimer);
    if (state.countdownTimer) clearInterval(state.countdownTimer);
    state.pollKey = profile.key;
    state.taskTimer = setInterval(() => { loadTasks(false); }, profile.taskMs);
    state.healthTimer = setInterval(() => { loadHealth(); }, profile.healthMs);
    state.countdownTimer = setInterval(() => {
      if (state.tasks.some((task) => task.status === 'completed')) renderTasks();
    }, 30000);
  }

  async function loadTasks(force = false) {
    if (state.taskInFlight && !force) return;
    state.taskInFlight = true;
    try {
      state.tasks = await guestApi('/api/guest/tasks') || [];
      renderTasks();
      schedulePolling(false);
    } catch (error) {
      el.taskList.innerHTML = `<p class="error">${escapeHtml(error.message || '无法加载任务')}</p>`;
    } finally {
      state.taskInFlight = false;
    }
  }

  function downloadTask(id) {
    const link = document.createElement('a');
    link.href = `/api/guest/tasks/${encodeURIComponent(id)}/download`;
    link.rel = 'noopener';
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function handleTaskAction(action, id) {
    try {
      if (action === 'download') {
        downloadTask(id);
        return;
      }
      if (action === 'delete' && !confirm('确定删除该任务记录？')) return;
      if (action === 'cancel') {
        await guestApi(`/api/guest/tasks/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
      } else if (action === 'retry') {
        await guestApi(`/api/guest/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' });
      } else if (action === 'delete') {
        await guestApi(`/api/guest/tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });
      }
      await loadTasks(true);
    } catch (error) {
      alert(error.message || '操作失败');
    }
  }

  el.probeForm.addEventListener('submit', (event) => {
    event.preventDefault();
    probeUrl();
  });

  el.url.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      probeUrl();
    }
  });

  el.resultCard.addEventListener('click', (event) => {
    const modeBtn = event.target.closest('[data-mode]');
    if (modeBtn) {
      createTask(modeBtn.getAttribute('data-mode'));
      return;
    }
    const videoBtn = event.target.closest('[data-video-format]');
    if (videoBtn) {
      const id = videoBtn.getAttribute('data-video-format');
      state.selectedVideo = (state.probe.video_options || []).find((item) => String(item.format_id) === id) || null;
      renderFormats();
      return;
    }
    const audioBtn = event.target.closest('[data-audio-format]');
    if (audioBtn) {
      const id = audioBtn.getAttribute('data-audio-format');
      state.selectedAudio = (state.probe.audio_options || []).find((item) => String(item.format_id) === id) || null;
      renderFormats();
    }
  });

  el.taskList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action][data-id]');
    if (!button) return;
    handleTaskAction(button.getAttribute('data-action'), button.getAttribute('data-id'));
  });

  el.refreshTasksBtn.addEventListener('click', () => { loadTasks(true); });
  el.manualRefreshBtn.addEventListener('click', () => { loadTasks(true); loadHealth(); });

  document.addEventListener('visibilitychange', () => {
    schedulePolling(true);
    if (document.visibilityState === 'visible') {
      loadTasks(true);
      loadHealth();
    }
  });

  window.addEventListener('beforeunload', clearTimers);

  if (el.subtitleOutputMode) {
    el.subtitleOutputMode.addEventListener('change', () => {
      if (el.subtitleTargetLanguage) el.subtitleTargetLanguage.disabled = el.subtitleOutputMode.value === 'original' || !state.limits.allow_subtitle_translation;
    });
  }
  applyLimits(DEFAULT_LIMITS);
  loadHealth();
  loadTasks(true).then(() => schedulePolling(true));
})();
