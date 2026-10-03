# OneAxe Voice Android 协作入口

- 当前源码位于 `~/work/touzi/OneAxe/oneaxe-voice/clients/android`，与服务端、Linux 客户端统一使用 `oneaxe-voice` 根 Git；Android 构建从本目录执行 `./gradlew`，不依赖 Pocket 的构建脚本。相关迁移与历史映射见 `docs/migration/`。
- 2026-10-04 用户已授权统一 Voice 根仓库、完整保留历史并推送原 Voice GitHub 远端；此前独立 Git、不推送和 Linux 暂停约定属于 2026-10-02 拆分迁移范围。Android 本次整合只更新文档并复验现有构建，不安装新 APK 或修改运行服务；后续功能与真机验收按实际任务授权执行。
- 默认简体中文。README 是入口，设计/历史验收/迁移记录在 `docs/`。功能开发依次确认需求、E2E 用例、技术方案和计划；得到执行指令后分批实施，以小 E2E 保持进度，不无限打磨细节。
- 保留用户改动、包名 `com.oneaxe.pocket.voicelab`、签名和配对身份。密钥、设备 token、私人录音不得提交。统一仓库提交与推送遵循仓库根协作约定及用户授权，发布 APK 仍需对应任务授权。

- 先读 [仓库入口](../../README.md)与[根协作约定](../../AGENTS.md)，再按需读[服务端 API](../../server/docs/api.md)、[服务端协作约定](../../server/AGENTS.md)和客户端实现。此前 `/home/sky/docs/oneaxe-voice/README.md` 及 `~/tools/oneaxe-voice` 路径保留在历史记录中。
- 本目录是独立的语音 APK。2026-10-02 用户明确要求与 Pocket 网络 App 长期保持独立，分别安装、更新和调试，不合并到主 App；源码保留在当前目录。语音使用用户已建立的 Tailnet 网络，不启动或管理 VPN。不得因此修改 Pocket 网络主 App，也不修改、重启现有 Voice/VPlus 服务。
- 手机只使用服务端配发的独立设备凭据，并通过 Android Keystore 加密保存；旧临时 lab token 已作废，PC 的模型管理凭据始终留在主机。录音、令牌和完整私人转写不得输出到日志、提交或对话。
- 不干扰 PC 当前录音；与网络测试协调 ADB 设备窗口。本地“录音检查与回放”不调用 ASR，可独立验证。2026-10-02 已明确手机无模型控制权，旧 WAV 接口会隐式切模型，因此仅检查桌面空闲已不足够，不直接恢复旧代理进行转写。模型权限/并发接入先按 [复核记录](docs/voice-connection-review-2026-10-02.md) 审核，不修改解析服务端。
- 手机语音仅通过用户已建立的 Tailnet 直连可配置 DNS 主机与端口；禁止 `localhost`、USB `adb reverse`、临时代理和本地 SSH 转发。正式 DNS `rtx4090.nase-stairs.ts.net`、HTTPS/WSS 端口 `8097` 已部署；协议不提供裸 IP 产品入口。用户自行开启 Tailscale，Lab 仅诊断并提示。旧隐式切模型 ASR 调用不得恢复。
- 正式协议以 [Voice 移动接口 V1](../../server/docs/mobile-api-v1.md) 和 [服务端交接](../../server/docs/pocket-handoff-2026-10-02.md) 为准；手机 HTTP 诊断使用带设备凭据的 `capabilities`，不请求远端 `/health`。帧大小、缓冲、会话上限等运行值从响应读取；设备凭据不可用于模型管理，断线不自动重放。手机固定音频 WSS 已有局部结果，完整真机验收见[执行记录](docs/voice-v1-device-acceptance-2026-10-02.md)，服务端双流测试不能代替手机双端验收。
- 证据分清 APK 构建、固定文本输入、实际 ASR、外部应用输入和端到端通过。辅助功能对自定义终端控件的兼容性须逐项验证，剪贴板救援不算自动输入通过。
