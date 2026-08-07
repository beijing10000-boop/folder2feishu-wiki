import {
  ArrowRight,
  CheckCircle2,
  Cloud,
  Database,
  File,
  Folder,
  FolderTree,
  HardDrive,
  LoaderCircle,
  RefreshCcw,
  ScanLine,
  SearchCheck,
  ShieldCheck
} from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import { severityIcon, translateTechnicalMessage } from "../lib/labels";
import {
  Button,
  EmptyState,
  Metric,
  Panel,
  PanelBody,
  PanelHeading,
  TreeBranch
} from "../components/ui";
import { formatBytes } from "../utils";

function hashNote(scan: NonNullable<ReturnType<typeof useConsole>["scan"]>, scanActive: boolean) {
  const s = scan.summary;
  if (s.hashes_deferred) {
    return `快速盘点：已延后 ${s.hashes_deferred.toLocaleString()} 个新文件哈希，迁移时按需计算`;
  }
  if (s.hashes_reused) return `已复用 ${s.hashes_reused.toLocaleString()} 个文件哈希`;
  if (scanActive) {
    return s.megabytes_per_second
      ? `${s.hash_workers || 8} 路并发 · 当前 ${s.megabytes_per_second.toLocaleString()} MB/秒`
      : `首次盘点采用 ${s.hash_workers || 8} 路并发读取`;
  }
  return s.megabytes_per_second
    ? `平均读取 ${s.megabytes_per_second.toLocaleString()} MB/秒`
    : "文件内容已完成校验";
}

export function ScanStep() {
  const c = useConsole();
  const { scan, scanActive, tree, draft, configReady } = c;

  const scanState = scanActive
    ? "扫描进行中"
    : scan?.summary.scan_complete
      ? "盘点完整"
      : scan?.status === "FAILED"
        ? "盘点失败"
        : "等待开始";

  return (
    <div className="view-stack">
      <Panel className="command-bar" tone={scan?.summary.scan_complete ? "green" : ""}>
        <div className="command-bar__inner">
          <div className="command-bar__copy">
            <span className="eyebrow">只读盘点</span>
            <h2>真实读取本地目录，建立可恢复迁移台账</h2>
            <p>此阶段递归读取目录、文件标识、大小、时间与 SHA-256，不修改或删除任何本地文件。</p>
            <div className="route-chips">
              <span title={draft.source_root}>
                <HardDrive size={14} /> {draft.source_root || "未配置本地目录"}
              </span>
              <ArrowRight size={13} />
              <span>
                <Database size={14} /> 本地迁移台账
              </span>
            </div>
          </div>
          <div className="command-bar__actions">
            <span className={`state-chip is-${(scan?.status ?? "idle").toLowerCase()}`}>
              {scanActive ? (
                <LoaderCircle className="spin" size={15} />
              ) : scan?.summary.scan_complete ? (
                <CheckCircle2 size={15} />
              ) : (
                <ScanLine size={15} />
              )}
              {scanState}
            </span>
            <Button
              icon={scan ? RefreshCcw : ScanLine}
              busy={scanActive}
              onClick={c.startScan}
              disabled={!configReady || scanActive}
            >
              {scanActive ? "盘点进行中" : scan ? "重新只读盘点" : "开始只读盘点"}
            </Button>
            <Button
              variant="primary"
              icon={ArrowRight}
              disabled={!scan?.summary.scan_complete}
              onClick={() => c.setStep("preflight")}
            >
              进入预检
            </Button>
          </div>
        </div>
        {scanActive ? (
          <div className="live-strip" role="status" aria-live="polite">
            <progress />
            <strong>已盘点 {(scan?.scanned_items ?? 0).toLocaleString()} 项</strong>
            <span>
              {translateTechnicalMessage(scan?.last_message) || "正在读取本地目录和计算文件指纹…"}
            </span>
            {scan?.current_path ? <small title={scan.current_path}>{scan.current_path}</small> : null}
          </div>
        ) : null}
      </Panel>

      {!scan ? (
        <Panel>
          <EmptyState
            icon={ScanLine}
            title="尚未建立本地目录台账"
            copy="配置页已只读验证目录存在且根层可枚举；点击“开始只读盘点”后，系统会进一步检查全部内容和云端占位状态。"
            action={
              <Button
                variant="primary"
                icon={ScanLine}
                busy={scanActive}
                onClick={c.startScan}
                disabled={!configReady || scanActive}
              >
                {scanActive ? "盘点进行中" : "开始只读盘点"}
              </Button>
            }
          />
        </Panel>
      ) : (
        <>
          <div className="metric-grid">
            <Metric icon={File} label="文件" value={scan.summary.files.toLocaleString()} />
            <Metric icon={Folder} label="目录" value={scan.summary.folders.toLocaleString()} />
            <Metric
              icon={HardDrive}
              label="数据量"
              value={formatBytes(scan.summary.bytes)}
              note={hashNote(scan, scanActive)}
            />
            <Metric
              icon={Cloud}
              label="云端占位文件"
              value={scan.summary.placeholders.toLocaleString()}
              note={
                scan.summary.placeholders ? "本轮延迟上传，不阻断其他文件" : "本地内容可继续检查"
              }
              tone={scan.summary.placeholders ? "amber" : "green"}
            />
          </div>

          <div className="split">
            <Panel className="tree-panel">
              <PanelHeading
                eyebrow="本地目录树"
                title="原目录盘点结果"
                copy="文件夹是一等对象；空目录也会在飞书云盘中创建对应文件夹。"
                icon={FolderTree}
              />
              <div className="tree-view">
                {(tree.length ? tree : scan.tree).length ? (
                  (tree.length ? tree : scan.tree).map((node) => (
                    <TreeBranch key={node.id} node={node} onExpand={c.loadTreeChildren} />
                  ))
                ) : (
                  <EmptyState
                    icon={FolderTree}
                    title="正在生成目录预览"
                    copy="大型目录会分批写入台账，扫描完成后显示抽样树。"
                  />
                )}
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

            <div className="stack">
              <Panel tone={scan.checks.some((check) => check.blocking) ? "red" : "green"}>
                <PanelHeading
                  eyebrow="盘点证据"
                  title="本地完整性检查"
                  copy="占位、不可读或超限对象会阻断；飞书不支持的 0 字节文件将记录后自动跳过。"
                  icon={SearchCheck}
                />
                <dl className="stat-quad">
                  <div>
                    <dt>不可读</dt>
                    <dd>{scan.summary.unreadable.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>0 字节</dt>
                    <dd>{scan.summary.empty_files.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>名称过长</dt>
                    <dd>{scan.summary.too_long_names.toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>预计上传调用</dt>
                    <dd>{scan.summary.upload_calls.toLocaleString()}</dd>
                  </div>
                </dl>
                <div className="finding-list">
                  {scan.checks.length ? (
                    scan.checks.map((check) => {
                      const Icon = severityIcon[check.severity];
                      return (
                        <article className={`finding is-${check.severity}`} key={check.code}>
                          <Icon size={15} />
                          <div>
                            <strong>{translateTechnicalMessage(check.title)}</strong>
                            <span>{translateTechnicalMessage(check.message)}</span>
                          </div>
                          {check.count ? <b>{check.count}</b> : null}
                        </article>
                      );
                    })
                  ) : (
                    <p className="finding-clear">
                      <CheckCircle2 size={16} /> 暂未发现本地盘点问题。
                    </p>
                  )}
                </div>
              </Panel>
            </div>
          </div>

          <Panel>
            <PanelBody className="handoff">
              <ShieldCheck size={20} />
              <div>
                <span className="eyebrow">预检交接</span>
                <h3 className="card-title">本页确认本地事实</h3>
                <p className="card-copy">
                  盘点完整后，下一步才会用固定授权身份真实检查云盘目标文件夹、容器编辑权限、
                  云盘根目录与租户容量。
                </p>
              </div>
            </PanelBody>
          </Panel>
        </>
      )}
    </div>
  );
}
