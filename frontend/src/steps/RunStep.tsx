import {
  ArrowRight,
  CheckCircle2,
  CirclePause,
  CirclePlay,
  Download,
  FileCheck2,
  FileWarning,
  Infinity as InfinityIcon,
  LoaderCircle,
  PanelTop,
  RefreshCcw,
  RotateCcw,
  SearchCheck,
  Square,
  SquareTerminal,
  UploadCloud
} from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import {
  describeErrorCode,
  describeRuntimeLog,
  describeStage,
  statusLabel,
  translateTechnicalMessage
} from "../lib/labels";
import { Button, EmptyState, Panel, PanelBody, PanelHeading } from "../components/ui";
import {
  formatBytes,
  formatEta,
  formatLocalDateTime,
  formatLocalTime,
  formatPercent
} from "../utils";

const baseName = (path: string) => path.split("\\").at(-1) ?? path;

export function RunStep() {
  const c = useConsole();
  const {
    run, runtimeLogs, events, busy, progress, byteProgress, runProcessed,
    runFilter, setRunFilter, filteredRunItems
  } = c;

  if (!run) {
    return (
      <Panel>
        <EmptyState
          icon={PanelTop}
          title="尚无迁移运行"
          copy="确认差异计划后，运行页会展示实时进度、接口速率、失败队列和远端证据。"
          action={
            <Button icon={ArrowRight} onClick={() => c.setStep("plan")}>
              返回差异计划
            </Button>
          }
        />
      </Panel>
    );
  }

  const statusFacts: Array<[string, string]> = [
    ["后台任务", run.state === "RUNNING" ? "正在运行" : "当前未运行"],
    ["当前阶段", describeStage(run.stage)],
    ["处理进度", `${runProcessed.toLocaleString()} / ${run.total.toLocaleString()}`],
    [
      "结果",
      `成功 ${run.completed.toLocaleString()} · 失败 ${run.failed.toLocaleString()} · 跳过 ${(run.skipped ?? 0).toLocaleString()} · 冲突 ${run.conflicts.toLocaleString()}`
    ],
    ["开始时间", run.started_at ? formatLocalDateTime(run.started_at) : "尚未开始"],
    ["已运行", formatEta(run.elapsed_seconds ?? 0)],
    ["最近心跳", run.heartbeat_at ? formatLocalDateTime(run.heartbeat_at) : "暂无"],
    ["重试次数", (run.retry_count ?? 0).toLocaleString()],
    ["并行工作线程", `${run.worker_count || 1} 个 · 执行中 ${run.in_flight ?? 0} 项`]
  ];

  return (
    <div className="view-stack">
      <Panel className={`run-head is-${run.state.toLowerCase()}`}>
        <div className="run-head__top">
          <div className="run-head__title">
            <span className="eyebrow">任务编号 · {run.id}</span>
            <h2>
              <i className="run-head__pulse" aria-hidden="true" />
              {statusLabel[run.state] ?? run.state}
            </h2>
            <p>
              {run.error
                ? translateTechnicalMessage(run.error)
                : run.last_message
                  ? translateTechnicalMessage(run.last_message)
                  : run.current_path || "当前没有正在处理的文件"}
            </p>
          </div>
          <div className="run-head__controls">
            {run.state === "RUNNING" ? (
              <Button icon={CirclePause} busy={busy === "run-pause"} onClick={() => c.controlRun("pause")}>
                安全暂停
              </Button>
            ) : (
              <Button
                variant="primary"
                icon={CirclePlay}
                busy={busy === "run-resume"}
                onClick={() => c.controlRun("resume")}
              >
                断点恢复
              </Button>
            )}
            <Button icon={RotateCcw} busy={busy === "run-retry"} onClick={() => c.controlRun("retry")}>
              重试失败项
            </Button>
            <Button variant="danger" icon={Square} busy={busy === "run-stop"} onClick={() => c.controlRun("stop")}>
              停止
            </Button>
          </div>
        </div>

        <div className="run-head__progress">
          <div className="progress-block">
            <div className="progress-block__top">
              <strong className="progress-block__pct">
                {progress}
                <small>%</small>
              </strong>
              <div className="progress-block__counts">
                <b>
                  {run.completed.toLocaleString()} / {run.total.toLocaleString()}
                </b>
                <span>
                  失败 {run.failed} · 冲突 {run.conflicts}
                </span>
              </div>
            </div>
            <div className="progress-track">
              <i style={{ width: `${progress}%` }} />
            </div>
            <div className="progress-block__foot">
              <span>
                数据进度 {byteProgress}% · {formatBytes(run.bytes_completed)} /{" "}
                {formatBytes(run.bytes_total)}
              </span>
              <span>
                预计剩余 <b>{formatEta(run.eta_seconds)}</b>
              </span>
            </div>
          </div>
          <div className="quota-block">
            <span className="eyebrow">上传接口调用</span>
            <div className="quota-block__value">
              <InfinityIcon size={22} aria-hidden="true" />
              <strong>{run.quota.upload_calls_used.toLocaleString()}</strong>
              <small>已调用</small>
            </div>
            <p>
              写入保护 {run.worker_count || 1} 个并行工作线程；飞书平台限额仍生效，限流自动冷却重试。
            </p>
          </div>
        </div>

        <dl className="run-facts" aria-label="迁移任务详细状态">
          {statusFacts.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
          <div className="run-facts__wide">
            <dt>当前对象</dt>
            <dd title={run.current_path}>{run.current_path || "无"}</dd>
          </div>
        </dl>
      </Panel>

      <div className="split">
        <Panel>
          <PanelHeading
            eyebrow="大文件分片"
            title="大文件分片进度"
            copy="每个分片成功后立即写入本地台账；页面刷新或服务重启后仍可继续显示。"
            icon={UploadCloud}
            tools={
              <span className="live-chip">
                <i />
                {run.active_uploads.length} 个上传中
              </span>
            }
          />
          {run.active_uploads.length ? (
            <div className="upload-list">
              {run.active_uploads.map((upload) => (
                <article className="upload-row" key={upload.action_id}>
                  <div className="upload-row__path">
                    <strong title={upload.relative_path}>{baseName(upload.relative_path)}</strong>
                    <span title={upload.relative_path}>{upload.relative_path}</span>
                  </div>
                  <div className="upload-row__numbers">
                    <b>
                      {upload.completed_parts.toLocaleString()} /{" "}
                      {upload.total_parts.toLocaleString()} 分片
                    </b>
                    <span>
                      {formatBytes(upload.uploaded_bytes)} / {formatBytes(upload.total_bytes)}
                    </span>
                  </div>
                  <div className="progress-track" aria-label={`上传进度 ${upload.percent}%`}>
                    <i style={{ width: `${Math.min(100, upload.percent)}%` }} />
                  </div>
                  <strong className="upload-row__pct">
                    {formatPercent(upload.completed_parts, upload.total_parts)}%
                  </strong>
                </article>
              ))}
            </div>
          ) : (
            <div className="inline-empty">
              <CheckCircle2 size={16} />
              <span>当前没有进行中的分片上传；不超过 20 MB 的文件会直接上传。</span>
            </div>
          )}
        </Panel>

        <Panel>
          <PanelHeading
            eyebrow="实时服务日志"
            title="实时迁移日志"
            copy="增量显示每个文件的开始、完成、跳过、重试和错误；完整记录同时保存在本机轮转日志中。"
            icon={SquareTerminal}
            tools={
              <span className="live-chip">
                <i />
                实时
              </span>
            }
          />
          <div className="log-list" aria-live="polite">
            {runtimeLogs.length ? (
              [...runtimeLogs].reverse().map((entry) => {
                const presentation = describeRuntimeLog(entry);
                return (
                  <article className={`log-row is-${entry.level.toLowerCase()}`} key={entry.id}>
                    <time>{formatLocalTime(entry.occurred_at)}</time>
                    <i />
                    <div>
                      <strong>{presentation.title}</strong>
                      <span>{presentation.detail}</span>
                    </div>
                    {entry.duration_ms ? <b>{Math.round(entry.duration_ms)} ms</b> : null}
                  </article>
                );
              })
            ) : (
              <div className="inline-empty">
                <LoaderCircle className={run.state === "RUNNING" ? "spin" : ""} size={16} />
                <span>
                  {run.state === "RUNNING" ? "等待下一条飞书请求记录…" : "当前没有新的运行日志"}
                </span>
              </div>
            )}
          </div>
        </Panel>
      </div>

      <div className="split">
        <Panel>
          <PanelHeading
            eyebrow="文件执行队列"
            title="文件执行队列"
            copy="上传令牌、分片进度和云盘对象令牌会在每一步立即写入本地台账。"
            icon={UploadCloud}
            tools={
              <div className="segmented" role="group" aria-label="队列筛选">
                {(["ALL", "ACTIVE", "FAILED"] as const).map((filter) => (
                  <button
                    type="button"
                    key={filter}
                    className={runFilter === filter ? "is-active" : ""}
                    aria-pressed={runFilter === filter}
                    onClick={() => setRunFilter(filter)}
                  >
                    {filter === "ALL" ? "全部" : filter === "ACTIVE" ? "处理中" : "失败 / 冲突"}
                  </button>
                ))}
              </div>
            }
          />
          {filteredRunItems.length ? (
            <div className="queue-list">
              {filteredRunItems.map((item) => (
                <article className={`queue-item is-${item.status.toLowerCase()}`} key={item.id}>
                  <span className="queue-item__status">
                    {item.status === "DONE" ? (
                      <CheckCircle2 size={16} />
                    ) : item.status === "UPLOADING" ? (
                      <LoaderCircle className="spin" size={16} />
                    ) : (
                      <FileWarning size={16} />
                    )}
                  </span>
                  <div className="queue-item__path">
                    <strong>{baseName(item.relative_path)}</strong>
                    <span>{item.relative_path}</span>
                    {item.error_message ? (
                      <small>
                        {describeErrorCode(item.error_code)} ·{" "}
                        {translateTechnicalMessage(item.error_message)}
                      </small>
                    ) : null}
                  </div>
                  <div className="queue-item__progress">
                    <span>{statusLabel[item.status] ?? item.status}</span>
                    <div className="progress-track">
                      <i style={{ width: `${item.progress}%` }} />
                    </div>
                  </div>
                  <b>{item.progress}%</b>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState icon={FileCheck2} title="此筛选下没有项目" copy="切换筛选条件查看其他执行记录。" />
          )}
        </Panel>

        <Panel>
          <PanelHeading eyebrow="远端证据" title="对账与审计" icon={SearchCheck} />
          <PanelBody className="audit-tools">
            <Button icon={RefreshCcw} busy={busy === "reconcile"} onClick={c.reconcile}>
              立即远端对账
            </Button>
            <div className="button-row">
              <Button
                variant="ghost"
                icon={Download}
                busy={busy === "export-csv"}
                onClick={() => c.exportAudit("csv")}
              >
                导出表格
              </Button>
              <Button
                variant="ghost"
                icon={Download}
                busy={busy === "export-json"}
                onClick={() => c.exportAudit("json")}
              >
                导出数据
              </Button>
            </div>
          </PanelBody>
          <div className="timeline">
            {events.length ? (
              events.map((event) => (
                <article className={`timeline__event is-${event.level.toLowerCase()}`} key={event.id}>
                  <i />
                  <time>{formatLocalTime(event.occurred_at)}</time>
                  <div>
                    <span>{describeStage(event.stage)}</span>
                    <strong>{translateTechnicalMessage(event.message)}</strong>
                    {event.relative_path ? <small>{event.relative_path}</small> : null}
                    {event.evidence ? <code>{translateTechnicalMessage(event.evidence)}</code> : null}
                  </div>
                </article>
              ))
            ) : (
              <p className="inline-empty">暂无审计事件。</p>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}
