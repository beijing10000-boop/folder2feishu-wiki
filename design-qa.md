# Design QA — UI v4 重构

## 对比输入

- 历史实现截图（重构前）：`docs/ui-audit/after-config.jpg`、`after-inventory.jpg`、`after-run.jpg`
- 当前实现截图（重构后）：`docs/ui-audit/v4-config.jpg`、`v4-scan.jpg`、`v4-preflight.jpg`、`v4-plan.jpg`、`v4-run.jpg`
- 全部当前截图取自本次代码，浏览器视口 1600×N，`VITE_DEMO=1` 演示环境；同一提交的生产构建已独立验证通过。

## 修复的具体缺陷

| # | 问题 | 证据 | 处理 |
|---|---|---|---|
| 1 | 顶栏步骤条标签被截断成单字（“集…/只…/权…/确…/断…”） | `.step-rail` 用 `left:286px; right:420px` 硬编码挤在品牌区与状态区之间 | 改为两行顶栏：步骤条独占整行，5 等分栅格 |
| 2 | 固定底栏遮挡正文 | `.statusbar` 为 `position: fixed`，正文 padding 不足 | 底栏改为文档流内静态元素，`.app-shell` 用 flex 列布局 |
| 3 | 文字粘连：“仅监听 127.0.0.1迁移控制面不会暴露到局域网。”“安全增量策略内容变更留历史…”“安全上传通道下一步: 写入飞书云盘” | `<b>`/`<small>` 在同一行内元素中未成块 | 相关容器统一改为 `display: grid`，并移除 CSS `::after` 伪元素文案 |
| 4 | 配置页过长（1600 宽下 2397px） | 6 项校验用 3×2 的 126px 高卡片；速率面板整行只放一个数字输入 | 校验项改为紧凑清单；速率面板移入左栏并与说明、按钮同排 → 2017px |
| 5 | 运行页“结果”字段被省略号截断 | `.run-facts dd` 为 `nowrap` + ellipsis | 允许换行，仅“当前对象”路径保留单行省略 |
| 6 | 盘点页左栏大片空白 | 右栏三张卡片叠加，左栏仅目录树 | “预检交接”卡改为整幅底部条 |
| 7 | 盘点页与预检页层级上限口径不一致（页面写 50 / 2,000，预检按 15 / 1,500 阻断） | `scanner.py` 沿用 v2 知识库常量 `MAX_WIKI_LOCAL_DEPTH=49`、`MAX_WIKI_CHILDREN=2000` | 新增 `core/limits.py` 作为唯一来源，扫描与预检共用；本地目录最多 14 层，项目根目录另占 1 层，单层最多 1,500 个对象 |

## 设计系统

- `frontend/src/styles.css` 重写为单一 token 体系：4px 间距栅格、7 级字号、语义色（primary/success/warning/danger）、两级圆角与阴影。
- 数字统一 `font-variant-numeric: tabular-nums`，进度刷新时不再抖动。
- 断点：1400 / 1200 / 900 / 640，并支持 `prefers-reduced-motion`。
- 面板、按钮、徽标、表格、空状态、进度条全部收敛为共享控件，配置页/盘点/预检/差异计划/运行页复用同一套。

## 自动化校验

| 检查 | 结果 |
|---|---|
| `ruff check folder2feishu tests` | 通过 |
| `ruff format --check folder2feishu tests` | 通过 |
| `mypy folder2feishu` | 通过（35 个源文件无错误） |
| `pytest` | 167 passed, 1 skipped |
| 前端 ESLint | 通过 |
| `vitest run` | 13 passed |
| `vite build` | 通过（JS 235.69 kB / gzip 73.46 kB；CSS 38.86 kB / gzip 6.85 kB） |
| 配额文件并发写入压力回归 | 连续 30 轮通过，无临时文件名冲突 |
| 35,000 文件 + 5,051 目录规模验收 | 通过；整轮 22.1 秒，扫描阶段低于 180 秒门槛 |

规模验收使用与正式桌面程序一致的首轮快速盘点策略：先持久化文件标识、大小、纳秒修改时间和相对路径，35,000 个新文件的 SHA-256 均延后到真正准备上传时按需计算。

## 结论

`passed` — 五个步骤页均已按当前样式表重新截图并逐项核对，上述 7 项缺陷已修复，回归全绿。
