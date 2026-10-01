# Pocket 接入交接

接口已由 Voice 定稿，请直接按 [移动接口 V1](mobile-api-v1.md) 实现，不再协商已确定的字段。部署操作见 [移动入口部署](mobile-deployment.md)，各项实测与限制见 [验证记录](validation.md)。

- 正式目标：`https://rtx4090.nase-stairs.ts.net:8097` / 同主机 `wss://`，保持系统证书和 DNS 身份校验。先查能力接口，不使用远端 `/health`；管理路径和旧转写路径在移动入口返回 403。
- 查询：`GET /api/mobile/v1/capabilities`；连接：`WS /api/mobile/v1/dictation/stream`。两者都用独立设备 Bearer，放 `Authorization` header。PC 本机令牌不能下发手机。
- 首版仅 `r2t2`，容量 1 PC + 1 手机。先查询，再用实例和模型代次 `start`，收到 `ready` 才录音。手机不传 mode，不替用户加载或切换模型。
- 音频为 `16 kHz / mono / PCM16LE` 二进制，每帧 2–5120 字节，建议 80–160 ms；按 `audio_send_limit` 的累计样本上限发送，缓冲最多 2 秒。发送与接收独立；`flush/finish` 携带 `after_audio_samples`。
- `text` 为累计固定全文，`pending` 只显示候选。核对身份、`seq` 与固定前缀，只追加新后缀。取消、断线、模型变更时保留本地已确认文字，不自动重连录音、不回放旧音频、不补贴旧目标。
- PC 主动切换/卸载时手机收到 `MODEL_CHANGED` / `MODEL_NOT_READY`；凭据吊销收到 `UNAUTHORIZED`。手机结束不卸载模型，不影响 PC。可重试诊断不等于自动重试录音。

用户或本机 agent 用 CLI 签发 Pocket 专用凭据并安全交给 App；文档不存真实 token。代码库测试脚本提供完整服务端复现。Pocket 仍需完成 Android 麦克风、Keystore 存储、真实输入框和不同网络经 Tailnet 的验收，这些不能由本机模拟客户端代替。
