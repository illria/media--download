(() => {
  const authHeaders = () => {
    const value = localStorage.mediaToken || '';
    return value ? { Authorization: `Bearer ${value}` } : {};
  };

  const formatBytes = value => {
    let size = Number(value || 0);
    if (!size) return '未知大小';
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (size < 1024) return `${size.toFixed(1)} ${unit}`;
      size /= 1024;
    }
    return `${size.toFixed(1)} TB`;
  };

  const addText = (parent