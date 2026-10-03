# OneAxe Voice

本机 GPU 听写服务，以及连接服务端的 Linux、Android 轻量客户端。服务端管理 Qwen / R2T2 模型并提供本机 **F8** 听写；Linux 客户端默认使用可配置的 **F9**，Android 保持独立 APK。

## 产品入口

| 工程 | 职责 | 文档 |
| --- | --- | --- |
| `server/` | GPU 识别、本机听写、模型生命周期与远端 API | [服务端 README](server/README.md) · [接口](server/docs/api.md) |
| `clients/linux/` | Linux 本地录音、F9 与 X11 输入 | [Linux README](clients/linux/README.md) · [使用与鉴权](clients/linux/docs/usage.md) |
| `clients/android/` | 手机录音、悬浮听写与输入适配 | [Android README](clients/android/README.md) |

Qwen 流式与 R2T2 当前支持 **1 路 PC + 1 路远端**，Linux 与 Android 共用远端名额。客户端查询 `capabilities` 后绑定 PC 已就绪模型，没有模型选择、加载、切换、卸载或策略权限；远端通过独立设备凭据与 Tailnet HTTPS/WSS 接入。

## 仓库与构建

项目根目录为 `~/work/touzi/OneAxe/oneaxe-voice`，使用原有 [insky2017/oneaxe-voice](https://github.com/insky2017/oneaxe-voice) 仓库和 `master` 主分支。服务端、Linux 与 Android 的历史保留在同一 Git 中；三者分别维护运行环境、构建与安装入口，子目录不另设 `.git`。

Git 操作在根目录执行：

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice
git status
```

服务端命令和环境操作在 `server/` 执行，具体安装与验证条件见[服务端运行说明](server/docs/api.md)：

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice/server
./bin/oneaxe-voice health
```

Linux 构建在 `clients/linux/` 执行：

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice/clients/linux
cargo build --release --locked
```

Android 构建在 `clients/android/` 执行，使用该工程自己的 Gradle wrapper；`ANDROID_HOME` 按本机 SDK 路径设置：

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice/clients/android
ANDROID_HOME="$HOME/tools/android" ./gradlew --offline --no-daemon :app:assembleDebug
```

容量研究保留在 `.worktrees/server/capacity`，其历史与环境按[整合记录](docs/git-consolidation-2026-10-04.md)管理。正式服务仍从 `server/` 运行；共享权重继续从 `~/tools/models` 只读加载。

## 关键文档

- [Git 整合流程与验收](docs/git-consolidation-2026-10-04.md) · [此前目录迁移记录](server/docs/directory-migration-2026-10-04.md)
- [架构与隔离边界](server/docs/architecture.md) · [交互式架构图](server/docs/architecture-map.html)
- [三模式与模型管理](server/docs/modes.md) · [桌面听写](server/docs/desktop.md)
- [移动接口 V1](server/docs/mobile-api-v1.md) · [远端部署与设备凭据](server/docs/mobile-deployment.md)
- [服务端验证记录](server/docs/validation.md) · [Linux 验证记录](clients/linux/docs/validation.md) · [Android 验证记录](clients/android/docs/voice-v1-device-acceptance-2026-10-02.md)

各验证记录只证明其注明的版本、入口和测试范围。Git 整合结果另行记录，不代替部署、真实识别或客户端输入验收。OneAxe Voice 的代码、环境和服务独立于 VPlus；令牌、录音、个人配置和依赖环境不进入 Git。
