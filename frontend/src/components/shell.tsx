import { AlertTriangle, CheckCircle2, LoaderCircle, OctagonX, RefreshCcw, UploadCloud } from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import { translateTechnicalMessage } from "../lib/labels";
import { Button } from "./ui";

export function BootScreen() {
  return (
    <main className="boot" aria-busy="true">
      <div className="boot__card">
        <span className="brand__mark" aria-hidden="true">
          <UploadCloud size={20} />
        </span>
        <LoaderCircle className="spin" size={26} aria-hidden="true" />
        <h1>正在建立本机安全会话</h1>
        <p>读取迁移台账、授权状态与上次断点…</p>
      </div>
    </main>
  );
}

export function BootError({ message }: { message: string }) {
  return (
    <main className="boot">
      <div className="boot__card boot__card--error" role="alert">
        <OctagonX size={28} aria-hidden="true" />
        <h1>本机迁移服务连接失败</h1>
        <p>{message}</p>
        <Button icon={RefreshCcw} variant="primary" onClick={() => window.location.reload()}>
          重新连接
        </Button>
      </div>
    </main>
  );
}

export function BackgroundTaskBar() {
  const c = useConsole();
  const task = c.backgroundTask;
  if (!task || task.state !== "RUNNING") return null;

  const percent = task.total ? Math.min(100, Math.round((task.completed / task.total) * 100)) : 0;

  return (
    <section className="bgtask" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={17} aria-hidden="true" />
      <div className="bgtask__copy">
        <strong>{task.kind === "PLAN" ? "正在生成差异计划" : "正在执行远端对账"}</strong>
        <span>
          {translateTechnicalMessage(task.last_message) || "后台任务已受理，正在准备执行…"}
        </span>
        {task.current_path ? <small title={task.current_path}>{task.current_path}</small> : null}
      </div>
      <div className="bgtask__meter">
        <div className="bgtask__numbers">
          <span>
            {task.completed.toLocaleString()} / {task.total.toLocaleString()}
          </span>
          <strong>{percent}%</strong>
        </div>
        <div className="progress-track">
          <i style={{ width: `${percent}%` }} />
        </div>
      </div>
      <Button variant="ghost" onClick={c.stopBackgroundTask}>
        停止任务
      </Button>
    </section>
  );
}

export function StatusBar() {
  const { auth, project } = useConsole();
  return (
    <footer className="statusbar">
      <span>
        <i className={auth.authorized ? "ok" : "warning"} />
        {`用户授权 ${auth.authorized ? "已就绪" : "等待中"}`}
      </span>
      <span>
        <i className={project ? "ok" : ""} /> 本地台账安全写入
      </span>
      <span>
        <i className="ok" /> 仅限本机访问
      </span>
      <span className="statusbar__right">源目录保护 · 只读</span>
    </footer>
  );
}

export function Toast() {
  const { notice, setNotice } = useConsole();
  if (!notice) return null;
  return (
    <div className={`toast is-${notice.tone}`} role="status" aria-live="polite">
      {notice.tone === "error" ? (
        <OctagonX size={17} />
      ) : notice.tone === "warning" ? (
        <AlertTriangle size={17} />
      ) : (
        <CheckCircle2 size={17} />
      )}
      <span>{notice.text}</span>
      <button type="button" aria-label="关闭提示" onClick={() => setNotice(undefined)}>
        ×
      </button>
    </div>
  );
}
