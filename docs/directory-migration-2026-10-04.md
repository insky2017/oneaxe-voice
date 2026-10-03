# OneAxe Voice 目录整合

迁移于 2026-10-03 开始、2026-10-04 完成。用户确认使用现有大写目录 `~/work/touzi/OneAxe`。本次只调整目录、环境前缀、部署配置和文档；服务端业务代码、接口、模型算法与两路限制不变。

## 日常入口

```text
~/work/touzi/OneAxe/oneaxe-voice/
├── README.md                     # 总入口
├── server/                       # 服务端独立 Git，正式服务从这里运行
│   ├── docs/                     # 架构、接口和验收记录
│   ├── runtime/                  # 原配置、凭据、证书与运行数据
│   ├── .venv/
│   └── .venv-stream/
├── clients/linux/                # 通用 Linux 客户端独立 Git
├── .worktrees/server/capacity/    # 保留的容量研究
└── .archive/                     # 私有迁移备份与旧实验资料
```

总目录不新建 Git。服务端保留 `master`、全部历史分支和既有远端，Linux 客户端保留 `main` 与独立历史；原客户端没有配置远端，本次不新增。父项目仅通过本地 `info/exclude` 忽略整个容器目录，避免将嵌套代码、环境或私有资料收入父仓库。

共享模型仍在 `~/tools/models`，VPlus 的代码、环境、服务和模型权重未迁移。Android 工程不在本次目录整理范围。

## 旧目录如何处理

| 原目录（均在 `~/tools/`） | 处理 |
| --- | --- |
| `oneaxe-voice` | 迁入 `server/` |
| `oneaxe-voice-linux` | 迁入 `clients/linux/` |
| `oneaxe-voice-capacity` | 用 `git worktree move` 迁入 `.worktrees/server/capacity/`，保留 5 个未合入 `master` 的研究提交 |
| `oneaxe-voice-concurrency` | 确认已合并、无未提交变更；保存 `runtime/` 与 `work/` 后收回工作区 |
| `oneaxe-voice-passive` | 同上 |
| `oneaxe-voice-qwen-passive` | 同上 |
| `oneaxe-voice-qwen-v1` | 同上 |

移除的是已完成开发工作区，历史分支仍在。迁移前两个仓库还各保存了一份 Git bundle。私有备份位于 `.archive/migration-2026-10-03/`，含旧实验资料、原服务配置、路径清单和环境重定位记录，不纳入 Git。

两套 `.venv*` 实际是 Conda prefix 环境。使用独立临时工具中的 `conda-pack 0.9.2`，以未压缩 tar 和最终 `dest_prefix` 重定位后再放入正式目录。严格预检没有忽略缺失文件；复制出的普通文件没有与源环境或 Conda 缓存共享 inode。没有重新下载 CUDA / 模型依赖，没有手工批量替换共享二进制文件。

正式切换前检查本机和远端会话是否空闲；只停止并恢复 Voice 的三个用户服务。主仓库移动后执行 `git worktree repair`，容量研究的环境链接改为指向新正式目录。

## 部署路径

- API、desktop、tray 的 user systemd 单元更新 `WorkingDirectory`、`ExecStart` 及适用的 runtime 环境变量。
- F8 更新为新 `server/bin/oneaxe-voice toggle`。F9 和 Linux 桌面入口仍使用 `~/.local/bin/oneaxe-voice-linux`。
- `runtime/mobile-listener.json` 只更新证书和私钥文件的位置。凭据、设备登记、证书和私钥原字节保留，无须重新配对或签证书。
- Conda 环境登记更新为新路径。Python、pip、OpenSSL 默认 CA 位置均在新环境内。
- e15l 的客户端源码与构建副本也迁入相同布局；它原本没有 `.git`，本次保持构建副本身份。两机安装 binary、用户配置和 F9 不重装。
- 更新 [总入口](../../README.md)、`~/docs/oneaxe-voice/README.md` 简要入口和当前复验命令；历史测量 JSON 与历史部署路径保留原始内容。

## 验证

| 检查 | 结果 |
| --- | --- |
| 目录与 Git | 两个独立仓库无 superproject；容量研究的 5 个独有提交保留；父仓库未跟踪容器目录；旧 `~/tools/oneaxe-voice*` 已不存在 |
| 环境 | 两套 Python prefix、pip 入口和 OpenSSL 默认 CA 均在新目录，CUDA 可用；两套环境均通过真实 TLS 证书验证，未认证 capabilities 返回预期 401 |
| 服务与设置 | 三个 Voice 用户服务从新路径运行，Qwen 流式已就绪于 `cuda:0`；F8 命令指新位置，F9 安装入口保持；凭据、证书和私钥字节校验一致，桌面配置保留 |
| 既有自动化回归 | 214 项通过，10.220 秒；这是无 GPU 的逻辑回归，不替代真实识别 |
| 本机与 e15l 客户端 | 已安装版本的 HTTPS 鉴权与 capabilities 查询均通过，服务器返回 `qwen-stream`、`ready=true`、`can_start=true` |
| Qwen 双路真实识别 | 本机 50 秒 + e15l 30 秒，观测重叠 29.9463 秒；本机 800000、远端 480000 样本全部处理，固定文字包含各自预期关键词、未检出另一通道关键词 |
| 短测速度与流控 | PC 已处理音频进度时延 P95 2.0814 秒；e15l 首次固定文字 5.218 秒，未发送缓冲峰值 5120 样本，发送减处理样本峰值 34560，均在原门槛内 |
| 隔离 | VPlus PID 保持；模型权重位置不变；客户端安装 binary 哈希两机保持一致，无须重新配对或安装 |

两机安装 binary SHA-256：`67fdeb2fd7b7ff222521d501f12cfc59e241d91e0953a148b0958db4bf1047fc`。目录切换和第一次识别验证均曾被实时忙状态检查阻止；等待实际空闲后继续，没有取消用户会话。

短测通过客户端既有 `tests/dual_stream.py` 的 `run()` 复用采集、流控和隔离检查；它的命令行长测入口仍要求至少 600 秒。本次显式使用 30 秒迁移冒烟验证，不作为新的容量或长期稳定性结论。仅使用既有受控音频，不操作用户输入框。F8/F9 本轮核对配置和运行入口，没有重新按键验收；未重测 R2T2 真模型、tmux、桌面粘贴、Android 真机或 600 秒容量长测。

完整数值与检查结果见 [迁移证据](evidence/directory-migration-2026-10-04.json)，不含凭据、音频或转写正文。环境与真实识别验证通过后，已删除两套冗余的旧环境备份和临时 tar；Git、配置与研究资料的私有备份保留。

## 后续维护

服务端操作先 `cd ~/work/touzi/OneAxe/oneaxe-voice/server`，Linux 构建先进入同级 `clients/linux/`。新服务开发工作区统一放 `.worktrees/server/<任务名>`；任务合并后先保存必要研究资料，再用 Git 回收。不要再次在 `~/tools/` 创建并列的 Voice 开发目录。

恢复旧部署时需要同时恢复仓库路径、环境前缀、user systemd 单元、F8、TLS 路径和 worktree 元数据；仅移动源码不能完成回滚。现有证书、凭据和共享模型无需重新生成。
