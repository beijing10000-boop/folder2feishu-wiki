# Folder2Feishu Drive V4

把 Windows 本地目录按原层级直接迁移到飞书云盘。

V4 延续云盘直传架构，并兼容 V3 的配置、数据库、断点台账和项目运行目录。文件不经过知识库迁入，目录直接对应云盘文件夹，Word、Excel、PPTX、PDF、图片、视频等均保留原格式。

## 主要能力

- 只读扫描本地文件夹，不修改 OneDrive 或本地文件。
- 保留根目录、子目录、空目录、原文件名和原格式。
- 后台迁移、实时进度、实时日志、安全暂停、断点恢复和失败项重试。
- SHA-256 与文件标识驱动的安全增量，第二次运行不会重复上传未变化文件。
- 本地删除只报告，不自动删除飞书云盘内容。
- 飞书对象被人工移动、改名或删除时标记冲突，不自动覆盖。
- SQLite 台账、日志和凭据均保存在新的独立运行目录。
- 在首页从统一数据根目录选择或新建项目；不同项目的台账、凭据和任务完全隔离。

## 系统要求

- Windows 10/11 或 Windows Server 2019/2022。
- PowerShell 5.1 或更高版本。
- 能访问 GitHub Releases、`open.feishu.cn` 和所在飞书租户。
- OneDrive 可以尚未完成全部下载；已下载文件正常迁移，云端占位文件会延迟到后续重新盘点后补传。

目标电脑不需要预装 Git、GitHub CLI 或 Python；在线安装包会携带独立 Python 环境。

## 一条命令在线安装

以普通 PowerShell 打开，执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm 'https://raw.githubusercontent.com/beijing10000-boop/folder2feishu-wiki/v4.1.0/deploy/Install-Online.ps1' | iex"
```

安装完成后，从开始菜单打开“Folder2Feishu 云盘迁移”，浏览器会访问 `http://127.0.0.1:8000`。

## 一条命令升级

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\Folder2FeishuDrive\App\Update-Folder2Feishu.ps1"
```

从 V3 升级到 V4 会保留配置、凭据、项目数据库、断点台账和日志。v2 知识库版位于不同目录，不会被覆盖。

## 运行目录

- 程序：`D:\Folder2FeishuDrive\App`
- 项目数据根目录：`D:\Folder2FeishuDrive\Projects`
- 各项目配置、数据库和凭据：`D:\Folder2FeishuDrive\Projects\<项目数据文件夹>`
- 服务日志：`D:\Folder2FeishuDrive\Projects\.service\logs\folder2feishu.log`

程序会读取 `Projects` 下的一级子文件夹作为可选项目。新建项目时，会在该目录下创建同名文件夹。切换项目不会复制、删除或合并任何数据；当前项目仍有运行、排队或暂停任务时，程序会阻止切换。

从旧版升级时，安装程序会先停止服务，将 `%LOCALAPPDATA%\Folder2FeishuDrive`
完整复制到 D 盘并逐文件校验 SHA-256；验证通过后才会删除 C 盘旧数据。

## 飞书应用配置

在飞书开放平台为应用开通并发布以下用户权限：

```text
offline_access
drive:drive
drive:file:upload
drive:quota_detail:read_one
contact:user.employee_id:readonly
```

回调地址必须与程序配置页一致，默认：

```text
http://127.0.0.1:8000/oauth/callback
```

授权用户必须能够访问并编辑目标飞书云盘文件夹。程序始终使用同一个 OAuth 用户完成目录创建、上传、增量更新和远端核验。

## 使用流程

1. 数据项目：从 `D:\Folder2FeishuDrive\Projects` 选择已有项目，或新建同名数据文件夹。
2. 配置：填写应用凭据、完成用户授权、设置上传速率、选择本地目录和目标云盘文件夹。
3. 盘点：只读扫描文件、目录、大小、时间和文件标识；仅在需要上传或元数据变化时计算 SHA-256。
4. 预检：检查授权、云盘容量、目录深度、单层对象数、占位文件和名称限制。
5. 差异计划：确认每一项创建、上传、移动、改名、换版、跳过或冲突动作。
6. 运行对账：后台执行并显示进度、当前对象、心跳、预计剩余时间、分片进度与实时日志。

若盘点中仍有 OneDrive 云端占位文件，预检只显示黄色提醒，不阻断其他文件。待 OneDrive 下载完成后，请依次执行“重新只读盘点 → 预检 → 生成新的差异计划 → 开始迁移”；不要使用“重试失败项”，因为占位文件属于本轮延迟项而不是失败项。

迁移执行器使用有界滑动队列：任一文件完成后立即补入下一项，不会因为同批次中仍有超大文件而让其他工作线程空等。多个大文件可以同时占用不同工作线程，但全部云盘写请求仍共同遵守每秒 5 次的安全上限，并在飞书返回限流时自动冷却。V4 修复了退避等待、限速锁竞争和工作租约释放问题，临时网络故障不会触发无间隔重试。

飞书云盘完整路径最多 15 层。程序会为每个项目建立一层独立根目录，因此本地目录最多允许 14 层；盘点阶段会直接指出超限路径，不再等到预检阶段才阻断。

正式全量前请先用 3–10 个代表性文件做试迁，其中包含三级目录和一个大于 20 MB 的文件。

## 开发与测试

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
python -m pytest -q
npm --prefix frontend test -- --run
python -m folder2feishu --no-browser
```

## 当前边界

- 来源仅处理本地文件和目录，不直接连接 Microsoft Graph 或 SharePoint。
- 不迁移 SharePoint 权限、版本历史、评论、共享链接和站点页面。
- 0 字节文件因飞书云盘不接受空文件而记录并跳过，不阻断其他项目。
- 飞书平台的接口限流和租户容量仍然生效；程序会自动退避，不能通过增加应用绕过。

## 安全

- 服务只监听 `127.0.0.1`。
- 应用密钥和刷新令牌使用 Windows 本机加密存储。
- 浏览器不保存飞书令牌。
- 所有远端写入均在用户确认差异计划后执行。

详细安全问题请参阅 [SECURITY.md](SECURITY.md)。
