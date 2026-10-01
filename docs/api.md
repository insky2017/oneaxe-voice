# 接口与运行说明

## 当前运行方式

以下命令以仓库位于 `~/tools/oneaxe-voice` 为例；仓库可以放在其他目录，包括带空格的路径。服务监听地址为 `http://127.0.0.1:8097`。文中的已运行状态和性能测量来自旧机器，仅作历史记录。

```bash
cd ~/tools/oneaxe-voice
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

`work/` 为未纳入 Git 的本机验证文件。首次识别和空闲卸载后的识别要等待模型加载；本次首次启动测得约 18.66 秒，模型在显存时约 0.93 秒，均为 10 秒音频的样本数据。

修复后复测：进程首次识别约 16.95 秒、热推理约 0.80 秒；空闲卸载后在同一进程重新加载并识别约 3.60 秒。实际耗时随系统缓存与 GPU 负载变化。

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
| `POST /api/dictation/warmup` | Bearer | 提前加载现有 CUDA 模型；无音频请求体 |
| `POST /api/dictation/prepare` | Bearer | JSON `mode` 选择并预热 `vad` / `qwen-stream` / `r2t2` |
| `WS /api/dictation/stream` | Bearer | 持续 PCM 输入与累计稳定文本、候选字幕 |
| `POST /api/dictation/transcribe` | Bearer | multipart 的 `file` 字段上传 WAV |

令牌由 `./bin/oneaxe-voice init` 创建，重复执行不会覆盖现有令牌。客户端从 `runtime/client.token` 读取，不需要复制到命令行。

预热成功返回 `{"model_loaded": true, "device": "cuda:0"}`；与转写使用同一模型和互斥锁，忙碌时返回 429，GPU 不可用时返回 503。桌面持续听写在 F8 开始时发起预热，采集同时进行。

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

状态的 `state` 为 `unloaded`、`loading`、`transcribing` 或 `ready`；`busy` 表示本次处理占用锁。`model_loaded` 与 `device` 表示当前模型状态。GPU 名称及上次峰值可能在卸载后保留，不能据此判断模型仍驻留。

`allocated_mib` 和 `reserved_mib` 在推理完成或卸载时采样，分别表示 PyTorch 活跃分配和分配器保留量；加载和推理进行中，它们仍可能是上一次采样值。两者不包含 CUDA 上下文等额外占用，进程总量需用 `nvidia-smi` 核对。

| HTTP 状态 | 含义与处理 |
| --- | --- |
| `400` | 无效 Content-Length 或无法解析的 multipart |
| `401` | 缺少或错误的本机令牌 |
| `403` | 客户端不是 loopback 地址 |
| `413` | 文件或请求体超过限制 |
| `422` | WAV 无效、规格不支持或缺少 `file` |
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
7. 发送文本 `finish` 获取 `final` 并结束本轮；紧接 `flush` 且没有新音频时不会重复生成。发送 `cancel` 或断开取消会话，已开始的 GPU 操作完成后回收实例。

流式整轮占用引擎，其间预热、切换、WAV 识别返回忙碌，不抢占录音。音频总量最多 60 分钟。最终文字前缀冲突时报错并保留已提交结果；不会回删目标应用内容。WS `error.detail` 为故障说明。

桌面私有 socket 的 `ui` 操作另提供 `pending_text`（候选）、`fixed_text`（模型已固定）、`committed_text`（已成功执行发送或复制操作）和 `queued_text`（固定但尚未发送的精确差量），各截取末 600 字。`delivery_state` 为 `idle`、`queued`、`pasted` 或 `copied`；菜单暂停期间只更新固定文字，不提前宣称已发送。`pasted` 说明已发出粘贴按键，不能据此保证任意目标应用已接收。公开状态不含上述正文。

## 服务操作

```bash
systemctl --user status oneaxe-voice.service
systemctl --user start oneaxe-voice.service
systemctl --user stop oneaxe-voice.service
journalctl --user -u oneaxe-voice.service -n 50 --no-pager
```

在仓库目录运行 `./bin/install-service`，安装器会把当前仓库的真实路径写入 `~/.config/systemd/user/oneaxe-voice.service`，再执行 `systemctl --user daemon-reload`。路径含空格时会正确转义。安装器不会启动服务；需要运行时再执行 `systemctl --user start oneaxe-voice.service`。修改模板后重新运行安装器，再按需重启本服务。停止服务即可完全释放该进程的 GPU 资源；不需要操作 VPlus。

前台调试使用 `./bin/serve`，先停止同端口的用户服务。保持单个 Uvicorn worker，多 worker 会加载多个模型并使进程内互斥锁失去全局限流效果。

## 环境重建

旧机器曾用以下方式创建独立环境；这是该机器的历史配置示例，不是可直接照搬到任意机器的环境路径。新环境应按本机 CUDA 与 Python 条件单独准备：

```bash
conda create \
  --prefix .venv \
  --clone ~/tools/miniconda3/envs/qwen3-asr \
  --offline --yes
cd ~/tools/oneaxe-voice
./bin/oneaxe-voice init
```

环境约 9.8 GB，已验证 torch 和 qwen_asr 从本项目 `.venv` 导入。运行依赖包括 Python 3.11、FastAPI 0.128.0、Starlette 0.50.0、httpx 0.28.1、torch 2.10.0、qwen-asr 0.0.6、python-multipart 0.0.22，以及系统 ffmpeg 和可用 NVIDIA CUDA 驱动。`pyproject.toml` 描述应用依赖；任意新机器的 pip 安装不保证自动匹配 CUDA 驱动，迁移时需要重新验证实际设备。

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
| `ONEAXE_VOICE_IDLE_SECONDS` | `120` | 空闲卸载阈值，`0` 表示禁用 |

改变 runtime 目录时，服务和客户端必须使用相同配置，并在目标目录初始化令牌。服务预设 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；本地 HTTP 客户端忽略代理环境变量。
