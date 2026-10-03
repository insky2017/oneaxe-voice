# Voice V1 客户端预实现记录

**后续状态：** 本页保留上线前的预实现历史。正式入口已部署，手机局部接入结果及剩余验收转到 [2026-10-02 真机执行记录](voice-v1-device-acceptance-2026-10-02.md)；不要用下文“尚未部署”判断当前状态。

日期：2026-10-02。依据 Voice 负责人定稿的[移动接口 V1](/home/sky/tools/oneaxe-voice/docs/mobile-api-v1.md)，用户已授权按确定部分提前实现独立 Voice Lab 客户端，变化部分暂不做真实服务调试。Voice 服务端尚未实现、部署或完成双端验收；本页不是手机听写可用证明。

## 当前范围

- 客户端已预实现 `GET /api/mobile/v1/capabilities` 和 `WS /api/mobile/v1/dictation/stream`。启动前核对服务实例与模型代次；手机只消费 PC 实际已加载且支持的模型，不发模型加载、切换、卸载或策略请求。
- 连续上传 16 kHz 单声道 PCM16 小端音频，遵守服务端给出的累计样本发送上限与有界本地缓冲；音频发送和事件接收独立。帧上限、缓冲时长、会话上限等运行值从 `capabilities` 获取，`8097` 仅是可配置的默认端口，不是硬编码性能或已上线承诺。
- 以服务实例、模型代次、会话 ID 和事件 `seq` 检查身份及顺序；`text` 是累计固定全文，只提交新增且前缀一致的部分，`pending` 不自动填入。取消后不再提交迟到文字；断线保留本地已收到文字，不自动重放音频、补贴文字或开始新会话。
- 设备专用 Bearer 经 Android Keystore 保护，并从 Android 备份中排除；不使用 PC 模型管理凭据。正式识别默认完整 Tailnet DNS 名的 HTTPS/WSS，系统证书及主机名校验不可关闭。裸 IP 仅作**无凭据诊断**，不走正式识别。
- 旧短 WAV 接口存在隐式切模型风险，继续停用。服务端移动接口未上线时，不借旧接口或 USB/本地转发模拟正式接入。

## 证据与待补

| 层级 | 当前状态 | 仍需证据 |
| --- | --- | --- |
| 源码 | 上述客户端与协议处理已实现；握手取消、缓冲和锁序专项复核通过 | 按服务端实际部署完成联调 |
| 构建 | debug APK 构建成功，命令退出码 0 | 本轮未安装到手机 |
| 确定性进程内测试 | 18 项通过，失败/错误/跳过均为 0 | 不覆盖真实 Android 采集/输入、TLS、设备权限或 GPU 并发 |
| 真实 Voice 服务与设备 | 尚未连接真实 V1 服务；本轮未操作设备 | 服务端实现部署后，按 V01–V07 联调；PC/手机不同测试语音连续至少 10 分钟并验收各自出字、流控、权限和输入框落点 |

既有 Voice Lab 的固定文字输入、录音回放、USB 实验和连接设置实测仍作为各自的历史局部证据，不升级为本次 V1 的正式通过。后续服务端发布实际监听/TLS、设备凭据配发方式和可运行联调脚本后，再安排真实手机 Tailnet 测试；运行时能力值以实际响应记录。


## 构建与确定性检查结果

契约文件 SHA-256：`3e24eb5cf02295d79bf3c3c80f82090a859f746170cca3c50d1874cf8945278a`。测试模拟该定稿契约，不预设未来推理性能或实际服务可用性。

```bash
cd /home/sky/work/touzi/OneAxe/oneaxe-voice-android
ANDROID_HOME=/home/sky/tools/android ./gradlew --offline --no-daemon :app:testDebugUnitTest :app:assembleDebug
```

结果：`BUILD SUCCESSFUL`，退出码 `0`。最终构建复用同源码已通过的单元测试结果（Gradle `UP-TO-DATE`）；产物为 [候选 APK](../app/build/outputs/apk/debug/app-debug.apk)，SHA-256 为 `4464ec01a2ae95d935ce9de64f0630e6077a35a489bc4ff6694b6ee725d62443`。

| 测试组 | 数量 | 主要覆盖 |
| --- | --- | --- |
| `MobileProtocolTest` | 9 | 实例/代次/会话/序号、累计固定全文、累计流控、运行参数、未就绪拒绝、错误终止无 processed 字段、已出队帧计入缓冲 |
| `VoiceTransportTest` | 4 | 进程内 FakeWire 音频发送与文字接收、finish 真实样本数、取消和迟到结果、网络库排队纳入上限、握手取消 |
| `DictationDraftTest` | 5 | 收到与填入分离、慢 UI 合并、取消保留草稿、目标变化不补贴、前缀冲突、原样保存空格/换行/Unicode |

`git diff --check` 和变更文档本地链接检查通过。测试不创建网络监听，不连接真实 Voice API；本轮未操作手机、安装 APK、录音或改变 PC 模型/服务状态。原设备录音回放能力未在此版本重新验收。
