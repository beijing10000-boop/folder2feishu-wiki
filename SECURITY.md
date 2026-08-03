# 安全说明

Folder2Feishu Wiki 是只在 Windows 本机运行的迁移工具。它会获得飞书用户授权，
因此按“本机高权限工具”处理，而不是普通公开网站。

## 安全边界

- HTTP 服务只监听 `127.0.0.1`，不支持局域网或公网部署。
- App Secret、access token 和 refresh token 不写入 SQLite、JSON、日志或前端。
- Windows 正式运行只允许使用当前用户 DPAPI 加密凭据。
- OAuth 使用 PKCE、一次性 `state` 和 v2 JSON token 接口。
- 所有状态变更 API 校验同源请求和启动期 CSRF token。
- 前端不使用 `localStorage`、`sessionStorage` 或 URL 保存任何令牌。
- Content Security Policy 禁止第三方脚本、对象和 iframe。
- 源目录只读；应用不会修改或删除本地文件。
- 本地删除只生成审计事件，不自动删除飞书节点。
- 日志和 CSV 导出会脱敏；CSV 中可能触发 Excel 公式的内容会被转义。

## 凭据丢失或授权撤销

在控制台执行“退出飞书授权”，并在飞书应用管理后台撤销用户授权。随后删除：

```text
%LOCALAPPDATA%\Folder2FeishuWikiNext\credentials.bin
```

删除凭据不会删除迁移台账或飞书文件。重新授权后可以继续使用已有 token 映射。

## 漏洞报告

请不要在公开 Issue 中提交 App Secret、token、知识库 URL 或包含业务文件名的日志。
通过仓库的 Security 页面私下报告，并提供版本号、复现步骤和已脱敏日志。
