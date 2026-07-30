# Folder2Feishu Wiki

Windows 本地目录到飞书知识库的原目录迁移工具。

它只读扫描 OneDrive 已下载到本机的目录，把根目录、子目录、空目录、原文件名和原文件格式保留到飞书知识库。飞书云盘仅作为 API 必需的临时中转；文件迁入成功后不留在中转目录。

```text
D:\Team FabDazzle - 文档
├─ Apparel
│  ├─ Reports
│  │  └─ weekly.xlsx
│  └─ brief.docx
└─ root.pdf

              ↓

飞书知识库目标父节点
└─ Team FabDazzle - 文档
   ├─ Apparel
   │  ├─ Reports
   │  │  └─ weekly.xlsx
   │  └─ brief.docx
   └─ root.pdf
```

不使用 Claude，不做 AI 分类，不重组目录，也不把 Office/PDF 转换成飞书在线文档。

## 下载、安装与更新

从 [Releases](https://github.com/beijing10000-boop/folder2feishu-wiki/releases) 下载：

- `Folder2Feishu-Python-*.zip`：Python 源码发布包。
- `SHA256SUMS.txt`：核对发布包完整性。

本项目不再提供或使用 PyInstaller EXE、Inno Setup 安装器。目标电脑只需安装
**Python 3.12（64 位）**，不需要 Git、GitHub CLI、Node.js 或 Go。

首次安装：

1. 解压 `Folder2Feishu-Python-*.zip`。
2. 双击 `Install.cmd`；脚本会建立独立 Python 虚拟环境并创建桌面快捷方式。
3. 以后双击桌面的 `Folder2Feishu Wiki`，浏览器会打开 `http://127.0.0.1:8000`。

程序仅监听本机。应用源码默认安装到：

```text
%LOCALAPPDATA%\Programs\Folder2FeishuWiki
```

运行数据位于：

```text
%LOCALAPPDATA%\Folder2FeishuWiki
├─ ledger.sqlite3
├─ settings.json
├─ credentials.bin
├─ quota.json
├─ logs\
└─ exports\
```

这些文件禁止放进 OneDrive 同步目录。

以后更新不需要重装 EXE：

- 私有 GitHub 仓库在线更新：

  ```powershell
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Folder2FeishuWiki\Update-Folder2Feishu.ps1" -GitHubToken "<只需仓库读取权限的Token>" -IncludePrerelease
  ```

- 离线更新：从 Release 下载新 ZIP 后运行：

  ```powershell
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Folder2FeishuWiki\Update-Folder2Feishu.ps1" -PackagePath "D:\下载\Folder2Feishu-Python-新版本.zip"
  ```

更新脚本会停止旧服务、替换应用源码、更新 Python 依赖并重新启动。SQLite 台账、
DPAPI 凭据、配置、日志和审计导出不会被覆盖。

## 飞书应用配置

在飞书开放平台创建企业自建应用，申请并发布以下 OAuth 权限：

```text
offline_access
drive:drive
drive:file:upload
wiki:wiki
drive:quota_detail:read_one
contact:user.employee_id:readonly
```

`drive:file:upload` 同时覆盖上传后恢复原文件名所用的文件标题更新接口。最后一项是获取当前授权用户 `user_id` 的字段权限；飞书容量接口需要用该 ID 查询当前用户容量。早期原型中写过 `auth:user.id:read`，该名称并不适用于当前服务端用户信息接口。

在安全设置中添加完全一致的重定向地址：

```text
http://localhost:8000/oauth/callback
```

授权用户还必须是目标知识库成员，并对目标父节点拥有容器编辑权限。上传、迁入、校验和后续增量始终使用同一个 OAuth 用户身份。

App Secret、access token 和 refresh token 不会发送到前端或写入 SQLite；Windows 正式运行使用当前用户 DPAPI 加密保存。OAuth 使用 v2、PKCE、一次性 `state` 和 refresh token 轮换。

## 桌面端操作流程

程序打开后默认进入“配置”首页。飞书应用、OAuth、上传限流、本地来源、知识库目标、
安全增量共六组配置都在这一页完成。App、OAuth、本地根目录和 Wiki 目标旁
均有独立验证按钮；“一键验证全部”会按顺序调用相同的真实后端验证。保存成功不等于
验证通过，全部必要项重新验证通过后才会开放盘点。

1. **配置**：填写六组设置，保存 App Secret，完成用户 OAuth，并逐项验证。
2. **盘点**：只读扫描本地目录，确认文件、空目录、0 字节占位策略、OneDrive 占位和人工处理项。
3. **预检**：检查授权身份、容量、文件可读性、名称、大小、深度和单层节点数。
4. **差异计划**：查看目录树、增量动作、预计上传调用量和预计迁移天数，确认后才允许写入。
5. **运行与对账**：执行、暂停、恢复、失败重试、远端对账，并导出 CSV/JSON 审计报告。

本版本仅面向 Windows 桌面端，不包含移动端适配或移动端验收。

正式全量迁移前必须先用 3–10 个代表文件做小批试迁，并包含三级目录、空目录和一个大于 20 MB 的文件。

## 迁移与恢复规则

- 本地目录是独立对象；每个目录映射为一个空白 Docx 知识库节点。
- 在用户云盘根目录创建 `Folder2Feishu-Staging/<项目ID>/<分片>` 中转目录。
- 不超过 20 MB 的文件直接上传；更大的文件按 4 MB 分片，分片会话和完成序号即时落库。
- 每个请求使用非空 `parent_node`。获得 `file_token`、`task_id`、`wiki_token` 后立即写入 SQLite。
- 请求超时先查询远端状态；不会因为本地未及时落库就盲目重传。
- 上传队列最高 4 QPS，Wiki 操作最高 90 次/分钟，每日上传预算为 9,500 次；达到预算自动暂停，下一次运行继续。
- SQLite 启用 WAL、`busy_timeout`、schema migration 和项目级执行锁；同一个项目不能被两个实例同时执行。
- 扫描不完整、预检阻断、计划未确认或发现远端人工改动时，执行器拒绝写入。

## 安全增量

- 路径和 SHA-256 都未改变：跳过。
- Windows File ID 或唯一哈希一致、但路径改变：移动/改名现有 Wiki 节点，不重新上传。
- 内容改变：完整上传并校验新文件；旧节点移入 `_Folder2Feishu_历史版本/<原目录>/<时间>`，新节点再进入原位置。
- 本地删除：只生成缺失报告，不删除或移动飞书内容。
- 飞书节点被人工改名、移动或删除：标记冲突，禁止自动覆盖。
- 0 字节文件：飞书 Drive API 不接受空文件，因此在原目录位置创建同名空白
  Wiki Docx 节点，并在台账标记 `empty_wiki_docx`；不会伪造 1 字节文件内容。
- 超过飞书限制、名称超限、锁定文件或未下载的 OneDrive 占位文件：进入人工处理清单，不静默改名或忽略。

第二次执行在来源与远端都未变化时应为零重复上传。

## 手动无界面运行

手动无界面运行一个已配置项目：

```powershell
cd "$env:LOCALAPPDATA\Programs\Folder2FeishuWiki"
.\.venv\Scripts\python.exe -m folder2feishu --run-project <项目ID>
```

产品不创建 Windows 计划任务，也不提供自动定时迁移。所有盘点和迁移都由用户
在界面中明确启动，或显式执行上述命令。

## 明确边界

- 来源只处理本地文件和目录，不直接连接 SharePoint Graph。
- 不修改或删除本地 OneDrive 文件。
- 不迁移 SharePoint/NTFS 权限、版本历史、评论、共享链接或站点页面。
- 不自动删除飞书节点。
- Word、Excel、PowerPoint、PDF 等保留原格式，不转换成飞书原生文档。
- 35,000 多文件受飞书租户配额和每日调用预算影响，通常需要跨多个自然日续跑。

## 开发、测试和发布

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check folder2feishu tests
.\.venv\Scripts\python.exe -m mypy folder2feishu
.\.venv\Scripts\python.exe -m pytest

cd frontend
npm ci
npm run lint
npm test
npm run build
cd ..

.\packaging\build-python-release.ps1
```

完整性能测试默认跳过，可显式运行：

```powershell
$env:FOLDER2FEISHU_RUN_SCALE_TEST = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_core_v2_scale.py
```

发布工作流会在 Windows 上运行后端检查、前端构建、Python 源码包安装测试和网页
健康检查。发布产物不包含应用 EXE，也不使用 PyInstaller 或 Inno Setup。

## Clean-room 说明

项目参考了 [WZLlin/Feishu_Knowledge_Base_Migrator](https://github.com/WZLlin/Feishu_Knowledge_Base_Migrator) 的台账、重试和迁移控制思路。该参考仓库未提供许可证，因此本版本只参考公开行为与产品思路，所有实现均重新设计、独立编写，没有复制其源代码。

## 官方接口

- [OAuth v2 获取用户访问凭证](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token?lang=zh-CN)
- [OAuth v2 刷新用户访问凭证](https://open.feishu.cn/document/authentication-management/access-token/refresh-user-access-token?lang=zh-CN)
- [上传文件](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/upload_all)
- [知识库 API](https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/wiki-v2)

## License

[MIT](LICENSE)
