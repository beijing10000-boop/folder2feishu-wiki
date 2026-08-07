import { CheckCircle2, ChevronRight, File, Folder, LoaderCircle, SearchCheck, AlertTriangle } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { TreeNode } from "../types";
import type { IconType } from "../lib/labels";
import type { ValidationStatus } from "../lib/defaults";
import { formatBytes } from "../utils";

type Tone = "" | "green" | "amber" | "red";

export function Panel({
  children,
  className = "",
  tone = ""
}: {
  children: ReactNode;
  className?: string;
  tone?: Tone;
}) {
  return (
    <section className={`panel ${tone ? `panel--${tone}` : ""} ${className}`.trim()}>
      {children}
    </section>
  );
}

/** Body padding for panels whose content is not an edge-to-edge list or table. */
export function PanelBody({
  children,
  className = ""
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={`panel-body ${className}`.trim()}>{children}</div>;
}

export function PanelHeading({
  eyebrow,
  title,
  copy,
  icon: Icon,
  tools
}: {
  eyebrow?: string;
  title: string;
  copy?: string;
  icon?: IconType;
  tools?: ReactNode;
}) {
  return (
    <div className="panel-heading">
      {Icon ? (
        <div className="panel-heading__mark">
          <Icon size={17} aria-hidden="true" />
        </div>
      ) : null}
      <div className="panel-heading__text">
        {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
        <h2>{title}</h2>
        {copy ? <p>{copy}</p> : null}
      </div>
      {tools ? <div className="panel-heading__tools">{tools}</div> : null}
    </div>
  );
}

export function Button({
  children,
  icon: Icon,
  variant = "secondary",
  busy = false,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  icon?: IconType;
  variant?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean;
}) {
  return (
    <button
      className={`button button--${variant} ${className}`.trim()}
      {...props}
      disabled={busy || props.disabled}
    >
      {busy ? (
        <LoaderCircle className="spin" size={15} aria-hidden="true" />
      ) : Icon ? (
        <Icon size={15} aria-hidden="true" />
      ) : null}
      <span>{children}</span>
    </button>
  );
}

export function Metric({
  label,
  value,
  note,
  icon: Icon,
  tone = ""
}: {
  label: string;
  value: string | number;
  note?: string;
  icon: IconType;
  tone?: Tone;
}) {
  return (
    <div className={`metric ${tone ? `metric--${tone}` : ""}`.trim()}>
      <div className="metric__icon">
        <Icon size={17} aria-hidden="true" />
      </div>
      <div className="metric__text">
        <span className="metric__label">{label}</span>
        <strong>{value}</strong>
        {note ? <small>{note}</small> : null}
      </div>
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
  required
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  required?: boolean;
}) {
  return (
    <label className="field">
      <span className="field__label">
        {label}
        {required ? <b aria-hidden="true">*</b> : null}
      </span>
      {children}
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  description
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  description: string;
}) {
  return (
    <label className="toggle-row">
      <span className="toggle-row__text">
        <strong>{label}</strong>
        <small>{description}</small>
      </span>
      <span className={`toggle ${checked ? "is-on" : ""}`.trim()}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
        />
        <span aria-hidden="true" />
      </span>
    </label>
  );
}

const validationText: Record<ValidationStatus, string> = {
  passed: "验证通过",
  failed: "需要处理",
  checking: "验证中",
  idle: "待验证"
};

export function ValidationBadge({
  status,
  message
}: {
  status: ValidationStatus;
  message: string;
}) {
  const Icon =
    status === "passed"
      ? CheckCircle2
      : status === "failed"
        ? AlertTriangle
        : status === "checking"
          ? LoaderCircle
          : SearchCheck;
  return (
    <span className={`validation-badge is-${status}`} title={message}>
      <Icon className={status === "checking" ? "spin" : ""} size={13} aria-hidden="true" />
      {validationText[status]}
    </span>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  copy,
  action
}: {
  icon: IconType;
  title: string;
  copy: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state__signal">
        <Icon size={24} aria-hidden="true" />
      </div>
      <h3>{title}</h3>
      <p>{copy}</p>
      {action}
    </div>
  );
}

export function TreeBranch({
  node,
  level = 0,
  onExpand
}: {
  node: TreeNode;
  level?: number;
  onExpand?: (node: TreeNode) => void;
}) {
  const hasChildren = Boolean(node.child_count || node.children?.length);
  if (node.kind === "folder") {
    return (
      <details
        className="tree-branch"
        open={level === 0}
        onToggle={(event) => {
          if (event.currentTarget.open && hasChildren && node.children === undefined) {
            onExpand?.(node);
          }
        }}
      >
        <summary>
          <span className="tree-chevron">
            {hasChildren ? <ChevronRight size={13} aria-hidden="true" /> : <span />}
          </span>
          <Folder size={15} aria-hidden="true" />
          <span className="tree-name">{node.name}</span>
          <small>
            {node.loading ? "读取中…" : `${node.child_count ?? node.children?.length ?? 0} 项`}
          </small>
        </summary>
        {hasChildren ? (
          <div className="tree-children">
            {node.children?.map((child) => (
              <TreeBranch key={child.id} node={child} level={level + 1} onExpand={onExpand} />
            ))}
          </div>
        ) : null}
      </details>
    );
  }
  return (
    <div className="tree-file">
      <span className="tree-chevron" />
      <File size={14} aria-hidden="true" />
      <span className="tree-name">{node.name}</span>
      <small>{formatBytes(node.size)}</small>
    </div>
  );
}
