# 独立工作目录与启动

共同基线：`351687d`（双方已有改动的混合快照，不是单方功能提交）。

开发时使用各自 Git worktree，Codex 的界面工作在 `codex/ui-refinement`。
Claude 的目录由 Claude 管理；分支名改变后，启动器会按 `claude/` 前缀重新发现。
不要在另一个开发者的目录中修改、检出、重置或提交文件。

## 启动命令

```text
HMI                       主工作目录
HMI --tree codex          codex/ 下唯一已检出的工作分支
HMI --tree claude         claude/ 下唯一已检出的工作分支
HMI --tree codex/ui-refinement
HMI --list               显示实际目录与分支
HMI --tree codex --check  只检查 Python / C++ 核路径，不创建主界面
```

同一前缀存在多个已检出的分支时，指定完整分支名。启动器不会执行 checkout、merge 或 reset。
`tools/hmi_launcher.py` 是版本控制中的启动器源码；安装时复制到用户的
`.local/bin/hmi_launcher.pyw`，已有 HMI.exe 负责转发参数，无需重新编译 EXE。
可设置 `HMI_BASE_PROJECT` 覆盖默认的 `~/AI工作文件夹/上位机` 仓库位置。

## 运行环境与配置

优先使用工作目录中的 `.venv`；没有时使用启动器当前 Python。
Git worktree 本身不隔离全局 Python 安装，安装/升级依赖或重编译安装 C++ 前先协调。
已编译 C++ 核可以复制到各自的 `native_core_runtime/` 固定当前版本，避免全局模块变化。
本地配置、密钥、运行日志、试验数据及编译产物不提交 Git。
Codex 本轮复制了现有 C++ 产物和 AI 配置，未重编译或修改全局 Python 安装。

## 协作边界

- Codex 本轮负责静态结构背景、绘图区底色、右侧诊断助手、启动器。
- 使用说明书及其页面留给 Claude，合并前协调 `main_window.py` 等交叉改动。
- 只做离线开发与测试。同一时间只能由一方连接控制板。
- 烧录固件和真机试验由用户确认并操作；AI 工具不自行连接硬件。
- 功能在各自分支提交，确认后再合并至主目录。
