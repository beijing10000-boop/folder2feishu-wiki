import { ArrowRight, BadgeCheck, Check, FileClock, ListFilter, Play, ShieldCheck, Waypoints } from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import { translateTechnicalMessage } from "../lib/labels";
import { Button, EmptyState, Panel, PanelBody, PanelHeading } from "../components/ui";
import { actionLabel, actionTone, formatBytes, formatLocalDateTime } from "../utils";

const DECISION_CHAIN: Array<[string, string, string]> = [
  ["01", "路径 + SHA 未变", "跳过"],
  ["02", "文件标识未变且内容一致", "移动 / 改名"],
  ["03", "内容发生变化", "安全换版"],
  ["04", "本地文件缺失", "仅报告"],
  ["05", "远端人工变化", "冲突停止"]
];

export function PlanStep() {
  const c = useConsole();
  const { plan, preflight, blocking, busy, actionFilter, setActionFilter, filteredActions } = c;

  if (!plan) {
    return (
      <Panel>
        <EmptyState
          icon={Waypoints}
          title="尚未生成差异计划"
          copy="先完成本地盘点和预检，系统才会列出准备写入飞书的每一项动作。"
          action={
            <Button
              icon={preflight?.writable ? Waypoints : ArrowRight}
              onClick={preflight?.writable ? c.buildPlan : () => c.setStep("preflight")}
              busy={busy === "plan"}
            >
              {preflight?.writable ? "现在生成计划" : "返回预检"}
            </Button>
          }
        />
      </Panel>
    );
  }

  return (
    <div className="view-stack">
      <Panel className="command-bar">
        <div className="command-bar__inner">
          <div className="command-bar__copy">
            <span className="eyebrow">不可变写入计划 · {plan.id}</span>
            <h2>每一项远端动作，先看清再执行</h2>
            <p>生成于 {formatLocalDateTime(plan.created_at)} · 程序不设累计上限</p>
          </div>
          <span className={`state-chip ${plan.confirmed ? "is-completed" : "is-pending"}`}>
            {plan.confirmed ? <BadgeCheck size={15} /> : <FileClock size={15} />}
            {plan.confirmed ? "计划已确认" : "等待最终确认"}
          </span>
        </div>
      </Panel>

      <div className="action-grid">
        {plan.counts.map((item) => (
          <button
            type="button"
            className={`action-counter tone-${actionTone[item.kind]} ${
              actionFilter === item.kind ? "is-selected" : ""
            }`}
            key={item.kind}
            aria-pressed={actionFilter === item.kind}
            onClick={() => setActionFilter(actionFilter === item.kind ? "ALL" : item.kind)}
          >
            <span>{actionLabel[item.kind]}</span>
            <strong>{item.count.toLocaleString()}</strong>
          </button>
        ))}
      </div>

      <div className="split">
        <Panel>
          <PanelHeading
            eyebrow="差异动作台账"
            title={actionFilter === "ALL" ? "差异动作样本" : actionLabel[actionFilter]}
            copy={`显示 ${filteredActions.length} 项代表性记录；完整台账可在运行页导出。`}
            icon={ListFilter}
            tools={
              actionFilter !== "ALL" ? (
                <Button variant="ghost" onClick={() => setActionFilter("ALL")}>
                  清除筛选
                </Button>
              ) : undefined
            }
          />
          <div className="data-table" role="table" aria-label="差异动作">
            <div className="data-table__head" role="row">
              <span role="columnheader">动作</span>
              <span role="columnheader">相对路径</span>
              <span role="columnheader">判定依据</span>
              <span role="columnheader">数据量</span>
            </div>
            {filteredActions.map((action) => (
              <div className="data-table__row" role="row" key={action.id}>
                <span role="cell">
                  <i className={`action-pill tone-${actionTone[action.kind]}`}>
                    {actionLabel[action.kind]}
                  </i>
                </span>
                <span role="cell" className="path-cell" title={action.relative_path}>
                  {action.relative_path}
                </span>
                <span role="cell">{translateTechnicalMessage(action.reason)}</span>
                <span role="cell" className="num-cell">
                  {action.bytes ? formatBytes(action.bytes) : "—"}
                </span>
              </div>
            ))}
          </div>
        </Panel>

        <div className="stack">
          <Panel>
            <PanelBody>
              <span className="eyebrow">决策规则</span>
              <h3 className="card-title">安全增量决策链</h3>
              <ol className="decision-chain">
                {DECISION_CHAIN.map(([no, condition, outcome]) => (
                  <li key={no}>
                    <span>{no}</span>
                    <b>{condition}</b>
                    <em>{outcome}</em>
                  </li>
                ))}
              </ol>
            </PanelBody>
          </Panel>

          <Panel tone={plan.confirmed ? "green" : "amber"}>
            <PanelBody className="launch">
              <ShieldCheck size={22} />
              <h3 className="card-title">{plan.confirmed ? "计划已锁定" : "确认后才能迁移"}</h3>
              <p className="card-copy">
                {plan.confirmed
                  ? `共 ${plan.writable_actions.toLocaleString()} 项写操作，可随时暂停并从断点恢复。`
                  : "确认只锁定当前盘点快照，不会立即上传。"}
              </p>
              {plan.confirmed ? (
                <Button
                  variant="primary"
                  icon={Play}
                  busy={busy === "run-start"}
                  onClick={c.startRun}
                  disabled={Boolean(blocking.length)}
                >
                  开始迁移到云盘
                </Button>
              ) : (
                <Button variant="primary" icon={Check} busy={busy === "confirm"} onClick={c.confirmPlan}>
                  最终确认当前计划
                </Button>
              )}
              {blocking.length ? <small className="launch__blocked">仍有预检阻断项，无法启动。</small> : null}
            </PanelBody>
          </Panel>
        </div>
      </div>
    </div>
  );
}
