import {
  AlertTriangle,
  ArrowRight,
  BadgeCheck,
  Check,
  Cloud,
  File,
  Folder,
  FolderTree,
  HardDrive,
  RefreshCcw,
  ScanLine,
  ShieldCheck,
  Waypoints
} from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import { issueCodeLabels, severityIcon, translateTechnicalMessage } from "../lib/labels";
import {
  Button,
  EmptyState,
  Metric,
  Panel,
  PanelHeading,
  TreeBranch
} from "../components/ui";
import { formatBytes } from "../utils";

export function PreflightStep() {
  const c = useConsole();
  const { scan, preflight, tree, blocking, busy } = c;

  if (!scan) {
    return (
      <Panel>
        <EmptyState
          icon={ScanLine}
          title="尚未盘点本地目录"
          copy="先在“盘点”中完成本地目录的真实只读扫描。"
          action={
            <Button icon={ArrowRight} onClick={() => c.setStep("scan")}>
              前往盘点
            </Button>
          }
        />
      </Panel>
    );
  }

  return (
    <div className="view-stack">
      <div className="metric-grid">
        <Metric icon={File} label="文件" value={scan.summary.files.toLocaleString()} />
        <Metric icon={Folder} label="目录" value={scan.summary.folders.toLocaleString()} />
        <Metric icon={HardDrive} label="数据量" value={formatBytes(scan.summary.bytes)} />
        <Metric
          icon={Cloud}
          label="预计上传接口调用"
          value={scan.summary.upload_calls.toLocaleString()}
          note="大文件分片会产生多次调用 · 飞书平台限额仍生效"
          tone="amber"
        />
      </div>

      {blocking.length ? (
        <div className="banner banner--danger" role="alert">
          <AlertTriangle size={20} />
          <div>
            <strong>{blocking.length} 类问题阻止正式迁移</strong>
            <span>处理后点击“重新执行预检”；工具不会绕过云端占位或权限问题。</span>
          </div>
          <Button icon={RefreshCcw} busy={busy === "preflight"} onClick={c.refreshPreflight}>
            重新执行预检
          </Button>
        </div>
      ) : (
        <div className="banner banner--success">
          <BadgeCheck size={20} />
          <div>
            <strong>预检通过，可以生成差异计划</strong>
            <span>生成计划仍不会对飞书执行写入。</span>
          </div>
          <Button variant="primary" icon={Waypoints} busy={busy === "plan"} onClick={c.buildPlan}>
            生成差异计划
          </Button>
        </div>
      )}

      <div className="split split--stretch">
        <Panel>
          <PanelHeading
            eyebrow="预检矩阵"
            title="权限、容量与文件检查"
            copy="阻断项必须清零；警告项会说明自动处理方式或需要关注的边界。"
            icon={ShieldCheck}
          />
          <div className="check-grid">
            {(preflight?.checks ?? scan.checks).map((check) => {
              const Icon = severityIcon[check.severity];
              return (
                <article className={`check-card is-${check.severity}`} key={check.code}>
                  <span className="check-card__icon">
                    <Icon size={17} />
                  </span>
                  <div>
                    <span className="check-card__code">
                      {issueCodeLabels[check.code] ?? "预检项目"}
                    </span>
                    <h3>{translateTechnicalMessage(check.title)}</h3>
                    <p>{translateTechnicalMessage(check.message)}</p>
                  </div>
                  {check.count ? <b>{check.count}</b> : <Check size={16} className="check-ok" />}
                </article>
              );
            })}
          </div>
        </Panel>

        <Panel className="tree-panel">
          <PanelHeading
            eyebrow="本地盘点"
            title="目录抽样"
            copy="目录是独立迁移对象，空目录也会保留。"
            icon={FolderTree}
          />
          <div className="tree-view">
            {(tree.length ? tree : scan.tree).map((node) => (
              <TreeBranch key={node.id} node={node} onExpand={c.loadTreeChildren} />
            ))}
          </div>
          <div className="panel-foot">
            <span>
              最大层级 <b>{scan.summary.max_depth}</b> / 14（项目根目录另占 1 层）
            </span>
            <span>
              单层最多节点 <b>{scan.summary.max_siblings}</b> / 1,500
            </span>
          </div>
        </Panel>
      </div>
    </div>
  );
}
