# OneAxe Voice Lab

独立语音 APK，当前用于调试，包名 `com.oneaxe.pocket.voicelab`。目标仍是“选中输入框 → 小悬浮按钮开始/结束 → 原输入框得到文字”，不自动回车或发送。所有接入工作先读 [OneAxe Voice 入口](/home/sky/docs/oneaxe-voice/README.md)。

用户已明确要求与 OneAxe Pocket 网络 App 长期保持独立：分别安装、更新和调试，不合并。Voice Lab 使用用户已建立的 Tailnet 网络，自身不启动或管理 VPN。

## 当前状态（2026-10-02）

- 已有 **Tailnet 主机与端口配置**。V1 正式识别默认使用可修改的 DNS 主机 `rtx4090.nase-stairs.ts.net` 与 HTTPS/WSS 端口 `8097`；裸 IP 仅用于无凭据诊断。历史 HTTP/地址配置不等于正式识别入口。
- 用户先自行连接 Pocket/Tailscale；App 只检查连接并显示错误，不启动或重连 VPN。拒绝 localhost、普通局域网/公网目标；不使用 USB 转发、临时代理或本地 SSH。
- 默认端点只是初始设置。上次核查 PC 的 `8097` 仅监听 loopback；本轮未重新探测服务。V1 定稿文档注明移动入口尚未部署，不能把默认值当作已可用入口。
- [Voice 移动接口 V1](/home/sky/tools/oneaxe-voice/docs/mobile-api-v1.md) 已定稿；Voice Lab 已预实现能力查询、流式客户端、连续 PCM、会话身份/序号检查、累计固定全文提交、取消、流控和凭据备份排除。候选 APK 构建及 18 项本地测试通过；服务端尚未实现/部署，未接真实服务、未在设备上验收这版客户端，不能称手机听写已可用。旧 WAV 接口仍禁用。
- “录音检查与回放”可独立使用，不依赖连接，不上传录音；不录音的固定文字输入仍可验证输入适配。

## 设置与使用

打开 App 的“连接设置”，填写 Tailnet DNS 主机及端口并保存，再检查连接。正式听写使用经系统证书和主机名校验的 HTTPS/WSS；裸 IP 仅用于无凭据诊断。无效配置不覆盖已保存配置。

旧 localhost 地址和旧 lab token 在首次打开新版时作废。手机专用凭据可稍后设置，不要复制 PC 模型管理令牌；凭据用 Android Keystore 加密保存、不回显。同端点保存且留空时保留现有凭据，换协议/地址/端口后旧凭据失效，可用“清除手机凭据”单独清除。

连接诊断只发不带凭据的 `GET /health`，走当前应用可用的 VPN Network，检查并固定 Tailnet 解析目标；HTTPS 保留原主机名的 SNI 与系统证书校验，不接受任意证书。不使用系统 HTTP 代理、不跟随 HTTP/HTTPS 重定向。失败区分未检测到 VPN、解析失败、目标地址不符合要求、连接/端口不可用、HTTP 拒绝或健康响应不符。VPN 存在和地址属于 Tailnet 范围不能替代设备认证；健康检查通过也不表示模型就绪、权限正确或双端识别可用。

本地输入实验使用辅助功能悬浮层，不需另授悬浮窗权限。录音检查需要麦克风权限，结束/离开页面清理缓存录音。外部 Termux、自定义控件兼容性仍未验收，复制草稿不算自动填入通过。

## 构建与检查

从本目录执行：

```bash
ANDROID_HOME=/home/sky/tools/android /home/sky/tools/oneaxe-pocket/android/gradlew --offline --no-daemon :app:testDebugUnitTest :app:assembleDebug
```

产物：`app/build/outputs/apk/debug/app-debug.apk`，仍为独立 debug APK。

`tests/TailnetEndpointTest.java` 是纯 Java 地址边界检查，可与 `TailnetEndpoint.java` 编译后执行。当前 E2E 与 APK 证据见 [连接设置真机记录](tests/tailnet-settings-e2e-2026-10-02.md)。

## 接口协作与历史证据

- [接口交接请求](../../docs/mobile-workspace/voice-api-handoff-2026-10-02.md)与[Pocket 回复](../../docs/mobile-workspace/voice-api-response-2026-10-02.md)保留协商历史；[正式 V1 契约](/home/sky/tools/oneaxe-voice/docs/mobile-api-v1.md)已定，服务端尚未实现部署。[客户端预实现记录](../../docs/mobile-workspace/voice-v1-client-preparation-2026-10-02.md)区分源码、构建、进程内测试与真实 E2E。
- [连接、模型与并发复核](../../docs/mobile-workspace/voice-connection-review-2026-10-02.md)：旧实现的实际副作用与失败原因。
- [2026-10-01 真机记录](tests/device-e2e-2026-10-01.md)：保留当时临时 USB 环境下的普通/弱音/静音/中文样本、麦克风回放与输入结果。这些历史结果不代表正式 Tailnet 接入、手机无模型权限或双端并发已完成。
- `tools/voice_lab_proxy.py`、`tools/pair_device.py` 仅保留为旧实验源码；不再作为手机连接恢复方式，不运行它们来绕过新要求。
