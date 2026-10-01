# OneAxe Voice Lab

独立调试 APK，包名 `com.oneaxe.pocket.voicelab`。目标仍是“选中输入框 → 小悬浮按钮开始/结束 → 原输入框得到文字”，不自动回车或发送。所有接入工作先读 [OneAxe Voice 入口](/home/sky/docs/oneaxe-voice/README.md)。

## 当前状态（2026-10-02）

- 已改为 **Tailnet 主机与端口配置**，默认 `https://rtx4090.nase-stairs.ts.net:8097`，支持 Tailnet IPv4、IPv6 和完整 `*.ts.net` 主机名，协议可选 HTTPS/HTTP。既有已保存地址保留，手机鉴权与正式流式协议仍待定稿。
- 用户先自行连接 Pocket/Tailscale；App 只检查连接并显示错误，不启动或重连 VPN。拒绝 localhost、普通局域网/公网目标；不使用 USB 转发、临时代理或本地 SSH。
- 默认端点只是初始设置。PC 的 `8097` 当前仍只监听 loopback；Tailnet HTTPS/WSS 入口尚未部署，不能把默认值当作已可用移动入口。
- **手机听写尚不可用。** 旧 WAV 接口会隐式替换 PC 模型，现已停止调用；新接口确认并实现前，开始听写先检查连接并说明原因，不采集/上传音频。文件测试路径也不能绕过此限制。
- “录音检查与回放”可独立使用，不依赖连接，不上传录音；不录音的固定文字输入仍可验证输入适配。

## 设置与使用

打开 App 的“连接设置”，选择协议，填写 Tailscale 主机及端口，再点“保存连接设置”和“检查已保存的连接”。支持例如 `100.76.106.96`、`rtx4090.nase-stairs.ts.net`；IPv6 主机栏不带方括号。无效配置不覆盖已保存配置。

旧 localhost 地址和旧 lab token 在首次打开新版时作废。手机专用凭据可稍后设置，不要复制 PC 模型管理令牌；凭据用 Android Keystore 加密保存、不回显。同端点保存且留空时保留现有凭据，换协议/地址/端口后旧凭据失效，可用“清除手机凭据”单独清除。

连接诊断只发不带凭据的 `GET /health`，走当前应用可用的 VPN Network，检查并固定 Tailnet 解析目标；HTTPS 保留原主机名的 SNI 与系统证书校验，不接受任意证书。不使用系统 HTTP 代理、不跟随 HTTP/HTTPS 重定向。失败区分未检测到 VPN、解析失败、目标地址不符合要求、连接/端口不可用、HTTP 拒绝或健康响应不符。VPN 存在和地址属于 Tailnet 范围不能替代设备认证；健康检查通过也不表示模型就绪、权限正确或双端识别可用。

本地输入实验使用辅助功能悬浮层，不需另授悬浮窗权限。录音检查需要麦克风权限，结束/离开页面清理缓存录音。外部 Termux、自定义控件兼容性仍未验收，复制草稿不算自动填入通过。

## 构建与检查

从本目录执行：

```bash
ANDROID_HOME=/home/sky/tools/android /home/sky/tools/oneaxe-pocket/android/gradlew --offline --no-daemon :app:assembleDebug
```

产物：`app/build/outputs/apk/debug/app-debug.apk`，仍为独立 debug APK。

`tests/TailnetEndpointTest.java` 是纯 Java 地址边界检查，可与 `TailnetEndpoint.java` 编译后执行。当前 E2E 与 APK 证据见 [连接设置真机记录](tests/tailnet-settings-e2e-2026-10-02.md)。

## 接口协作与历史证据

- [接口交接请求](../../docs/mobile-workspace/voice-api-handoff-2026-10-02.md)：给 Voice 会话协商设备权限、只用当前模型、双端并发与 Tailnet 入口。用户已转来 Voice 会话反馈；[Pocket 回复](../../docs/mobile-workspace/voice-api-response-2026-10-02.md)接受版本化 API、DNS/HTTPS/WSS 和 R2T2 双端先验方向。完整消息/认证契约尚未定稿，不是已实现协议。
- [连接、模型与并发复核](../../docs/mobile-workspace/voice-connection-review-2026-10-02.md)：旧实现的实际副作用与失败原因。
- [2026-10-01 真机记录](tests/device-e2e-2026-10-01.md)：保留当时临时 USB 环境下的普通/弱音/静音/中文样本、麦克风回放与输入结果。这些历史结果不代表正式 Tailnet 接入、手机无模型权限或双端并发已完成。
- `tools/voice_lab_proxy.py`、`tools/pair_device.py` 仅保留为旧实验源码；不再作为手机连接恢复方式，不运行它们来绕过新要求。
