export type StepId = "config" | "scan" | "preflight" | "plan" | "run";
export type Severity = "ok" | "warning" | "error" | "info";
export type ItemStatus =
  | "DISCOVERED"
  | "PLANNED"
  | "UPLOADING"
  | "DRIVE_UPLOADED"
  | "WIKI_MOVING"
  | "VERIFYING"
  | "DONE"
  | "PAUSED"
  | "RETRYABLE"
  | "CONFLICT"
  | "MANUAL_ACTION";
export type PlannedActionKind =
  | "CREATE_FOLDER"
  | "UPLOAD"
  | "MOVE"
  | "RENAME"
  | "VERSION_UPDATE"
  | "MISSING"
  | "SKIP"
  | "CONFLICT";

export interface ApiEnvelope<T> {
  data?: T;
  detail?: string;
}

export interface AuthStatus {
  configured: boolean;
  authorized: boolean;
  app_id_masked?: string;
  user_name?: string;
  scopes: string[];
  expires_at?: string;
  message?: string;
}

export interface VerificationResult {
  ok: boolean;
  kind: "app" | "oauth" | "source" | "target";
  message: string;
  details: Record<string, string | number | boolean>;
}

export interface AppSettings {
  app_id: string;
  redirect_uri: string;
  scopes: string[];
  app_secret_configured: boolean;
  upload_qps: number;
  wiki_calls_per_minute: number;
  daily_upload_budget: number;
}

export interface AppSettingsInput {
  app_id: string;
  app_secret?: string;
  redirect_uri: string;
  scopes: string[];
  upload_qps?: number;
  wiki_calls_per_minute?: number;
  daily_upload_budget?: number;
}

export interface Project {
  id: string;
  name: string;
  source_root: string;
  target_wiki_url: string;
  target_space_id?: string;
  target_parent_token?: string;
  wrapper_name?: string;
  create_wrapper?: boolean;
  mode: "safe_incremental";
  last_run_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ProjectDraft {
  name: string;
  source_root: string;
  target_wiki_url: string;
  create_wrapper: boolean;
  wrapper_name?: string;
  mode?: "safe_incremental";
}

export interface PreflightCheck {
  code: string;
  title: string;
  message: string;
  severity: Severity;
  count?: number;
  blocking: boolean;
}

export interface InventorySummary {
  files: number;
  folders: number;
  bytes: number;
  empty_files: number;
  placeholders: number;
  too_long_names: number;
  unreadable: number;
  max_depth: number;
  max_siblings: number;
  upload_calls: number;
  hashes_computed?: number;
  hashes_reused?: number;
  estimated_days: number;
  scan_complete: boolean;
}

export interface TreeNode {
  id: string;
  name: string;
  relative_path: string;
  kind: "folder" | "file";
  size?: number;
  status?: ItemStatus;
  child_count?: number;
  loading?: boolean;
  children?: TreeNode[];
}

export interface ScanResult {
  scan_id: string;
  run_id?: string;
  status?: "PENDING" | "RUNNING" | "PAUSED" | "INTERRUPTED" | "COMPLETED" | "FAILED" | "STOPPED";
  scanned_items?: number;
  current_path?: string;
  stage?: string;
  last_message?: string;
  heartbeat_at?: string;
  summary: InventorySummary;
  checks: PreflightCheck[];
  tree: TreeNode[];
}

export interface PreflightResult {
  complete: boolean;
  writable: boolean;
  checked_at: string;
  checks: PreflightCheck[];
}

export interface ActionCount {
  kind: PlannedActionKind;
  count: number;
}

export interface PlannedAction {
  id: string;
  kind: PlannedActionKind;
  relative_path: string;
  reason: string;
  bytes?: number;
  blocking?: boolean;
}

export interface MigrationPlan {
  id: string;
  created_at: string;
  counts: ActionCount[];
  total_actions: number;
  writable_actions: number;
  estimated_upload_calls: number;
  estimated_days: number;
  confirmed: boolean;
  actions: PlannedAction[];
}

export interface QuotaState {
  upload_calls_used: number;
  upload_calls_limit: number;
  wiki_calls_minute: number;
  wiki_calls_limit: number;
  next_reset_at?: string;
}

export interface RunSummary {
  id: string;
  project_id: string;
  kind?: "SCAN" | "PLAN" | "MIGRATION" | "RECONCILIATION";
  stage?: string;
  state: "IDLE" | "RUNNING" | "PAUSED" | "INTERRUPTED" | "COMPLETED" | "FAILED" | "STOPPED";
  started_at?: string;
  finished_at?: string;
  current_path?: string;
  last_message?: string;
  error?: string;
  heartbeat_at?: string;
  elapsed_seconds?: number;
  retry_count?: number;
  worker_count?: number;
  in_flight?: number;
  skipped?: number;
  total: number;
  completed: number;
  failed: number;
  conflicts: number;
  bytes_total: number;
  bytes_completed: number;
  eta_seconds?: number;
  quota: QuotaState;
}

export interface RunItem {
  id: string;
  relative_path: string;
  status: ItemStatus;
  progress: number;
  attempts: number;
  error_code?: string;
  error_message?: string;
  updated_at: string;
}

export interface AuditEvent {
  id: string;
  occurred_at: string;
  level: "INFO" | "SUCCESS" | "WARNING" | "ERROR";
  stage: string;
  relative_path?: string;
  message: string;
  evidence?: string;
}

export interface DashboardState {
  version: string;
  auth: AuthStatus;
  project?: Project;
  scan?: ScanResult;
  plan?: MigrationPlan;
  run?: RunSummary;
  run_items: RunItem[];
  events: AuditEvent[];
}
