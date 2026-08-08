import { FormEvent, useMemo, useState } from "react";
import { CheckCircle2, Database, FolderOpen, Plus, ShieldCheck } from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import "../workspace.css";

export function WorkspaceStep() {
  const c = useConsole();
  const [name, setName] = useState("");
  const cleanName = name.trim();
  const duplicate = useMemo(
    () => c.workspaces?.items.some((item) => item.folder_name.toLowerCase() === cleanName.toLowerCase()),
    [c.workspaces, cleanName]
  );

  const create = (event: FormEvent) => {
    event.preventDefault();
    if (!cleanName || duplicate) return;
    void c.createWorkspace(cleanName);
  };

  return (
    <div className="workspace-step">
      <section className="panel workspace-root-card">
        <div className="workspace-root-card__icon" aria-hidden="true">
          <Database size={20} />
        </div>
        <div>
          <span className="eyebrow">统一数据根目录</span>
          <h2>{c.workspaces?.projects_root ?? "D:\\Folder2FeishuDrive\\Projects"}</h2>
          <p>
            程序只读取该目录的直接子文件夹。每个子文件夹独立保存迁移配置、加密授权、
            SQLite 台账、断点和审计记录。
          </p>
        </div>
        <div className="workspace-root-card__path" title={c.workspaces?.projects_root}>
          {c.workspaces?.projects_root ?? "正在读取项目目录…"}
        </div>
      </section>

      <div className="workspace-step__grid">
        <section className="panel workspace-list-card">
          <div className="panel__header workspace-section-head">
            <div>
              <span className="eyebrow">现有数据项目</span>
              <h2>选择要打开的数据文件夹</h2>
              <p>选择后，后端会读取对应目录中的配置和迁移台账。</p>
            </div>
            <span className="chip chip--quiet">{c.workspaces?.items.length ?? 0} 个项目</span>
          </div>

          <div className="workspace-list" role="list">
            {c.workspaces?.items.length ? (
              c.workspaces.items.map((item) => (
                <article
                  className={`workspace-list__item ${item.active ? "is-active" : ""}`.trim()}
                  key={item.folder_name}
                  role="listitem"
                >
                  <span className="workspace-list__folder" aria-hidden="true">
                    <FolderOpen size={20} />
                  </span>
                  <div className="workspace-list__copy">
                    <div className="workspace-list__title">
                      <strong>{item.project_name || item.folder_name}</strong>
                      {item.active ? (
                        <span className="status-tag status-tag--ok">
                          <CheckCircle2 size={13} /> 当前项目
                        </span>
                      ) : null}
                    </div>
                    <span>数据文件夹：{item.folder_name}</span>
                    <small>{item.folder_path}</small>
                  </div>
                  <div className="workspace-list__meta">
                    <span>{item.has_ledger ? "已有迁移台账" : "尚未配置"}</span>
                    <button
                      type="button"
                      className={item.active ? "button button--secondary" : "button button--primary"}
                      disabled={c.busy.startsWith("workspace-")}
                      onClick={() => void c.selectWorkspace(item.folder_name)}
                    >
                      {item.active ? "进入配置" : "选择项目"}
                    </button>
                  </div>
                </article>
              ))
            ) : (
              <div className="workspace-empty">
                <FolderOpen size={30} />
                <strong>还没有数据项目</strong>
                <p>请在右侧创建第一个项目，程序会自动建立同名数据文件夹。</p>
              </div>
            )}
          </div>
        </section>

        <section className="panel workspace-create-card">
          <div className="workspace-create-card__icon" aria-hidden="true">
            <Plus size={20} />
          </div>
          <span className="eyebrow">新增项目</span>
          <h2>创建独立迁移项目</h2>
          <p>项目名称会同时作为数据文件夹名称，创建后自动进入配置步骤。</p>

          <form onSubmit={create}>
            <label htmlFor="workspace-name">项目名称 / 数据文件夹名称</label>
            <input
              id="workspace-name"
              value={name}
              maxLength={80}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：JustFab 或 Team-FabDazzle"
              autoComplete="off"
            />
            <small className={duplicate ? "field-message field-message--error" : "field-message"}>
              {duplicate
                ? "同名项目已存在，请从左侧直接选择。"
                : `将创建：${c.workspaces?.projects_root ?? "D:\\Folder2FeishuDrive\\Projects"}\\${cleanName || "项目名称"}`}
            </small>
            <button
              type="submit"
              className="button button--primary workspace-create-card__submit"
              disabled={!cleanName || duplicate || c.busy.startsWith("workspace-")}
            >
              <Plus size={15} />
              {c.busy === "workspace-create" ? "正在创建…" : "创建并进入配置"}
            </button>
          </form>

          <div className="workspace-safety-note">
            <ShieldCheck size={17} />
            <span>
              切换项目不会删除任何数据。当前项目如有运行、排队或暂停中的任务，后端会拒绝切换。
            </span>
          </div>
        </section>
      </div>
    </div>
  );
}
