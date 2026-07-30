# Folder2Feishu Wiki

把 Windows 本地目录按原始层级迁移到飞书知识库。它只解决一件事：

```text
本地根目录
├─ Apparel
│  ├─ Reports
│  │  └─ weekly.xlsx
│  └─ brief.docx
└─ root.pdf

              ↓

飞书知识库目标节点
└─ 本地根目录
   ├─ Apparel
   │  ├─ Reports
   │  │  └─ weekly.xlsx
   │  └─ brief.docx
   └─ root.pdf
```

不做 AI 分类，不把文件重新放进“制度、项目、会议”等模板目录。

## 主要能力

- 递归扫描本地目录，保留原始相对路径。
- 将每个本地文件夹创建为一个空白 Docx 知识库节点，用作目录入口。
- 文件以 OAuth 用户身份上传，再挂载到对应 Wiki 父节点。
- 20 MB 以内直接上传；超过 20 MB 自动分片上传。
- SQLite 台账记录目录节点、文件 token、Wiki token、SHA-256、状态和错误。
- 可暂停、继续、停止；重启后已成功项自动跳过。
- 失败项重新排队；中途断网或限流后无需从头开始。
- 已成功迁移后发生变化的本地文件只标记为 `changed`，不会静默覆盖飞书内容。
- 本地 Web 控制台仅监听 `127.0.0.1`。

## 安装要求

- Windows 10/11
- Python 3.11 或更高版本，推荐 Python 3.12
- 飞书企业自建应用
- 执行 OAuth 的飞书用户必须能编辑目标知识库节点
- OneDrive 文件应尽量先完成本地下载，显示绿色对勾

## 飞书应用配置

在飞书开放平台创建企业自建应用，并完成以下设置：

1. 权限管理中开通并发布：
   - `wiki:wiki`：查看、编辑和管理知识库
   - `drive:drive`：查看、评论、编辑和管理云空间中的文件
   - `offline_access`：离线访问已授权数据
2. 安全设置中添加 OAuth 重定向地址：

   ```text
   http://localhost:8765/oauth/callback
   ```

3. 发布应用版本。
4. 确保进行 OAuth 授权的用户是目标知识库成员，并且对目标父节点具有编辑权限。

本工具使用 `user_access_token` 上传，因此不需要把个人云盘文件夹分享给机器人群。

## Windows 使用方法

1. 下载或克隆本仓库。
2. 双击 `install.bat`。
3. 双击 `start.bat`。
4. 浏览器打开 `http://localhost:8765`。
5. 填写 App ID、App Secret 和回调地址，保存。
6. 点击“前往飞书授权”。
7. 填写本地根目录，例如：

   ```text
   D:\TechStyle\Team FabDazzle - 文档
   ```

8. 填写目标知识库父节点链接，例如：

   ```text
   https://example.feishu.cn/wiki/XdhSwsU7PiDZSak2WoIc2Qb8nDc
   ```

9. 扫描本地目录并检查文件数、目录数、OneDrive 离线文件数。
10. 验证目标知识库。
11. 建议先用小目录试迁，确认目录、文件名和权限后再迁移全部内容。
12. 点击“开始 / 继续迁移”。

## 大规模迁移说明

飞书“上传文件”接口单次上传上限为 20 MB、频率上限为 5 QPS，并有每天 10,000 次调用的限制。35,000 个文件通常不能在一天内全部完成，应按天续跑。达到限额后：

1. 等下一配额周期；
2. 点击“失败项重新排队”；
3. 点击“开始 / 继续迁移”。

已成功文件不会重复上传。

迁移期间：

- 禁止电脑自动睡眠。
- `start.bat` 窗口需要保持运行；浏览器可以关闭。
- 不要移动、重命名或删除本地根目录。
- 不要同时运行多个迁移实例。
- 定期检查“异常与人工处理”列表。

## 幂等与同名节点

- 本地 SQLite 台账是主要的断点依据。
- 创建目录前会读取目标父节点的直接子节点；存在一个同名节点时会复用。
- 同一父节点存在多个同名节点时，工具会停止该目录，避免误挂。
- 建议使用空白、专用的目标知识库父节点。
- 切换目标节点前应完成当前迁移或重置本地台账。

## 安全边界

- `.env`、OAuth token、SQLite 台账和日志位于本地，均被 `.gitignore` 排除。
- API 不向浏览器返回 App Secret 或 OAuth token。
- 工具不会删除本地源文件。
- 工具不会删除现有 Wiki 节点。
- 默认忽略 `desktop.ini`、`Thumbs.db`、`.DS_Store`、Office `~$` 临时文件和符号链接。
- 空文件无法通过飞书文件上传接口上传，会进入失败清单。
- 本工具迁移原始文件，不会把 Office/PDF 自动转换为飞书原生文档。
- SharePoint/NTFS 权限、版本历史、评论和共享链接不在本工具范围内。

## 开发与测试

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## 参考

- [飞书：上传文件](https://open.feishu.cn/document/server-docs/docs/drive-v1/upload/upload_all)
- [飞书：获取知识空间节点信息](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)
- [飞书：获取知识空间子节点列表](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/list)
- [飞书：获取 OAuth 授权码](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)
- 设计参考：[WZLlin/Feishu_Knowledge_Base_Migrator](https://github.com/WZLlin/Feishu_Knowledge_Base_Migrator)

## License

MIT

