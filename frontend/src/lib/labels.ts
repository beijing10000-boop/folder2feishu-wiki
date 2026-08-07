import {
  AlertTriangle,
  CheckCircle2,
  KeyRound,
  OctagonX,
  SearchCheck
} from "lucide-react";
import type { PlannedActionKind, RuntimeLogEntry, Severity } from "../types";
import { actionLabel } from "../utils";

export type IconType = typeof KeyRound;

export const DEFAULT_SCOPES = [
  "offline_access",
  "drive:drive",
  "drive:file:upload",
  "drive:quota_detail:read_one",
  "contact:user.employee_id:readonly"
];

export const scopeLabels: Record<string, string> = {
  offline_access: "保持用户授权",
  "drive:drive": "访问云空间",
  "drive:file:upload": "上传文件",
  "drive:quota_detail:read_one": "读取云空间容量",
  "contact:user.employee_id:readonly": "读取用户身份"
};

export const issueCodeLabels: Record<string, string> = {
  FEISHU_PERMISSION: "云盘权限",
  DRIVE_CAPACITY: "云空间容量",
  ONEDRIVE_PLACEHOLDER: "本地文件状态",
  OFFLINE_PLACEHOLDER: "云端占位文件",
  source_items: "本地文件状态",
  ZERO_BYTE: "空文件",
  NAME_LENGTH: "名称长度",
  TREE_LIMITS: "目录层级"
};

export const errorCodeLabels: Record<string, string> = {
  REMOTE_CHANGED: "远端内容已变化",
  PERMISSION_DENIED: "权限不足",
  RATE_LIMITED: "请求频率受限",
  TIMEOUT: "请求超时",
  INTERNAL_ERROR: "飞书服务内部错误"
};

export const severityIcon: Record<Severity, IconType> = {
  ok: CheckCircle2,
  warning: AlertTriangle,
  error: OctagonX,
  info: SearchCheck
};

export const statusLabel: Record<string, string> = {
  IDLE: "尚未运行",
  RUNNING: "正在迁移",
  PAUSED: "已暂停",
  INTERRUPTED: "迁移已中断",
  COMPLETED: "迁移完成",
  FAILED: "运行失败",
  STOPPED: "已停止",
  DISCOVERED: "已盘点",
  PLANNED: "已计划",
  UPLOADING: "上传中",
  DRIVE_UPLOADED: "云盘上传完成",
  WIKI_MOVING: "云盘归档中",
  VERIFYING: "远端核验",
  DONE: "已完成",
  RETRYABLE: "可重试",
  CONFLICT: "人工冲突",
  MANUAL_ACTION: "人工处理"
};

export const stageLabel: Record<string, string> = {
  QUEUED: "等待后台执行",
  PREFLIGHT: "参数与权限检查",
  PLANNING: "生成迁移计划",
  SCANNING: "扫描本地目录",
  DATA_MIGRATION: "迁移文件与目录",
  REMOTE_RECONCILIATION: "远端对账",
  COMPLETED: "全部完成",
  NEEDS_ATTENTION: "存在待处理项目",
  PAUSED: "安全暂停",
  INTERRUPTED: "服务中断",
  CANCELLED: "用户停止",
  FAILED: "执行失败",
  VERIFYING: "远端核验",
  WIKI_MOVING: "云盘归档中",
  RECONCILE: "远端对账",
  MIGRATION: "迁移执行"
};

export const describeStage = (stage?: string): string => {
  if (!stage) return "尚未记录";
  if (stageLabel[stage]) return stageLabel[stage];
  if (stage.startsWith("MIGRATING_")) {
    const action = stage.slice("MIGRATING_".length) as PlannedActionKind;
    return actionLabel[action] ? `正在${actionLabel[action]}` : "正在迁移项目";
  }
  return statusLabel[stage] ?? "迁移处理中";
};

export const translateTechnicalMessage = (message?: string): string => {
  if (!message) return "";
  return message
    .replace(/Feishu(?:APIError|OpenAPIError)|Feishu OpenAPI error/gi, "飞书接口错误")
    .replace(/OpenAPIError/gi, "开放接口错误")
    .replace(/request trigger frequency limit/gi, "请求频率超过飞书限制")
    .replace(/internal server error/gi, "飞书服务内部错误")
    .replace(/permission denied/gi, "权限不足")
    .replace(/rate limit(?:ed)?/gi, "请求频率受限")
    .replace(/request timeout|timed out|timeout/gi, "请求超时")
    .replace(/SHA-256 matched/gi, "SHA-256 校验一致")
    .replace(/App ID/gi, "应用编号")
    .replace(/App Secret|Secret/gi, "应用密钥")
    .replace(/Windows File ID/gi, "本机文件标识")
    .replace(/OneDrive/gi, "本地同步文件")
    .replace(/wiki_token/gi, "云盘对象令牌")
    .replace(/OAuth/gi, "用户授权")
    .replace(/Wiki/gi, "云盘")
    .replace(/source item has no remote mapping/gi, "源项目尚无远端映射");
};

export const describeErrorCode = (code?: string): string => {
  if (!code) return "";
  return errorCodeLabels[code] ?? (/^\d+$/.test(code) ? `错误码 ${code}` : "迁移异常");
};

const runtimeActionLabel: Record<string, string> = {
  CREATE_FOLDER: "创建目录",
  UPLOAD: "上传文件",
  MOVE: "移动文件",
  RENAME: "文件改名",
  VERSION_UPDATE: "更新版本",
  SKIP: "跳过未变化文件",
  REPORT_MISSING: "记录本地缺失"
};

export const describeRuntimeLog = (
  entry: RuntimeLogEntry
): { title: string; detail: string } => {
  const message = entry.message;
  if (message === "迁移项开始") {
    return {
      title: `${runtimeActionLabel[entry.action_type ?? ""] ?? "处理文件"}开始`,
      detail: entry.path || "正在处理迁移项"
    };
  }
  if (message === "迁移项完成") {
    return {
      title:
        entry.result === "skipped"
          ? "文件未变化，已跳过"
          : `${runtimeActionLabel[entry.action_type ?? ""] ?? "迁移项"}完成`,
      detail: entry.path || "迁移项已写入台账"
    };
  }
  if (message.includes("迁移项") && (entry.level === "ERROR" || entry.level === "WARNING")) {
    return { title: message, detail: entry.path || "详细原因已写入本机日志" };
  }
  if (message.includes("/upload_part") && message.includes("200 OK")) {
    return { title: "分片上传成功", detail: "飞书已接收一个 4 MB 文件分片" };
  }
  if (message.includes("/upload_prepare") && message.includes("200 OK")) {
    return { title: "分片会话已建立", detail: "飞书已返回大文件上传策略" };
  }
  if (message.includes("/upload_finish") && message.includes("200 OK")) {
    return { title: "大文件上传完成", detail: "全部分片已在飞书云盘合并" };
  }
  if (message.includes("/upload_all") && message.includes("200 OK")) {
    return { title: "文件上传成功", detail: "飞书云盘已接收文件" };
  }
  if (message.includes("rate limit") || message.includes("bucket deferred")) {
    return { title: "触发限流保护", detail: "程序正在按飞书返回时间自动冷却重试" };
  }
  if (message.includes("timeout") || (entry.error_type ?? "").toLowerCase().includes("timeout")) {
    return {
      title: "网络请求超时",
      detail: `程序已进入受控重试${entry.retry_count ? ` · 第${entry.retry_count}次` : ""}`
    };
  }
  return {
    title: entry.level === "ERROR" ? "远端请求失败" : "迁移服务记录",
    detail: entry.path || message
  };
};
