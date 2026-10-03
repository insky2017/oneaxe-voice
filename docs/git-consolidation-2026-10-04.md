# OneAxe Voice Git 整合记录

## 目标与授权

用户于 2026-10-04 批准将服务端、Linux 和 Android 放入原有 `git@github.com:insky2017/oneaxe-voice.git` 仓库，根目录为 `~/work/touzi/OneAxe/oneaxe-voice`，沿用 `master` 主分支。三部分完整历史以原提交保留，客户端通过非 squash 合并导入；Linux 与 Android 继续独立构建、安装和调试，子目录不再保有独立 `.git`。

本记录接续[此前目录迁移](../server/docs/directory-migration-2026-10-04.md)。此前记录中的“两个独立仓库”“总目录不新建 Git”描述的是那次迁移完成时的结构；当前结构和 Git 验收以本记录及[根 README](../README.md)为准。旧记录的运行测量和验收数据保留其原版本含义。

整合前，产品总目录只是容器，没有自己的 `.git`；在该目录执行 Git 会向上找到父目录 OneAxe 的仓库。原 Voice Git 位于 `server/`，其历史一直保留。此次把该 Git 延续到产品根目录，并接入客户端历史；父目录仓库不作为 Voice 的 Git 根。

```text
oneaxe-voice/                     # 原服务端 Git 延续至此
├── README.md
├── AGENTS.md
├── docs/                        # 公共整合记录
├── server/                      # Python/GPU 服务；正式运行路径不变
├── clients/
│   ├── linux/                   # Rust/GTK3；独立构建
│   └── android/                 # Gradle wrapper；独立 APK
├── .archive/                    # 本地备份和旧资料，不纳入 Git
└── .worktrees/server/capacity/   # 保留旧容量研究 worktree
```

此次整合只调整 Git 归属、Android 源码组织和当前文档入口。服务端源码、API、两路限制、共享模型、运行环境、user systemd、F8/F9、设备凭据和已安装客户端沿用当前部署；这些运行对象不随仓库切换重新安装或重启。

## 整合流程

1. 冻结三个来源的提交、分支与工作区状态，备份各自 Git 和必要本地资料；私有文件不导入。
2. 从原服务端主线建立临时整合 worktree，将被跟踪的服务端文件放入 `server/`，保留此前原提交。
3. 将 Linux 来源历史作为合并父链保留，最终文件树导入 `clients/linux/`；以同样方式导入 Android 至 `clients/android/`。禁止 squash 或只复制快照后丢弃父链。
4. 统一根 README、公共 AGENTS 和当前工程入口；历史迁移、验证记录保留原证据，只补后续整合链接。
5. 静态核对来源历史可达性、文件树、唯一 Git 根、忽略规则、相对链接、独立构建入口及旧容量研究；如需构建或运行验证，另按用户当前会话与授权安排。
6. 保存旧仓库元数据与恢复入口，把原服务端 Git 延续到产品根目录，保留已有 `server/` 运行对象并收回客户端独立 `.git`；修复旧容量研究的 Git 元数据而不改变其研究内容。
7. 在根目录提交整合文档并快进原主分支，按已批准范围推送到原远端；核对远端提交和本地根目录一致，再补写最终验收结果。

## 临时整合基线

本节为临时仓库中的已准备结构，不代表正式根目录切换或远端推送已完成：

| 结构提交 | 保留关系 |
| --- | --- |
| `e73fb35` | 服务端来源 `4a67e4d` 延续，当前文件树移入 `server/` |
| `d225b7e` | 第一父为 `e73fb35`，第二父为 Linux 来源 `64dcb6b`，导入 `clients/linux/` |
| `21f1cec` | 第一父为 `d225b7e`，第二父为 Android 来源 `a78f4ab`，导入 `clients/android/` |

历史导入核对覆盖各来源所有原有分支和标签的可达提交。三个用于整合的来源 tip 均保留为主线祖先；服务端未合入的容量研究继续由原研究分支保留，不混入正式主线。Linux 归档 ref 为 `archive/linux/main`；Android 保留 `archive/android/codex/standalone-voice-android` 和原注解标签 `archive/android/import/pocket-3f60ab3`。Linux 原 `.git` 中不可达的试验提交随私有整库备份保留，不额外推送；最终来源计数与核验结果在下方验收记录补充。

旧容量研究仍保留未合入服务端主线的研究提交和工作区；不因 Git 整合将其宣布已合并、正式部署或完成扩容。恢复操作须依据本地备份与最终切换记录进行，不能只复制源码推断 Git 元数据和运行路径已恢复。

## 待验收

- [x] 原来源 refs 可达提交全部仍在统一库可达：服务端 25、Linux 3、Android 18。服务端 `4a67e4d`、Linux `64dcb6b`、Android `a78f4ab` 均为整合主线祖先；所有原分支/标签对象 ID 保留，客户端 refs 使用组件归档前缀。
- [ ] 证明最终根目录属于原 Voice Git，三个工程目录没有独立 `.git`，也不属于上级 OneAxe Git；记录远端与分支。
- [x] 三个初始导入子树的 tree OID 与来源 HEAD 一致；之后仅修改 README、AGENTS 与文档，没有修改功能代码、API 或构建脚本。
- [ ] 核对 `.gitignore` 与跟踪清单，不引入 runtime、令牌、录音、依赖环境、产物或私有备份。
- [ ] 核对服务端环境、systemd 和 F8/F9 路径保持当前部署，已安装 Linux binary 与 Android APK 未被替换；此次不进行服务重启或真听写测试。
- [ ] 证明 `.worktrees/server/capacity` 的分支、独有研究历史和工作区仍可用。
- [x] 文档相对链接与独立构建入口核对通过；临时统一树内服务端 214 项测试、Linux 59 项库测试 + 10 项客户端测试、Android 32 项测试均通过。Android 离线 Debug APK 构建成功，applicationId 与原 APK 签名证书一致，未安装到手机。
- [ ] 记录最终本地提交、远端推送结果和恢复入口。

## 最终结果

待主任务完成正式切换和核验后补充。本轮文档准备阶段未运行服务测试、GPU 识别或客户端输入验收；源码整理、历史导入、正式目录切换、推送和运行证据分别记录。
