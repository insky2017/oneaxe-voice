# OneAxe Voice Android

手机语音客户端，App 显示名为 OneAxe Voice Lab，包名 `com.oneaxe.pocket.voicelab`，当前为独立调试 APK。选中输入框后，用悬浮按钮开始或结束听写，将文字填入原输入框，不自动回车或发送。服务端源码位于 [`../../server`](../../server)，接入说明见 [项目入口](../../README.md)和[服务端 API](../../server/docs/api.md)。

语音 APK 与 OneAxe Pocket 网络 App、通知 App 分别维护、安装和更新。Voice Lab 使用用户已建立的 Tailnet 网络，自身不启动或管理 VPN。

## 功能与边界

- 可配置 Tailnet DNS 主机及 HTTPS/WSS 端口，查询服务能力并连续发送 PCM 音频，支持停止、取消和流控。当前部署入口为 `rtx4090.nase-stairs.ts.net:8097`；不提供裸 IP 产品入口。
- 只通过用户已建立的 Tailnet 直连，拒绝 localhost、普通局域网和公网目标，不使用 USB 转发、临时代理或本地 SSH。
- 手机仅使用独立设备凭据，不加载、切换或卸载模型。开始前由 PC 选择模型并等待就绪；运行参数和可开始状态以服务端 `capabilities` 为准。Linux 与 Android 共用一个远端名额。
- 普通输入框实时分段写入，并核对文本与光标；光标、目标窗口或编辑会话变化后停止自动提交。密码框和无法验证的输入类型拒绝听写。
- Termux 使用可拖动的悬浮草稿框：听写中只预览，停止并收到最终结果后可编辑，点击“确认粘贴”才一次填入原终端。取消或异常不自动粘贴；确认后安全文本保留在剪贴板。草稿仅保存在辅助功能服务内存中，服务重启不保证保留。
- “录音检查与回放”不依赖连接，不上传录音，离开页面后清理缓存；固定文字输入可独立检查输入适配。

## 设置与使用

1. 自行连接 Pocket/Tailscale，再在 App 的“连接设置”填写 Tailnet DNS 主机及端口并保存。无效配置不覆盖已保存配置。
2. 填写服务端为手机签发的独立设备凭据并检查连接。不要复制 PC 模型管理令牌。
3. 授予麦克风权限，并在系统设置中启用 Voice Lab 辅助功能服务。输入使用辅助功能悬浮层，不需另授悬浮窗权限，也不切换默认键盘。
4. 确认服务端模型就绪，选中目标输入框，用悬浮按钮开始和结束听写。Termux 需在最终结果后主动确认粘贴。

设备凭据使用 Android Keystore 加密保存、不回显。同端点保存且留空时保留现有凭据；换协议、地址或端口后旧凭据失效，可用“清除手机凭据”单独清除。录音、令牌和完整私人转写不得输出到日志或提交仓库。

连接诊断使用设备 Bearer 请求 `GET /api/mobile/v1/capabilities`，走当前应用可用的 VPN Network，检查并固定 Tailnet 解析目标。HTTPS/WSS 保留原主机名的 SNI，并校验系统证书和主机名，不接受任意证书。不请求远端 `/health`，不使用系统 HTTP 代理，不跟随重定向。

失败会区分 VPN 未连接、解析失败、端口不可用、TLS 身份或认证失败，以及模型未就绪、不支持或容量不足。断线后不自动重放音频或重新开始，需用户主动发起新会话。

## 兼容限制

- 最低 Android 8.0（API 26）。Android 13+ 使用辅助功能输入连接并读回确认；Android 8–12 保留节点输入路径，现有真机记录未覆盖这些系统。
- 普通输入框的已有真机证据来自 Pixel 9 Pro XL / Android 17，覆盖微信、ChatGPT PWA、X 搜索、Firefox 地址栏、Keep 和 Gemini 的指定版本及控件；其他自定义编辑器须逐项验证，复制草稿不算自动填入通过。
- Termux 需启用原生硬件快捷键。终端粘贴没有通用接收回执；Android 8–12、隐藏密码提示、任意 TUI 和硬件快捷键切换会话尚未全面验证。
- 完整 V01–V07 验收尚未全部通过；双端真实麦克风长流、跨网络及更多故障场景仍有待验项。能力查询或服务端双流成功不能代替手机端到端验证，历史长流结果对应各自 APK。
- 旧 WAV 接口禁用；`tools/voice_lab_proxy.py`、`tools/pair_device.py` 仅保留为实验源码，不作为正式连接或恢复方式。

## 构建与检查

已验证的构建环境为 JDK 21 和 Android SDK API 36。本组件使用 Gradle wrapper（9.6.0）与 Android Gradle Plugin 9.4.0，Java 源码级别为 17。SDK 路径通过 `ANDROID_HOME` 或本地 `local.properties` 的 `sdk.dir` 配置，`local.properties` 不提交仓库。

以下命令从本组件目录（仓库内 `clients/android/`）执行：

```bash
./gradlew --no-daemon :app:testDebugUnitTest :app:assembleDebug
```

首次构建需联网下载 Gradle 和依赖；缓存齐备时可加 `--offline`。产物为 `app/build/outputs/apk/debug/app-debug.apk`。

`tests/TailnetEndpointTest.java` 是纯 Java 地址边界检查，可与 `app/src/main/java/com/oneaxe/pocket/voicelab/TailnetEndpoint.java` 编译后执行。

## 接口与验证记录

- [正式移动 V1 契约](../../server/docs/mobile-api-v1.md)与[服务端交接](../../server/docs/pocket-handoff-2026-10-02.md)
- [连接设置真机记录](tests/tailnet-settings-e2e-2026-10-02.md)
- [普通输入兼容记录](docs/voice-input-compatibility-2026-10-02.md)
- [Termux 悬浮编辑记录](docs/voice-termux-overlay-2026-10-02.md)与[原始兼容记录](docs/voice-termux-compatibility-2026-10-02.md)
- [Voice V1 真机记录及待验项](docs/voice-v1-device-acceptance-2026-10-02.md)
- [连接、模型与并发复核](docs/voice-connection-review-2026-10-02.md)
