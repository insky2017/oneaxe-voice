# 接口与运行说明

本页主要说明本机管理与旧协议。本机双向 V1 `WS /api/dictation/v1/stream` 支持 `r2t2` 与 `qwen-stream`，仍需本机 PC Bearer；其 `start` 为 `{"type":"start","protocol_version":1,"mode":"r2t2","audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1}}`，使用 Qwen 时将 `mode` 改为 `qwen-stream`。R2T2 桌面 F8 使用本机 V1，Qwen F8 保留旧 WS transport。

远端统一先查询 `GET /api/mobile/v1/capabilities`，再连接 `WS /api/mobile/v1/dictation/stream`，按就绪能力加入当前 Qwen 流式或 R2T2，不按模型选择路径。移动 `start` 不传 `mode`，须携带预期实例与模型代次；`vad` 不支持远端。身份绑定、额度、事件和错误规则见 [移动接口 V1](mobile-api-v1.md)，PC V1 复用这些流式规则，不需要 `expected_*` 字段。凭据与 Tailnet TLS 入口见 [部署文档](mobile-deployment.md)。

V1 发送额度取实际 `ready` / `flow` 的 `audio_send_limit`；能力查询的 `flow.window_samples` 供开始前配置。R2T2 默认窗口 32000 样本，Qwen 按 `max(configured_window, 32000 + max_frame_bytes / 2)` 计算，默认 34560 样本。客户端不得硬编码某个模型的窗口，也不得把每条 flow 当作新增余额；未推理音频缓冲不增加 `audio_processed_samples`，真实已消费数字静音会增加，flush 后计数不清零。

## 当前运行方式

以下命令均在仓库的 `server/` 目录执行。服务监听地址为 `http://127.0.0.1:8097`。客户端安装与使用见 [Linux 客户端](../../clients/linux/README.md) 和 [Android 客户端](../../clients/android/README.md)。

```bash
./bin/oneaxe-voice health
./bin/oneaxe-voice status
./bin/oneaxe-voice transcribe /absolute/path/recording.wav
./bin/oneaxe-voice transcribe /absolute/path/recording.wav --json
./bin/oneaxe-voice transcribe /absolute/path/recording.wav --output /absolute/path/result.txt
```

可使用本机验证样本：

```bash
./bin/oneaxe-voice transcribe ./work/smoke-zh-10s.wav
```

`work/` 中的本机验证文件不随仓库提供，也可使用自己的录音。首次识别和卸载后的识别须等待模型加载，实际耗时随系统缓存与 GPU 负载变化；测量结果见 [验证记录](validation.md)。

输入须为 16-bit PCM WAV、0.1–60 秒、8–48 kHz、单/双声道、最多 12 MiB。可先用 ffmpeg 转换已有录音：

```bash
ffmpeg -nostdin -i /absolute/path/input.m4a -t 60 -ac 1 -ar 16000 -c:a pcm_s16le /absolute/path/recording.wav
```

该命令只取前 60 秒，不会自动处理余下音频。

## HTTP 契约

| 方法与路径 | 鉴权 | 用途 |
| --- | --- | --- |
| `GET /health` | 无 | HTTP 存活检查；不加载或验证模型 |
| `GET /api/dictation/status` | Bearer | 模型加载、忙碌、设备和资源配置 |
| `POST /api/dictation/warmup` | Bearer | 选择并预热 `vad` CUDA 模型；无音频请求体 |
| `POST /api/dictation/prepare` | Bearer | JSON `mode` 选择并预热 `vad` / `qwen-stream` / `r2t2` |
| `POST /api/dictation/unload` | Bearer | 立即释放本服务模型；忙碌时返回 429，不排队等待 |
| `POST /api/dictation/policy` | Bearer | JSON `auto_unload` 布尔值；启用 120 秒空闲卸载或保持常驻 |
| `WS /api/dictation/stream` | Bearer | 持续 PCM 输入与累计稳定文本、候选字幕 |
| `WS /api/dictation/v1/stream` | PC Bearer | 本机 Qwen / R2T2 双向 V1；`start` 必须传 `mode` |
| `GET /api/mobile/v1/capabilities` | 设备 Bearer | 查询已就绪模型、可开始状态与动态音频额度 |
| `WS /api/mobile/v1/dictation/stream` | 设备 Bearer | 远端 Qwen / R2T2 双向 V1；绑定代次、不传 `mode` |
| `POST /api/dictation/transcribe` | Bearer | multipart 的 `file` 字段上传 WAV |

令牌由 `./bin/oneaxe-voice init` 创建，重复执行不会覆盖现有令牌。客户端从 `runtime/client.token` 读取，不需要复制到命令行。

预热成功返回 `{"model_loaded": true, "device": "cuda:0"}` 及完整状态；PC 占用时返回 429，GPU 不可用时返回 503。桌面启动和点击模式时发起预热；R2T2 的 F8 会等模型和 V1 会话 ready 后才开始采集，避免把整段冷启动音频堆入有限缓冲。

示例 Python 客户端：

```python
from pathlib import Path
import httpx

root = Path(__file__).resolve().parent
token = (root / 'runtime/client.token').read_text().strip()
with httpx.Client(
    base_url='http://127.0.0.1:8097',
    headers={'Authorization': 'Bearer ' + token},
    trust_env=False, timeout=180,
) as client:
    with Path('/absolute/path/recording.wav').open('rb') as audio:
        response = client.post(
            '/api/dictation/transcribe',
            files={'file': ('recording.wav', audio, 'audio/wav')},
        )
    response.raise_for_status()
    print(response.json()['text'])
```

实际热推理响应示例：

```json
{
  "request_id": "9036fbaf-3c5c-428a-b412-b915547e9cc2",
  "text": "大家好，今天是二零二六年九月二十六日，丙午年。",
  "language": "Chinese",
  "model": "Qwen3-ASR-1.7B",
  "device": "cuda:0",
  "gpu_name": "NVIDIA GeForce RTX 4090",
  "pid": 219265,
  "audio_seconds": 10.0,
  "inference_performed": true,
  "peak_allocated_mib": 3982.1,
  "timing_ms": {
    "normalize": 112.73,
    "model_load": 0.19,
    "inference": 818.78,
    "total": 931.7
  }
}
```

PID 和耗时随运行变化。`total` 为服务端开始处理至推理完成的时间，不包含客户端上传和响应传输。`peak_allocated_mib` 是 PyTorch 分配峰值，不是 nvidia-smi 显示的进程总量。数字静音返回 `text: ""`、`inference_performed: false`、`skipped_reason: "digital_silence"`、`device: null`，只包含总耗时。

### 模型状态与策略

状态的 `state` 为 `unloaded`、`loading`、`transcribing`、`ready`、`unloading` 或 `error`；`busy` 表示任一会话或本机管理操作占用，`pc_busy` 表示 PC 的管理保护。手机单独活动时 `busy=true, pc_busy=false`，PC 可主动切换/卸载并结束手机。稳听（`vad`）推理显示 `transcribing`；流式会话中 `state` 仍可为 `ready`。`mode`、`model_loaded` 与 `device` 表示实际模型；`server_instance_id`、`model_generation` 绑定服务启动与模型加载代次。GPU 名称及上次峰值可能在卸载后保留，不能据此判断仍驻留。

`allocated_mib` 和 `reserved_mib` 在推理完成或卸载时采样，分别表示 PyTorch 活跃分配和分配器保留量；加载和推理进行中，它们仍可能是上一次采样值。两者不包含 CUDA 上下文等额外占用，进程总量需用 `nvidia-smi` 核对。

`auto_unload` 与 `idle_seconds` 显示当前策略。`POST /api/dictation/policy` 接受 `{"auto_unload": true}` 启用 120 秒空闲卸载，或 `{"auto_unload": false}` 保持常驻；字段只接受 JSON 布尔值。策略原子保存到权限为 `0600` 的 `runtime/model-policy.json`，重启 API 后恢复，优先于 `ONEAXE_VOICE_IDLE_SECONDS`。切换策略不重启或主动加载模型，可以在录音中操作。

空闲计时从最近一次预热、转写完成或全部流式会话结束后开始，检查周期为 5 秒。状态查询不刷新计时，也不触发加载。PC 流式会话整轮受管理保护，远端有独立租约；任一路活动时都不会自动卸载。稳听在两次片段请求之间的长静音期间可能卸载，下段识别会重新加载。

`POST /api/dictation/unload` 在已有加载、本机推理或 PC 流式会话时返回 429；只有远端活动时，PC 主动卸载会结束远端。重复卸载空模型仍成功。手动卸载后，桌面轮询不会重新加载；点击模式、立即加载、F8 或新的 API 预热/识别请求可重新加载。`prepare`、`unload` 和 `policy` 成功时都返回当前模型状态。所有接口都仅操作本服务的实例。

### HTTP 错误

| HTTP 状态 | 含义与处理 |
| --- | --- |
| `400` | 无效 Content-Length 或无法解析的 multipart |
| `401` | 缺少或错误的本机令牌 |
| `403` | 客户端不是 loopback 地址 |
| `413` | 文件或请求体超过限制 |
| `422` | WAV 无效、规格不支持、缺少 `file`、模式无效或 `auto_unload` 不是布尔值 |
| `429` | 已有请求在处理；`Retry-After: 2`，等待后重试 |
| `503` | CUDA、本地模型目录或显存预算不可用；检查 status 和日志 |
| `500` | 其他识别故障；检查服务日志 |

## 流式协议

仅接受 loopback 客户端且必须携带 Authorization header；拒绝带 Origin 的浏览器连接。音频无需经过代理。

1. 连接 `/api/dictation/stream`，发送 `{"mode":"qwen-stream"}` 或 `{"mode":"r2t2"}`。
2. 等待 `type: "ready"`。首次模型加载可能需要约 40 秒。
3. 发送 16 kHz 单声道 PCM16 小端二进制块，每块 1–32000 个采样。客户端通常每 160 ms 送入，等待该块响应再送下一块。
4. `partial` 返回整轮累计冻结 `text`、尚未冻结的后缀 `pending`、最近 600 字的 `preview`（冻结前文与候选后缀组合）、递增 `sequence`、`device`、`audio_seconds`、`window_seconds` 和 `inference_ms`。只按 `text` 差量输入；`pending` 不可直接粘贴。
5. 停顿时发送文本 `flush`：调用官方结束接口补尾，返回 `partial`，保留整轮文字和模型实例，重建本句流式状态。后续音频作为下一句继续处理。`audio_seconds` 仅统计实际发送的 PCM，桌面的录音时长另包含被过滤的空闲静音。
6. 空闲静音期间每 10 秒发送文本 `keepalive`，返回 `{"type":"keepalive"}`，不调用模型、不增加 sequence。超过 30 秒没有应用消息会关闭会话。
7. 发送文本 `finish` 获取 `final` 并结束本轮；紧接 `flush` 且没有新音频时不会重复生成。发送 `cancel` 或断开只取消本会话；Qwen 与 R2T2 均可中止本路在途模型请求，不关闭共享模型。

本机只允许一个 PC 流式会话，Qwen 流式与 R2T2 均可另接一个独立的移动 V1 会话。两者都经 `concurrent_worker`，每会话独立 decoder，共享一份 AsyncLLM；Qwen 默认 512 MiB KV、R2T2 默认 1 GiB KV，两者默认双 seq 与 `[1, 2]` decode graphs，保留 4 CPU 线程与子进程 `OMP_WAIT_POLICY=PASSIVE`。PC 录音整轮受到保护，其间切换、卸载、WAV 识别和第二个 PC 会话返回忙碌；状态查询和策略修改仍可使用。正常结束、取消和断线只清理本路状态，保留共享模型。音频总量最多 60 分钟。固定文字前缀冲突时报错并保留已提交结果；不会回删目标应用内容。旧 WS 的 `error.detail` 为故障说明，新 V1 使用稳定 `code` 与 `message`。

桌面私有 socket 的 `ui` 操作另提供 `pending_text`（候选）、`fixed_text`（模型已固定）、`committed_text`（已成功执行发送或复制操作）和 `queued_text`（固定但尚未发送的精确差量），各截取末 600 字。`delivery_state` 为 `idle`、`queued`、`pasted` 或 `copied`；菜单暂停期间只更新固定文字，不提前宣称已发送。`pasted` 说明已发出粘贴按键，不能据此保证任意目标应用已接收。公开状态不含上述正文。

## 服务操作

```bash
systemctl --user status oneaxe-voice.service
systemctl --user start oneaxe-voice.service
systemctl --user stop oneaxe-voice.service
journalctl --user -u oneaxe-voice.service -n 50 --no-pager
```

在 `server/` 工程目录运行 `./bin/install-service`，安装器会把该工程的真实路径写入 `~/.config/systemd/user/oneaxe-voice.service`，再执行 `systemctl --user daemon-reload`。路径含空格时会正确转义。安装器不会启动服务；需要运行时再执行 `systemctl --user start oneaxe-voice.service`。修改模板后重新运行安装器，再按需重启本服务。停止服务即可完全释放该进程的 GPU 资源；不需要操作 VPlus。

前台调试使用 `./bin/serve`，先停止同端口的用户服务。保持单个 Uvicorn worker，多 worker 会加载多个模型并使进程内互斥锁失去全局限流效果。

## 环境重建

在 `server/` 下准备独立的 `.venv`，按本机 CUDA 与 Python 条件安装依赖。也可从兼容的已有环境克隆，后续依赖操作仅针对 `.venv`。两个流式模式使用独立的 `.venv-stream`，安装入口见 [三模式说明](modes.md)。环境就绪后运行 `./bin/oneaxe-voice init` 初始化本机令牌。

已验证的运行依赖包括 Python 3.11、FastAPI 0.128.0、Starlette 0.50.0、httpx 0.28.1、torch 2.10.0、qwen-asr 0.0.6、python-multipart 0.0.22，以及系统 ffmpeg 和 NVIDIA CUDA 驱动。`pyproject.toml` 描述应用依赖；新机器须单独验证 CUDA 可用性。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ONEAXE_VOICE_MODEL_DIR` | `~/tools/models/Qwen3-ASR-1.7B` | 既有模型目录；支持 `~` 展开 |
| `ONEAXE_VOICE_RUNTIME_DIR` | 项目下 `runtime` | 令牌和临时录音目录 |
| `ONEAXE_VOICE_API_URL` | `http://127.0.0.1:8097` | 桌面控制器的 loopback API 地址，独立测试可换端口 |
| `ONEAXE_VOICE_R2T2_MODEL_DIR` | `~/tools/models/Confucius4-R2T2` | R2T2 权重目录 |
| `ONEAXE_VOICE_STREAM_PYTHON` | 项目下 `.venv-stream/bin/python` | 独立流式解释器 |
| `ONEAXE_VOICE_CUDA_DEVICE` | `0` | CUDA 设备索引 |
| `ONEAXE_VOICE_MEMORY_FRACTION` | `0.25` | PyTorch 分配器显存比例上限 |
| `ONEAXE_VOICE_IDLE_SECONDS` | `0` | 未保存菜单策略时的初始空闲阈值；`0` 表示常驻，保存的策略优先 |

改变 runtime 目录时，服务和客户端必须使用相同配置，并在目标目录初始化令牌。服务预设 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；本地 HTTP 客户端忽略代理环境变量。
