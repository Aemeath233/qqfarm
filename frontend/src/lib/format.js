export function duration(seconds) {
  if (!Number.isFinite(seconds)) return "未提供";
  const value = Math.max(0, Math.ceil(seconds));
  if (value < 60) return `${value}秒`;
  if (value < 3600)
    return `${Math.floor(value / 60)}分${value % 60 ? `${value % 60}秒` : ""}`;
  return `${Math.floor(value / 3600)}小时${Math.floor((value % 3600) / 60) ? `${Math.floor((value % 3600) / 60)}分` : ""}`;
}

export function maturity(seconds) {
  return seconds <= 0 ? "已成熟" : duration(seconds);
}
