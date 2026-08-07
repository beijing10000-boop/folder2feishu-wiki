import { AlertTriangle, Check, RefreshCcw, UploadCloud } from "lucide-react";
import { api } from "../api/client";
import { useConsole } from "../hooks/consoleContext";
import { steps } from "../lib/steps";

/**
 * Two-row header. The step nav gets its own full-width row instead of being
 * squeezed between the brand and the status chips, which is what used to
 * truncate every step label to a single character.
 */
export function AppHeader() {
  const c = useConsole();
  const { auth, version, busy, step } = c;

  return (
    <header className="topbar">
      <div className="topbar__row">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            <UploadCloud size={19} />
          </span>
          <span className="brand__text">
            <strong>飞书云盘迁移</strong>
            <small>本地目录到飞书云盘</small>
          </span>
        </div>
        <div className="topbar__status">
          <button
            type="button"
            className="chip chip--button"
            onClick={c.refreshCurrentPage}
            disabled={busy === "page-refresh"}
            aria-label="刷新当前页面数据"
            title="重新读取本机台账和飞书状态，不离开当前步骤"
          >
            <RefreshCcw className={busy === "page-refresh" ? "spin" : ""} size={14} aria-hidden="true" />
            {busy === "page-refresh" ? "刷新中" : "刷新当前页"}
          </button>
          {api.isDemo ? <span className="chip chip--warning">演示数据</span> : null}
          <span className={`chip ${auth.authorized ? "chip--success" : "chip--warning"}`}>
            <i className="dot" />
            {auth.authorized ? "身份已锁定" : "等待授权"}
          </span>
          <span className="chip chip--quiet">版本 {version}</span>
        </div>
      </div>

      <nav className="steprail" aria-label="迁移步骤">
        {steps.map((item) => {
          const status = c.stepStatus(item.id);
          const enabled = c.stepEnabled(item.id);
          const Icon = item.icon;
          return (
            <button
              type="button"
              key={item.id}
              className={`steprail__item is-${status}`}
              onClick={() => c.setStep(item.id)}
              disabled={!enabled}
              title={c.stepDisabledReason(item.id)}
              aria-current={step === item.id ? "step" : undefined}
            >
              <span className="steprail__badge">
                {status === "done" ? <Check size={13} /> : item.no}
              </span>
              <span className="steprail__copy">
                <strong>
                  <Icon size={14} aria-hidden="true" />
                  {item.label}
                </strong>
                <small>{item.description}</small>
              </span>
              {status === "blocked" ? (
                <AlertTriangle size={14} className="steprail__warning" aria-label="存在阻断项" />
              ) : null}
            </button>
          );
        })}
      </nav>
    </header>
  );
}
