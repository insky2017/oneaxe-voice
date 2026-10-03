# OneAxe Voice Android

手机语音客户端源码：`~/work/touzi/OneAxe/oneaxe-voice/clients/android`。由 Pocket 的 `experiments/voice-lab` 提取，现与服务端、Linux 客户端统一由 `oneaxe-voice` 根 Git 管理；本目录保留自己的 Gradle wrapper。手机 App 仍显示 Voice Lab，包名、签名与已配对身份不变。服务端源码位于 [`../../server`](../../server)，手机语音 APK 与 Pocket 网络 App、通知 App 分别维护。

2026-10-04 用户授权整合 Voice 仓库并保留完整历史；Android 本次只更新目录与协作说明并复验现有构建，见[整合复验记录](docs/migration/unified-2026-10-04.md)。[2026-10-02 独立工程迁移](docs/migration/standalone-2026-10-02.md)保留当时路径、Git 映射与构建证据，下文真机结果继续对应各自 APK 基线。

独立语音 APK，当前用于调试，包名 `com.oneaxe.pocket.voicelab`。目标仍是“选中输入框 → 小悬浮按钮开始/结束 → 原输入框得到文字”，不自动回车或发送。接入工作先读 [OneAxe Voice 仓库入口](../../README.md)与[服务端 API](../../server/docs/api.md)。

用户已明确要求与 OneAxe Pocket 网络 App 长期保持独立：分别安装、更新和调试，不合并。Voice Lab 使用用户已建立的 Tailnet 网络，自身不启动或管理 VPN。

## 功能与真机证据（2026-10-02 APK 基线）

- 已有 **Tailnet 主机与端口配置**。V1 正式识别使用可修改的 DNS 主机 `rtx4090.nase-stairs.ts.net` 与 HTTPS/WSS 端口 `8097`；目前没有裸 IP 产品入口。
- 用户先自行连接 Pocket/Tailscale；App 只检查连接并显示错误，不启动或重连 VPN。拒绝 localhost、普通局域网/公网目标；不使用 USB 转发、临时代理或本地 SSH。
- Voice 正式 Tailnet `8097` 入口已部署；[交接记录](../../server/docs/pocket-handoff-2026-10-02.md)证明服务端 TLS、认证、真实 WSS 转写及 PC + 移动双流测试。Voice Lab 已实现能力查询、连续 PCM、固定全文提交和取消/流控；当前修复版新增输入框文本与光标写入确认，**32/32 本地单测通过**。
- 当前已安装 APK SHA-256 `24c649c0fe73d8f0ad3637f8631f0d1dba0635cee2c191a4418ece59e6ae8405`。**Termux 悬浮草稿框**实时显示听写文字，停止并收到最后结果后可编辑；点击“确认粘贴”才一次填入原终端，不自动回车。32/32 单测与固定音频预览、编辑后粘贴、取消、抽屉/前台变化、真实麦克风、拖动/软键盘及普通框回归已通过，见[悬浮编辑真机记录](docs/voice-termux-overlay-2026-10-02.md)。确认前终端不写入，文字可在浮窗审阅；确认后安全文本保留在剪贴板。普通输入框继续实时分段写入。
- 前一版 Termux APK SHA-256 `1c26380dd93f83e8345f5d1becac3762b839657fac13f2858070d99ff265b39b` 的 TYPE_NULL/原生粘贴、粘滞 Ctrl/Alt 和换会话验证见[原始兼容记录](docs/voice-termux-compatibility-2026-10-02.md)。仍需保持 Termux 硬件快捷键启用；终端粘贴请求没有通用接收回执。
- 前一版输入兼容 APK SHA-256 `371b3cb75a6b6f73d44dbbed8674b95e09a029f6b28a402e3db199e1a6b7d323`。Android 13+ 新增辅助功能输入连接，按光标增量提交并读回确认，不切换默认键盘；修复空框 `selection=-1` 误判，以及微信不暴露节点、未知绝对 offset 的适配。Pixel 9 Pro XL / Android 17 上，微信、ChatGPT PWA、X 搜索、Firefox 地址栏、Keep 和 Gemini 均已完成固定音频输入验证；微信另完成真实麦克风回录 88/88。见[兼容性真机记录](docs/voice-input-compatibility-2026-10-02.md)。Android 8–12 保留原节点路径，本轮未做这些系统的真机验证。
- 前一版 Chrome 输入确认、取消和断网恢复，以及更早 APK 的手机/PC 10 分钟固定音频测试，保留在[Voice V1 真机记录](docs/voice-v1-device-acceptance-2026-10-02.md)；不将历史长流测试写成当前 APK 已重跑。V01–V07 尚未全部通过，旧 WAV 接口仍禁用。
- “录音检查与回放”可独立使用，不依赖连接，不上传录音；不录音的固定文字输入仍可验证输入适配。

## 设置与使用

打开 App 的“连接设置”，填写 Tailnet DNS 主机及端口并保存，再检查连接。正式听写使用经系统证书和主机名校验的 HTTPS/WSS；当前没有裸 IP 产品入口。无效配置不覆盖已保存配置。

旧 localhost 地址和旧 lab token 在首次打开新版时作废。手机专用凭据可稍后设置，不要复制 PC 模型管理令牌；凭据用 Android Keystore 加密保存、不回显。同端点保存且留空时保留现有凭据，换协议/地址/端口后旧凭据失效，可用“清除手机凭据”单独清除。

连接诊断使用设备 Bearer 请求 `GET /api/mobile/v1/capabilities`，走当前应用可用的 VPN Network，检查并固定 Tailnet 解析目标；HTTPS 保留原主机名的 SNI 与系统证书校验，不接受任意证书。不请求远端 `/health`，不使用系统 HTTP 代理、不跟随 HTTP/HTTPS 重定向。失败区分未检测到 VPN、解析失败、连接/端口不可用、TLS 身份或认证失败，以及模型未就绪/不支持/容量不足。能力查询成功仍不等于持续听写或双端 E2E 通过。

本地输入实验使用辅助功能悬浮层，不需另授悬浮窗权限。录音检查需要麦克风权限；9 秒手机采集已测 RMS 527.09、峰值 4836，可回放，离开后缓存自动清理。Termux 内置文本输入栏保留此前结果，终端画布的本轮范围与限制见[Termux 记录](docs/voice-termux-compatibility-2026-10-02.md)；其他自定义控件仍需逐项验证，复制草稿不算自动填入通过。

## 构建与检查

从统一仓库中的 Android 目录执行，使用本目录的 wrapper：

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice/clients/android
ANDROID_HOME="$HOME/tools/android" ./gradlew --offline --no-daemon :app:testDebugUnitTest :app:assembleDebug
```

产物：`app/build/outputs/apk/debug/app-debug.apk`，仍为独立 debug APK。2026-10-04 整合复验只覆盖现有本地测试和 APK 构建；未安装新 APK，也未重跑真机或服务端 E2E，详见[整合复验记录](docs/migration/unified-2026-10-04.md)。

`tests/TailnetEndpointTest.java` 是纯 Java 地址边界检查，可与 `TailnetEndpoint.java` 编译后执行。当前 E2E 与 APK 证据见 [连接设置真机记录](tests/tailnet-settings-e2e-2026-10-02.md)。

## 接口协作与历史证据

- [接口交接请求](docs/voice-api-handoff-2026-10-02.md)与[Pocket 回复](docs/voice-api-response-2026-10-02.md)保留协商历史；[正式 V1 契约](../../server/docs/mobile-api-v1.md)和[服务端交接](../../server/docs/pocket-handoff-2026-10-02.md)是当前依据。[客户端预实现记录](docs/voice-v1-client-preparation-2026-10-02.md)保留上线前状态，既有验收见[真机记录](docs/voice-v1-device-acceptance-2026-10-02.md)。
- [连接、模型与并发复核](docs/voice-connection-review-2026-10-02.md)：旧实现的实际副作用与失败原因。
- [2026-10-01 真机记录](tests/device-e2e-2026-10-01.md)：保留当时临时 USB 环境下的普通/弱音/静音/中文样本、麦克风回放与输入结果。这些历史结果不代表正式 Tailnet 接入、手机无模型权限或双端并发已完成。
- `tools/voice_lab_proxy.py`、`tools/pair_device.py` 仅保留为旧实验源码；不再作为手机连接恢复方式，不运行它们来绕过新要求。
