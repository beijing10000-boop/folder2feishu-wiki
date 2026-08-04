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

2.0 版按全新程序安装，使用独立的 `Folder2FeishuWikiNext` 目录，不读取或转换
旧版数据库、配置和凭据。旧版本可保留作回退，但启动新版前必须先关闭占用 8000
端口的旧进程。

首次安装：

目标电脑已安装 64 位 Python 3.12 时，可在 PowerShell 粘贴一条命令在线安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://raw.githubusercontent.com/beijing10000-boop/folder2feishu-wiki/v2.0.0-rc.7/deploy/Install-Online.ps1' | iex"
```

脚本会从 GitHub Release 下载指定版本、校验 SHA-256、解压并安装。目标电脑不需要
Git、GitHub CLI、Node.js 或 Go。

离线安装：

1. 解压 `Folder2Feishu-Python-*.zip`。
2. 双击 `Install.cmd`；脚本会建立独立 Python 虚拟环境并创建桌面快捷方式。
3. 以后双击桌面的 `Folder2Feishu Wiki Next`，浏览器会打开 `http://127.0.0.1:8000`。

程序仅监听本机。应用源码默认安装到：

```text
%LOCALAPPDATA%\Programs\Folder2FeishuWikiNext
```

运行数据位于：

```text
%LOCALAPPDATA%\Folder2FeishuWikiNext
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
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Folder2FeishuWikiNext\Update-Folder2Feishu.ps1" -GitHubToken "<只需仓库读取权限的Token>" -IncludePrerelease
  ```

- 离线更新：从 Release 下载新 ZIP 后运行：

  ```powershell
  powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Folder2FeishuWikiNext\Update-Folder2Feishu.ps1" -PackagePath "D:\下载\Folder2Feishu-Python-新版本.zip"
  ```

更新脚本会停止旧服务、替换应用源码、更新 Python 依赖并重新启动。SQLite 台账、
DPAPI 凭据、配置、日志和审计导出不会被覆盖。

### 2.0.0-rc.2 单应用性能优化

- 中转目录 token 按项目和每 1,000 项分片缓存，不再为每个文件重复查询云盘根目录、项目目录和分片目录。
- 新目录首次创建只调用一次创建接口；仅在响应不确定或进程恢复时查询远端，继续保留防重复机制。
- Wiki 节点创建、只读查询和迁移写入使用独立限速通道，任务轮询不再占用目录创建额度。
- 当前运行期间缓存本地相对路径到 Wiki 父节点 token，避免为每个文件重复查询 SQLite。
- 所有 `file_token`、`task_id`、`wiki_token` 的即时落库和重启恢复规则保持不变。

正式环境升级前先点击“安全暂停”，等待当前对象完成并显示暂停后，再执行更新命令。
升级不会重新盘点，也不会清空现有台账；重新打开后在“运行对账”页继续原任务。

### 2.0.0-rc.3 本地时间显示修复

- SQLite 中没有时区标记的历史 UTC 时间会先按 UTC 解析，再转换为当前 Windows/浏览器本地时间。
- 修复右侧审计时间和差异计划生成时间少 8 小时的问题；无需修改电脑时区或历史台账。

### 2.0.0-rc.4 迁移流水线性能优化

- 同一层级且互不依赖的知识库目录最多使用 4 个工作线程并行创建；下一级目录仍会等待父目录落库，目录结构不变。
- 不同文件的“上传、迁入知识库、轮询、校验”使用有界并发流水线，单个文件失败不会阻塞其他文件。
- 目录创建意图和最终 token 使用更少的 SQLite 事务；进度约每秒批量持久化一次，减少高频磁盘写入。
- Wiki 写操作按飞书官方单应用上限调整为 100 次/分钟；旧版默认 90 会自动升级，自定义较低值保持不变。
- 运行页显示并行工作线程和当前执行中项目数，暂停、停止、断点恢复和幂等防重规则保持不变。
- 相同模拟网络延迟下，目录场景由 3.556 秒降至 1.263 秒（2.82 倍），目录加文件场景由 13.188 秒降至 4.008 秒（3.29 倍）。真实速度仍受飞书接口响应和限流影响。

### 2.0.0-rc.6 大文件分片进度与页面实时日志

- 运行页直接显示每个大文件的已完成分片、总分片、已上传字节和百分比。
- 分片进度读取现有 SQLite 断点台账，刷新页面或重启服务不会丢失。
- 页面增量显示飞书上传、限流冷却、重试和错误日志，不再要求打开 PowerShell。
- 日志接口只读取新增的结构化记录并限制数量，避免大日志拖慢页面。

### 2.0.0-rc.7 全中文企业控制台

- 五个核心页面、导航、状态、表单、按钮、提示和运行日志统一为中文展示。
- 配置验证由拥挤的单行卡片改为三列分组，差异动作改为四列布局，长中文说明更易阅读。
- 统一页面宽度、卡片边界、表格列宽、运行状态区、队列密度和桌面端响应式规则。
- 飞书接口英文错误会转换为中文说明；用户自己的文件名、目录名和标准文件扩展名保持原样。

### 2.0.0-rc.5 飞书限流恢复修复

- 修复并发迁移中云盘文件改名未进入上传限速通道，导致大量 `99991400` 的问题。
- Drive 上传、分片和元数据写入共享上传 QPS；Wiki 创建、迁入和任务轮询共享每分钟额度，避免多个独立限速桶叠加超过飞书租户/应用窗口。
- 同时支持 HTTP 429 和旧式 HTTP 400 的 `99991400`，并将 `99991400/99991401/99991402/1061045` 作为显式可重试限流处理。
- 读取飞书 `Retry-After` 与 `x-ogw-ratelimit-reset` 响应头；一个线程触发限流后，整个对应请求桶都会按服务器要求冷却。
- 默认重试窗口覆盖至少一个常见的一分钟频控周期；已经落库的文件 token、分片和 Wiki 任务会在断点恢复时复用，不重复上传。

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
2. **盘点**：只读扫描本地目录，确认文件、空目录、0 字节跳过清单、OneDrive 占位和人工处理项。
3. **预检**：检查授权身份、容量、文件可读性、名称、大小、深度和单层节点数。
4. **差异计划**：查看目录树、增量动作和预计上传 API 调用量，确认后才允许写入。
   大文件的预上传、各分片和完成请求会分别计数，所以调用量可能高于文件数。
5. **运行与对账**：执行、暂停、恢复、失败重试、远端对账，并导出 CSV/JSON 审计报告。

盘点、计划生成、迁移和远端对账均为持久化后台任务：按钮点击后接口立即返回任务 ID，
页面通过增量状态查询显示阶段、进度、当前对象、成功/失败/跳过数量、耗时、预计剩余时间
和最新日志。刷新或关闭浏览器不会丢失任务；服务意外退出后，遗留任务会标记为“已中断”，
可从已落库断点继续，不会永久显示“处理中”。

本版本仅面向 Windows 桌面端，不包含移动端适配或移动端验收。

正式全量迁移前必须先用 3–10 个代表文件做小批试迁，并包含三级目录、空目录和一个大于 20 MB 的文件。

## 迁移与恢复规则

- 本地目录是独立对象；每个目录映射为一个空白 Docx 知识库节点。
- 在用户云盘根目录创建 `Folder2Feishu-Staging/<项目ID>/<分片>` 中转目录。
- 不超过 20 MB 的文件直接上传；更大的文件按 4 MB 分片，分片会话和完成序号即时落库。
- 每个请求使用非空 `parent_node`。获得 `file_token`、`task_id`、`wiki_token` 后立即写入 SQLite。
- 请求超时先查询远端状态；不会因为本地未及时落库就盲目重传。
- Drive 上传与元数据写入共享最高 4 QPS，Wiki 操作共享最高 100 次/分钟；程序不另设累计调用上限，但飞书平台的接口级 QPS、分钟和每日限额仍然生效，触发限流时按服务端窗口自动冷却重试。
- 飞书请求配置连接、读取、写入和连接池超时；临时网络错误有限次指数退避，等待期间仍可暂停或取消。
- 文件、计划动作和远端映射均分批读取和写入；界面目录树按需展开，日志只增量读取，避免一次加载数万行。
- SQLite 启用 WAL、`busy_timeout`、schema migration 和项目级执行锁；同一个项目不能被两个实例同时执行。
- 扫描不完整、预检阻断、计划未确认或发现远端人工改动时，执行器拒绝写入。

## 安全增量

- 路径和 SHA-256 都未改变：跳过。
- Windows File ID 或唯一哈希一致、但路径改变：移动/改名现有 Wiki 节点，不重新上传。
- 内容改变：完整上传并校验新文件；旧节点移入 `_Folder2Feishu_历史版本/<原目录>/<时间>`，新节点再进入原位置。
- 本地删除：只生成缺失报告，不删除或移动飞书内容。
- 飞书节点被人工改名、移动或删除：标记冲突，禁止自动覆盖。
- 0 字节文件：飞书 Drive API 不接受空文件，因此记录到盘点与审计报告后自动跳过；
  不阻断其他文件，不创建伪内容或空白 Wiki 节点。
- 超过飞书限制、名称超限、锁定文件或未下载的 OneDrive 占位文件：进入人工处理清单，不静默改名或忽略。

第二次执行在来源与远端都未变化时应为零重复上传。

## 手动无界面运行

手动无界面运行一个已配置项目：

```powershell
cd "$env:LOCALAPPDATA\Programs\Folder2FeishuWikiNext"
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
- 35,000 多文件仍受文件体积、上传带宽和飞书接口实时限流影响，工具会持续退避续跑，不按累计调用次数暂停。

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

本次后台任务、断点恢复、网络连接池和规模测试的完整诊断与回滚说明见
[`docs/PERFORMANCE_STABILITY_V2.md`](docs/PERFORMANCE_STABILITY_V2.md)。

## Clean-room 说明

项目参考了 [WZLlin/Feishu_Knowledge_Base_Migrator](https://github.com/WZLlin/Feishu_Knowledge_Base_Migrator) 的台账、重试和迁移控制思路。该参考仓库未提供许可证，因此本版本只参考公开行为与产品思路，所有实现均重新设计、独立编写，没有复制其源代码。

## 官方接口

- [OAuth v2 获取用户访问凭证](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token?lang=zh-CN)
- [OAuth v2 刷新用户访问凭证](https://open.feishu.cn/document/authentication-management/access-token/refresh-user-access-token?lang=zh-CN)
- [上传文件](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/upload_all)
- [知识库 API](https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/wiki-v2)

## License

[MIT](LICENSE)
