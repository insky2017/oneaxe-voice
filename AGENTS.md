# Voice Lab 协作入口

- 先读 `/home/sky/docs/oneaxe-voice/README.md`，再按其中链接读 `/home/sky/tools/oneaxe-voice/docs/api.md`、`AGENTS.md` 和桌面听写实现。该入口由用户明确指定。
- 本目录是独立的语音实验 APK，不修改 Pocket 网络主 App，也不修改、重启现有 Voice/VPlus 服务。
- 手机只使用服务端配发的独立设备凭据，并通过 Android Keystore 加密保存；旧临时 lab token 已作废，PC 的模型管理凭据始终留在主机。录音、令牌和完整私人转写不得输出到日志、提交或对话。
- 不干扰 PC 当前录音；与网络测试协调 ADB 设备窗口。本地“录音检查与回放”不调用 ASR，可独立验证。2026-10-02 已明确手机无模型控制权，旧 WAV 接口会隐式切模型，因此仅检查桌面空闲已不足够，不直接恢复旧代理进行转写。模型权限/并发接入先按 [复核记录](../../docs/mobile-workspace/voice-connection-review-2026-10-02.md) 审核，不修改解析服务端。
- 手机语音仅通过用户已建立的 Tailnet 直连可配置 DNS 主机与端口；禁止 `localhost`、USB `adb reverse`、临时代理和本地 SSH 转发。默认 DNS `rtx4090.nase-stairs.ts.net`、HTTPS/WSS 端口 `8097` 是未部署的初值；裸 IP 仅作无凭据诊断。用户自行开启 Tailscale，Lab 仅诊断并提示。旧隐式切模型 ASR 调用不得恢复。
- 正式协议以 [Voice 移动接口 V1](/home/sky/tools/oneaxe-voice/docs/mobile-api-v1.md) 为准，不再把[历史接口回复](../../docs/mobile-workspace/voice-api-response-2026-10-02.md)当成待定 schema。服务端仍未实现/部署；可预实现客户端与确定性测试，但构建/桩测试不算真实服务或手机 E2E。帧大小、缓冲、会话上限等运行值从 `capabilities` 读取；设备凭据不可用于模型管理，断线不自动重放。当前进展见 [预实现记录](../../docs/mobile-workspace/voice-v1-client-preparation-2026-10-02.md)。
- 证据分清 APK 构建、固定文本输入、实际 ASR、外部应用输入和端到端通过。辅助功能对自定义终端控件的兼容性须逐项验证，剪贴板救援不算自动输入通过。
