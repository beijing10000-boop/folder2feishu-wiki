import { FolderKanban, Gauge, ScanLine, Settings2, ShieldCheck, Waypoints } from "lucide-react";
import type { StepId } from "../types";
import type { IconType } from "./labels";

export interface StepDefinition {
  id: StepId;
  no: string;
  eyebrow: string;
  label: string;
  description: string;
  icon: IconType;
}

export const steps: StepDefinition[] = [
  {
    id: "workspace",
    no: "01",
    eyebrow: "数据项目",
    label: "选择项目",
    description: "选择或新建独立数据目录",
    icon: FolderKanban
  },
  {
    id: "config",
    no: "02",
    eyebrow: "基础配置",
    label: "配置",
    description: "集中填写与逐项验证",
    icon: Settings2
  },
  {
    id: "scan",
    no: "03",
    eyebrow: "本地盘点",
    label: "盘点",
    description: "只读扫描本地目录",
    icon: ScanLine
  },
  {
    id: "preflight",
    no: "04",
    eyebrow: "迁移预检",
    label: "预检",
    description: "权限、容量与文件",
    icon: ShieldCheck
  },
  {
    id: "plan",
    no: "05",
    eyebrow: "差异确认",
    label: "差异计划",
    description: "确认每一项写操作",
    icon: Waypoints
  },
  {
    id: "run",
    no: "06",
    eyebrow: "运行控制",
    label: "运行对账",
    description: "断点、速率与证据",
    icon: Gauge
  }
];

export const STEP_STORAGE_KEY = "folder2feishu:last-step";

const validSteps = new Set<StepId>(steps.map((item) => item.id));

export const readSavedStep = (): StepId => {
  try {
    const saved = window.localStorage.getItem(STEP_STORAGE_KEY) as StepId | null;
    return saved && validSteps.has(saved) ? saved : "workspace";
  } catch {
    return "workspace";
  }
};

export const rememberStep = (step: StepId): void => {
  try {
    window.localStorage.setItem(STEP_STORAGE_KEY, step);
  } catch {
    // Navigation persistence is a convenience only; the migration must work without it.
  }
};
