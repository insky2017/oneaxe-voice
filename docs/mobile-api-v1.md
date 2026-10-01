# OneAxe Voice 移动接口 V1

2026-10-02，由 Voice 负责人定稿。**服务端已实现本契约**；Pocket 按本文开发。实际部署地址、凭据操作见 [部署文档](mobile-deployment.md)，GPU、接口和真机证据分别记录在 [验证记录](validation.md)。运行时数值以服务返回值为准，示例标识不是实际凭据或代次。

## 入口和权限

- 默认 HTTPS/WSS 主机 `rtx4090.nase-stairs.ts.net`，端口 `8097`；手机设置允许修改 DNS 主机名与端口。严格使用系统证书及主机名校验，裸 IP 仅作诊断。
- 服务仅在受控 Tailnet 地址提供移动接口；本机模型管理入口保留 loopback。两种入口共用一个模型核心。
- 每设备独立随机 Bearer，至少 32 字节随机熵。本机配发、手动录入手机；服务端只存摘要及设备元数据，手机使用受 Keystore 保护的私有存储并排除备份。
- 凭据仅有 `voice.mobile.read`、`voice.mobile.stream`。默认持续有效直至 PC 吊销或轮换；轮换使旧凭据立即失效。吊销同时结束该设备已有会话，不影响 PC。
- HTTP 和 WS 握手均使用 `Authorization: Bearer <device-token>`，不得放入 URL。未认证返回 401，认证后无权限返回 403；旧 HTTP 识别/管理接口拒绝移动凭据，旧 WS 在升级前拒绝或关闭为 1008。
- 手机不得选择、加载、切换、卸载模型或修改常驻策略。`start` 不接受模型管理字段或其他未知字段。旧 PC 令牌不下发手机。

## 能力查询

`GET /api/mobile/v1/capabilities`，需要 `voice.mobile.read`。查询不加载模型、不刷新空闲计时。认证成功返回 200，未就绪/不支持等状态通过正文表示。

```json
{"protocol_version":1,"server_instance_id":"boot-example","model_generation":"load-example","model_id":"Confucius4-R2T2","mode":"r2t2","model_state":"ready","ready":true,"stream_supported":true,"can_start":true,"unavailable_reason":null,"max_sessions":2,"mobile_slots_available":1,"audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1,"max_frame_bytes":5120},"flow":{"window_samples":32000,"client_buffer_max_ms":2000},"session_max_seconds":3600}
```

- `ready` 只表示实际模型完成加载和预热；`can_start` 同时考虑模式支持、切换状态及移动会话容量。查询结果不是容量预留。
- 首版支持 1 路 PC + 1 路手机；为 PC 保留容量，不支持第二路手机。`mobile_slots_available` 为 0 或 1。
- 首版只有 `r2t2` 的 `stream_supported=true`。其他模式返回 `MODEL_UNSUPPORTED`，不切换模型。
- `model_state` 为 `unloaded/loading/ready/unloading/error`。未加载时模型代次为空；`server_instance_id` 在重启后变化，模型重新加载后 `model_generation` 变化。客户端把二者当不透明标识。
- 不可开始时 `unavailable_reason` 使用下文稳定错误码。故障、未就绪、不支持、容量不足分别判断，不用 `ready` 一个字段代替所有状态。

## 建立会话

`WS /api/mobile/v1/dictation/stream`，需要 `voice.mobile.stream`。连接后 5 秒内发送一次 `start`，收到 `ready` 后才可发送音频。

```json
{"type":"start","protocol_version":1,"expected_server_instance_id":"boot-example","expected_model_generation":"load-example","audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1}}
```

服务端在同一个生命周期临界区核对身份、实际就绪模型、上述两个标识及容量，并绑定会话。状态已变返回 `MODEL_CHANGED`，不替手机加载模型。

```json
{"type":"ready","server_instance_id":"boot-example","model_generation":"load-example","session_id":"session-example","seq":1,"audio_received_samples":0,"audio_processed_samples":0,"audio_send_limit":32000}
```

`ready` 之后所有服务端事件均包含上例中的实例、代次、会话和 `seq`。`seq` 是本会话递增事件号，包含流控、候选、保活和结束事件；不是字数或文本版本。所有事件经过同一发送队列，按实际发送顺序分配序号；不得让独立发送任务把流控和文字序号发乱。不保证序号连续，允许合并尚未发送的中间快照。建立失败的 `error` 使用 `session_id:null, seq:0`。

## 音频与流控

- 二进制消息为 16 kHz、单声道、PCM16 小端。每帧 2–5120 字节且为偶数；正常按 80–160 ms 采集节奏发送，短尾帧可小于此范围。每秒最多 100 条输入消息（音频和控制合计），超过则结束本会话。
- 发送与接收独立进行。服务端持续返回累计进度的 `flow`，不要求音频包与文字事件一一对应。

```json
{"type":"flow","server_instance_id":"boot-example","model_generation":"load-example","session_id":"session-example","seq":2,"audio_received_samples":5120,"audio_processed_samples":5120,"audio_send_limit":37120}
```

- 三个音频计数均从本会话 0 开始，只计客户端真实样本，不计推理内部回放的窗口、预热或补入静音；`received` 是已接受输入，`processed` 是已完成处理的输入，包括明确跳过且无需 GPU 推理的数字静音，不能把二者混淆。`flush` 不重置这些会话累计计数。
- `audio_send_limit` 是允许发送的**累计样本上限**，初始 32000（2 秒），随后为 `processed + 32000`。它不是可累加的增量额度。
- 客户端维护累计已发送样本 `sent`。只在 `sent + 当前帧样本数 <= audio_send_limit` 时发送。限额用尽则等待新 `flow`，不等待新文字；旧流控事件不重复增加额度，不重传已发送帧。
- 服务端在入队前同样检查 `received + 当前帧样本数 <= audio_send_limit`，不信任客户端自律；超过即按 `FLOW_CONTROL_EXCEEDED` 终止本会话，不继续缓存。
- 服务端至少每 160 ms 合并发布一次发生变化的进度，进度未变可不发。句尾、结束、取消和保活控制不消耗音频额度，不因音频额度为零而停止读取控制。
- 客户端未发送缓冲最多 2 秒。达到上限且不能继续接收新采集音频时，明确停止本轮并提示积压，发 `cancel`，保留已收到文字；不静默丢音频。服务端超额输入只结束违规会话。
- 会话总采集时间最多 3600 秒。建立后任意有效输入刷新通信活动时间；客户端每 10 秒发一次 `keepalive`，30 秒无输入结束本会话。保活不触发推理或修改模型策略。

## 控制与文字

控制为 JSON，`after_audio_samples` 等于发出该控制前累计已发送样本；服务端验证它等于此前在本连接接受的样本总数。

```json
{"type":"flush","after_audio_samples":5120}
{"type":"finish","after_audio_samples":5120}
{"type":"cancel"}
{"type":"keepalive"}
```

- `flush` 按本路音频顺序补齐当前句，然后继续本会话；不等待另一会话，也不阻止后续音频接收。
- `finish` 停止接收新音频，处理本路此前音频与尾部，发 `final` 后正常关闭。
- `cancel` 立即关闭本地文字提交通道；服务端丢弃本路待处理工作、取消本路在途请求，尽力发 `final`，`reason=cancelled, complete=false`。客户端取消后忽略包括该终止快照在内的迟到文字。
- `keepalive` 返回同名事件。重复 `flush` 且无新音频不重复生成文字；终止幂等，终止后的音频拒绝。V1 不需要客户端重发任何控制或音频。

```json
{"type":"partial","server_instance_id":"boot-example","model_generation":"load-example","session_id":"session-example","seq":3,"text":"测试文字。","pending":"下一","audio_processed_samples":5120}
{"type":"final","server_instance_id":"boot-example","model_generation":"load-example","session_id":"session-example","seq":4,"text":"测试文字。下一句。","pending":"","audio_processed_samples":5120,"reason":"finished","complete":true}
```

- `text` 为累计固定全文，`pending` 为尚可修订的候选；只把 `text` 的新增后缀用于输入。
- 最终全文即使未变，仍发送新序号的 `final`。失败使用终止 `error`，不再发送表示成功的 `final`。
- 去重键为实例、代次、会话及序号。旧序号忽略，新的固定全文必须以本地已收到的固定全文为前缀；不满足则停止自动填入并保留原文。
- 手机分别保存已收到固定全文和已成功填入的位置。目标变化、取消或 App 重启后不自动补贴；此契约不承诺外部输入框任意故障下严格 exactly-once。

## 错误、模型管理与断线

```json
{"type":"error","server_instance_id":"boot-example","model_generation":"load-example","session_id":"session-example","seq":5,"code":"MODEL_CHANGED","message":"PC 已切换模型，本轮已结束","retryable":false,"retry_after_ms":null,"text":"测试文字。","pending":"","complete":false}
```

| 错误码 | HTTP / WS close | 处理 |
| --- | --- | --- |
| `UNAUTHORIZED` | 401 / 1008 | 无效或已吊销凭据；先在 PC 处理，不自动重试录音 |
| `FORBIDDEN` | 403 / 1008 | 缺少权限，不重试 |
| `MODEL_NOT_READY` | 409 / 1008 | 等 PC 加载完成；手机不预热 |
| `MODEL_UNSUPPORTED` | 409 / 1008 | 提示当前模式未支持；手机不切模型 |
| `MODEL_CHANGED` | 409 / 1008 | 旧会话结束；重新查询后由用户开始新会话 |
| `CAPACITY_EXCEEDED` | 429 / 1013 | 暂无移动名额，建议 2000 ms 后重查能力 |
| `INVALID_MESSAGE` | 400 / 1008 | 协议、音频或控制格式错误 |
| `FLOW_CONTROL_EXCEEDED` / `RATE_LIMITED` | 429 / 1008 | 仅终止违规会话 |
| `SESSION_TIMEOUT` / `SESSION_LIMIT` | 408 / 1008 | 保留本地文字，用户可重新开始 |
| `SERVICE_UNAVAILABLE` | 503 / 1011 | 服务故障，建议 2000 ms 后重查能力 |

HTTP 列用于未升级时的失败映射；已经建立 WS 后尽力发送结构化 `error` 再关闭。正常完成/取消为 close 1000。终止错误中的全文只含已经固定部分，不强行识别未完成尾部；建立前错误 `text` 为空。所有事件不含 token，常规日志不记录音频或转写正文。

PC 录音期间保留模型管理保护。PC 未录音而手机活动时，PC 明确切换/卸载会终止手机会话，分别给出 `MODEL_CHANGED` / `MODEL_NOT_READY`，安全停止旧工作后执行管理操作。移动取消、断线和超时均不得卸载共享模型。自动卸载仅遵循用户原有策略，在全部会话空闲后开始计时。

V1 断线即结束旧会话，不恢复旧 ASR 状态、不自动重发音频、不自动开始录音。手机保留本地已收到的固定文字并提示尾部可能未完成，不能保证断线时收到最终事件。只有用户主动开始新会话才恢复听写。状态诊断可按 2/4/8 秒有界重试三次；凭据/权限/TLS 身份错误不自动重试。客户端能确认 Tailnet 未连接时才细分原因，否则显示连接失败；TLS 身份失败单独提示。

## 交付门槛

服务端提供 `tests/e2e_concurrent.py`、`tests/e2e_mobile_lifecycle.py`，分别检查实时双流、GPU资源及取消/权限/卸载隔离，命令见 [部署文档](mobile-deployment.md)。服务端通过不等于 Android 已接入；Pocket 完成客户端后还需验证真实麦克风、跨网络和输入框填入。

共同验收必须使用真实采集时钟的双端不同测试语音，持续至少 10 分钟；两端各自持续出字、积压不持续增长，无串流或重复误填。覆盖各端 finish/cancel/断线、慢接收/流控、旧事件、PC 切换、凭据越权与吊销、代次竞态及跨网络 Tailnet。双路性能、真实手机填入和权限隔离全部有证据，才算完成。

并发实现按 [技术研究](concurrency-research-2026-10-02.md) 的 AsyncLLM 路线执行。以上窗口和超时为 V1 初始默认值；兼容的数值调整通过能力查询返回，协议语义不变，不需重新讨论产品原则。
