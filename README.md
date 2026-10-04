# OneAxe Voice

本机 GPU 听写服务，以及连接服务端的 Linux、Android 轻量客户端。服务端管理 Qwen / R2T2 模型并提供本机 **F8** 听写；Linux 客户端默认使用可配置的 **F9**，Android 保持独立 APK。

## 产品入口

| 工程 | 职责 | 文档 |
| --- | --- | --- |
| `server/` | GPU 识别、本机听写、模型生命周期与远端 API | [服务端 README](server/README.md) · [接口](server/docs/api.md) |
| `clients/linux/` | Linux 本地录音、F9 与 X11 输入 | [Linux README](clients/linux/README.md) · [使用与鉴权](clients/linux/docs/usage.md) |
| `clients/android/` | 手机录音、悬浮听写与输入适配 | [Android README](clients/android/README.md) |

Qwen 流式与 R2T2 当前支持 **1 路 PC + 1 路远端**，Linux 与 Android 共用远端名额。客户端查询 `capabilities` 后绑定 PC 已就绪模型，没有模型选择、加载、切换、卸载或策略权限；远端通过独立设备凭据与 Tailnet HTTPS/WSS 接入。

## 运行与构建

以下命令均从仓库根目录执行。各组件的依赖与安装步骤见上表中的 README；Android 构建需先将 `ANDROID_HOME` 配置为本机 SDK 目录。

服务端状态检查：

```bash
./server/bin/oneaxe-voice health
```

Linux 构建：

```bash
(cd clients/linux && cargo build --release --locked)
```

Android 构建：

```bash
(cd clients/android && ./gradlew --no-daemon :app:assembleDebug)
```

## 关键文档

- [架构与隔离边界](server/docs/architecture.md) · [交互式架构图](server/docs/architecture-map.html)
- [三模式与模型管理](server/docs/modes.md) · [桌面听写](server/docs/desktop.md)
- [移动接口 V1](server/docs/mobile-api-v1.md) · [远端部署与设备凭据](server/docs/mobile-deployment.md)
- [服务端验证记录](server/docs/validation.md) · [Linux 验证记录](clients/linux/docs/validation.md) · [Android 验证记录](clients/android/docs/voice-v1-device-acceptance-2026-10-02.md)

验证记录注明各自的版本和测试范围。
