import type { PlannedActionKind } from "./types";

export const formatBytes = (bytes = 0): string => {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const tier = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** tier;
  return `${value >= 10 || tier === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[tier]}`;
};

export const formatEta = (seconds?: number): string => {
  if (seconds === undefined || seconds < 0) return "计算中";
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} 小时`;
  return `${(seconds / 86400).toFixed(1)} 天`;
};

const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/**
 * SQLite returns UTC timestamps without an offset on Windows.  JavaScript
 * otherwise interprets those strings as local wall-clock time.  Normalize a
 * missing offset to UTC, then let Intl render it in the browser/Windows local
 * timezone.
 */
export const parseServerDate = (value: string): Date => {
  const normalized = HAS_TIMEZONE.test(value) ? value : `${value}Z`;
  return new Date(normalized);
};

export const formatLocalDateTime = (value: string): string =>
  parseServerDate(value).toLocaleString("zh-CN");

export const formatLocalTime = (value: string): string =>
  parseServerDate(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });

export const formatPercent = (done: number, total: number): number =>
  total > 0 ? Math.min(100, Math.max(0, Math.round((done / total) * 100))) : 0;

export const actionLabel: Record<PlannedActionKind, string> = {
  CREATE_FOLDER: "新建目录",
  UPLOAD: "新增文件",
  MOVE: "移动",
  RENAME: "改名",
  VERSION_UPDATE: "版本更新",
  MISSING: "本地缺失",
  SKIP: "无需变更",
  CONFLICT: "人工冲突"
};

export const actionTone: Record<PlannedActionKind, string> = {
  CREATE_FOLDER: "blue",
  UPLOAD: "green",
  MOVE: "cyan",
  RENAME: "cyan",
  VERSION_UPDATE: "amber",
  MISSING: "slate",
  SKIP: "slate",
  CONFLICT: "red"
};

export const downloadBlob = (blob: Blob, filename: string): void => {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(href);
};
