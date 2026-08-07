import {
  ArrowRight,
  BadgeCheck,
  Check,
  Cloud,
  Database,
  ExternalLink,
  Gauge,
  HardDrive,
  Infinity as InfinityIcon,
  KeyRound,
  LockKeyhole,
  Route,
  SearchCheck,
  Server,
  Settings2,
  ShieldCheck
} from "lucide-react";
import { useConsole } from "../hooks/consoleContext";
import { DEFAULT_SCOPES, scopeLabels } from "../lib/labels";
import { validationChecklist } from "../lib/defaults";
import { Button, Field, Panel, PanelBody, PanelHeading, Toggle, ValidationBadge } from "../components/ui";

export function ConfigStep() {
  const c = useConsole();
  const {
    settings, setSettings, secret, setSecret, auth, draft, setDraft, validation,
    configReady, passedCount, busy, markValidation, invalidateDownstream
  } = c;

  /** Every credential edit invalidates the verdict it produced. */
  const touchAppConfig = (message: string) => {
    markValidation("app", "idle", message);
    markValidation("oauth", "idle", "应用配置变化后需要重新验证用户授权身份");
    invalidateDownstream("config");
  };

  return (
    <div className="view-stack">
      <Panel className="gate" tone={configReady ? "green" : "amber"}>
        <div className="gate__head">
          <div className="gate__title">
            <span className="panel-heading__mark">
              <Settings2 size={17} />
            </span>
            <div>
              <span className="eyebrow">必要配置检查</span>
              <h2>先配置、逐项验证，再进入迁移流程</h2>
            </div>
          </div>
          <div className="gate__actions">
            <span className={`gate__score ${configReady ? "is-ready" : ""}`}>
              <strong>{passedCount}</strong>
              <span>/ 6 项已验证</span>
            </span>
            <Button icon={SearchCheck} busy={busy === "validate-all"} onClick={() => c.validateAll()}>
              一键验证全部
            </Button>
            <Button
              variant="primary"
              icon={ArrowRight}
              disabled={!configReady}
              onClick={() => c.setStep("scan")}
            >
              进入盘点
            </Button>
          </div>
        </div>
        <ol className="checklist" aria-label="必要配置验证状态">
          {validationChecklist.map(([key, no, label, detail]) => (
            <li className={`checklist__item is-${validation[key].status}`} key={key}>
              <span className="checklist__no">{no}</span>
              <div className="checklist__text">
                <strong>{label}</strong>
                <small>{detail}</small>
                <p title={validation[key].message}>{validation[key].message}</p>
              </div>
              <ValidationBadge {...validation[key]} />
            </li>
          ))}
        </ol>
      </Panel>

      <div className="split split--wide">
        <div className="stack">
          <Panel>
            <PanelHeading
              eyebrow="应用凭据"
              title="飞书应用与本机安全配置"
              copy="应用密钥只提交给本机服务，并由系统加密保存，页面不会回显。"
              icon={KeyRound}
              tools={<ValidationBadge {...validation.app} />}
            />
            <PanelBody>
              <form
                className="form-stack"
                onSubmit={(event) => {
                  event.preventDefault();
                  void c.validateApp();
                }}
              >
                <div className="field-grid">
                  <Field label="飞书应用编号" required>
                    <input
                      value={settings.app_id}
                      onChange={(event) => {
                        setSettings({ ...settings, app_id: event.target.value });
                        touchAppConfig("应用配置已修改，请重新验证");
                      }}
                      placeholder="请输入飞书应用编号"
                      autoComplete="off"
                      required
                    />
                  </Field>
                  <Field
                    label="应用密钥"
                    hint={
                      settings.app_secret_configured
                        ? "已加密保存；留空表示不更换。"
                        : "首次配置必须填写，页面不会回显。"
                    }
                    required={!settings.app_secret_configured}
                  >
                    <input
                      type="password"
                      value={secret}
                      onChange={(event) => {
                        setSecret(event.target.value);
                        touchAppConfig("应用密钥已修改，请重新验证");
                      }}
                      placeholder={settings.app_secret_configured ? "••••••••••••••••" : "输入应用密钥"}
                      autoComplete="new-password"
                      required={!settings.app_secret_configured}
                    />
                  </Field>
                </div>
                <Field label="用户授权回调地址" required hint="必须与飞书开放平台安全设置完全一致。">
                  <input
                    value={settings.redirect_uri}
                    onChange={(event) => {
                      setSettings({ ...settings, redirect_uri: event.target.value });
                      touchAppConfig("回调地址已修改，请重新验证");
                    }}
                    required
                  />
                </Field>
                <div className="scope-rack" aria-label="所需权限范围">
                  <span className="scope-rack__label">所需授权范围</span>
                  <div className="scope-rack__items">
                    {DEFAULT_SCOPES.map((scope) => (
                      <code key={scope} title={scope}>
                        <Check size={11} /> {scopeLabels[scope] ?? "飞书权限"}
                      </code>
                    ))}
                  </div>
                </div>
                <div className="button-row">
                  <Button type="submit" icon={ShieldCheck} busy={validation.app.status === "checking"}>
                    验证应用配置
                  </Button>
                  <Button
                    type="button"
                    variant="primary"
                    icon={ExternalLink}
                    busy={busy === "oauth"}
                    onClick={c.beginAuth}
                    disabled={validation.app.status !== "passed"}
                  >
                    前往飞书授权
                  </Button>
                </div>
              </form>
            </PanelBody>
          </Panel>

          <Panel>
            <PanelHeading
              eyebrow="速率控制"
              title="云盘上传速率"
              copy="使用受控并发保护本机与飞书服务；触发平台限流后自动退避并从断点继续。"
              icon={Gauge}
              tools={<ValidationBadge {...validation.throttle} />}
            />
            <PanelBody>
              <div className="rate-row">
                <Field label="每秒文件上传请求数" required hint="范围：大于 0 且不超过 5">
                  <input
                    type="number"
                    min="0.1"
                    max="5"
                    step="0.1"
                    value={settings.upload_qps}
                    onChange={(event) => {
                      setSettings({ ...settings, upload_qps: Number(event.target.value || 0) });
                      markValidation("throttle", "idle", "每秒上传请求数已修改，请重新验证");
                      invalidateDownstream("config");
                    }}
                  />
                </Field>
                <div className="note note--info">
                  <InfinityIcon size={16} />
                  <div>
                    <strong>程序不设累计上限</strong>
                    <span>飞书平台限额仍生效，并保留每秒请求节流、服务端冷却和断点恢复保护。</span>
                  </div>
                </div>
                <Button
                  icon={SearchCheck}
                  busy={validation.throttle.status === "checking"}
                  onClick={c.validateThrottle}
                >
                  验证并保存并发速率
                </Button>
              </div>
            </PanelBody>
          </Panel>
        </div>

        <div className="stack">
          <Panel tone={auth.authorized ? "green" : "amber"}>
            <PanelBody className="identity">
              <div className="identity__top">
                <span className="identity__seal">
                  {auth.authorized ? <BadgeCheck size={26} /> : <KeyRound size={24} />}
                </span>
                <ValidationBadge {...validation.oauth} />
              </div>
              <span className="eyebrow">固定操作身份</span>
              <h3>{auth.authorized ? (auth.user_name ?? "飞书用户已授权") : "等待完成飞书授权"}</h3>
              <p>
                {auth.authorized
                  ? "此身份将贯穿上传、目录创建、增量更新和远端回读。"
                  : "先验证应用配置，再在飞书页面确认所需权限。"}
              </p>
              <dl className="kv-pair">
                <div>
                  <dt>状态</dt>
                  <dd>{auth.authorized ? "已就绪" : "未就绪"}</dd>
                </div>
                <div>
                  <dt>凭据</dt>
                  <dd>系统加密保存</dd>
                </div>
              </dl>
              <Button
                icon={SearchCheck}
                busy={validation.oauth.status === "checking"}
                onClick={c.validateOauth}
              >
                验证当前身份
              </Button>
            </PanelBody>
          </Panel>

          <Panel>
            <PanelBody>
              <h3 className="card-title">本机安全边界</h3>
              <ul className="fact-list">
                <li>
                  <LockKeyhole size={16} />
                  <div>
                    <strong>不在浏览器保存令牌</strong>
                    <span>用户授权令牌与应用密钥不会写入浏览器本地存储。</span>
                  </div>
                </li>
                <li>
                  <Server size={16} />
                  <div>
                    <strong>仅监听 127.0.0.1</strong>
                    <span>迁移控制面不会暴露到局域网。</span>
                  </div>
                </li>
                <li>
                  <Database size={16} />
                  <div>
                    <strong>台账独立于同步目录</strong>
                    <span>数据库保存在本机应用数据目录并启用安全写入。</span>
                  </div>
                </li>
              </ul>
            </PanelBody>
          </Panel>
        </div>
      </div>

      <div className="split split--wide">
        <Panel>
          <PanelHeading
            eyebrow="单向迁移路径"
            title="唯一来源、唯一云盘落点与安全增量"
            copy="源目录始终只读；文件和目录按原结构直接写入目标云盘文件夹。"
            icon={Route}
          />
          <PanelBody>
            <form className="form-stack" onSubmit={c.validateAll}>
              <div className="route">
                <div className="route__node">
                  <div className="route__type">
                    <HardDrive size={19} />
                    <span>本地来源</span>
                    <em>只读</em>
                  </div>
                  <div className="route__body">
                    <Field
                      label="本地已下载目录"
                      required
                      hint="请确保目标文件显示为绿色实心勾，不能是云端占位。"
                    >
                      <input
                        value={draft.source_root}
                        onChange={(event) => {
                          setDraft({ ...draft, source_root: event.target.value });
                          markValidation("source", "idle", "本地路径已修改，请重新验证");
                          invalidateDownstream("source");
                        }}
                        placeholder="D:\迁移资料\公司文档"
                        required
                      />
                    </Field>
                    <div className="inline-verify">
                      <ValidationBadge {...validation.source} />
                      <Button
                        type="button"
                        variant="ghost"
                        icon={SearchCheck}
                        busy={validation.source.status === "checking"}
                        onClick={c.validateSource}
                      >
                        验证本地目录配置
                      </Button>
                    </div>
                  </div>
                </div>

                <div className="route__spine" aria-hidden="true">
                  <i />
                  <span>安全上传通道</span>
                  <i />
                </div>

                <div className="route__node route__node--target">
                  <div className="route__type">
                    <Cloud size={19} />
                    <span>云盘目标</span>
                    <em>最终落点</em>
                  </div>
                  <div className="route__body">
                    <Field
                      label="飞书云盘文件夹地址"
                      required
                      hint="请粘贴 /drive/folder/ 地址，并确认当前授权用户可以编辑该文件夹。"
                    >
                      <input
                        type="url"
                        value={draft.target_wiki_url}
                        onChange={(event) => {
                          setDraft({ ...draft, target_wiki_url: event.target.value });
                          markValidation("target", "idle", "云盘目标文件夹已修改，请重新验证");
                          invalidateDownstream("target");
                        }}
                        placeholder="https://example.feishu.cn/drive/folder/xxxxxxxx"
                        required
                      />
                    </Field>
                    <div className="inline-verify">
                      <ValidationBadge {...validation.target} />
                      <Button
                        type="button"
                        variant="ghost"
                        icon={SearchCheck}
                        busy={validation.target.status === "checking"}
                        onClick={c.validateTarget}
                      >
                        验证云盘文件夹
                      </Button>
                    </div>
                  </div>
                </div>
              </div>

              <div className="field-grid">
                <Field label="迁移项目名称">
                  <input
                    value={draft.name}
                    onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                  />
                </Field>
                <Field label="云盘根文件夹名称" hint="默认使用本地根目录名称。">
                  <input
                    value={draft.wrapper_name ?? ""}
                    onChange={(event) => {
                      setDraft({ ...draft, wrapper_name: event.target.value });
                      markValidation("policy", "idle", "根节点名称已修改，请重新验证");
                      invalidateDownstream("target");
                    }}
                    placeholder="本地文档迁移"
                    disabled={!draft.create_wrapper}
                  />
                </Field>
              </div>

              <Toggle
                checked={draft.create_wrapper}
                onChange={(value) => {
                  setDraft({ ...draft, create_wrapper: value });
                  markValidation("policy", "idle", "根节点策略已修改，请重新验证");
                  invalidateDownstream("target");
                }}
                label="创建同名根节点"
                description="必需。完整保留本地根目录，并避免不同来源混入同一云盘层级。"
              />

              <div className="policy-verify">
                <ShieldCheck size={17} />
                <div>
                  <strong>安全增量策略</strong>
                  <span>内容变更留历史、本地删除只报告、远端人工变化转冲突。</span>
                </div>
                <ValidationBadge {...validation.policy} />
                <Button
                  type="button"
                  variant="ghost"
                  icon={SearchCheck}
                  busy={validation.policy.status === "checking"}
                  onClick={c.validatePolicy}
                >
                  验证安全策略
                </Button>
              </div>

              <div className="button-row button-row--end">
                <Button type="submit" icon={SearchCheck} busy={busy === "validate-all"}>
                  一键验证全部
                </Button>
                <Button
                  type="button"
                  variant="primary"
                  icon={ArrowRight}
                  onClick={() => c.setStep("scan")}
                  disabled={!configReady}
                >
                  验证完成，进入盘点
                </Button>
              </div>
            </form>
          </PanelBody>
        </Panel>

        <div className="stack">
          <Panel>
            <PanelBody>
              <span className="eyebrow">传输路径</span>
              <h3 className="card-title">文件会经过哪里？</h3>
              <div className="flow-line">
                <span>
                  <HardDrive size={15} /> 本地文件
                </span>
                <ArrowRight size={13} />
                <span>
                  <Cloud size={15} /> 目标云盘文件夹
                </span>
              </div>
              <p className="card-copy">
                文件直接上传到目标目录，无需在线文档转换；所有文件保持原名称与格式。
              </p>
            </PanelBody>
          </Panel>
          <Panel tone="green">
            <PanelBody>
              <span className="eyebrow">安全增量</span>
              <h3 className="card-title">安全增量约定</h3>
              <ul className="rule-list">
                <li>
                  <b>内容未变</b>不重复上传
                </li>
                <li>
                  <b>仅移动改名</b>复用原云盘对象
                </li>
                <li>
                  <b>内容变化</b>旧版移入统一历史目录，新版保留原路径和原文件名
                </li>
                <li>
                  <b>本地删除</b>只报告，不删飞书
                </li>
              </ul>
            </PanelBody>
          </Panel>
        </div>
      </div>
    </div>
  );
}
