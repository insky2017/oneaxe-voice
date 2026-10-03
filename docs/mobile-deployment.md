# 移动入口部署与联调

协议以 [移动接口 V1](mobile-api-v1.md) 为准。本页提供实际命令；上线状态和测试范围见 [验证记录](validation.md)。安装代码不代表 HTTPS 入口已经启用。

## 证书和监听

在工程目录操作。先通过用户已经建立的 Tailscale 连接确认本机完整 DNS 名；脚本从 `tailscale status --json` 读取地址、DNS 和允许签发的证书域名，不使用通配监听。

```bash
./bin/configure-mobile --port 8097
```

如果提示证书签发权限不足，在本机交互终端执行：

```bash
./bin/configure-mobile --port 8097 --sudo-cert
```

`--sudo-cert` 只对一次 `tailscale cert` 提权，证书和密钥经捕获的 stdout 交还用户进程，不打印密钥，不让 root 写入项目目录。它不修改 Tailscale operator、不重启 Tailscale、不授予常驻 sudo 权限。证书与配置保存在私有 `runtime/tls/`、`runtime/mobile-listener.json`；私钥权限为 0600。配置失败不会发布新的监听设置。

普通签发模式每 12 小时尝试自动续期；`--sudo-cert` 模式需要用户每月重跑上述命令续期，不能假称后台已有 sudo 权限。运行服务每分钟检查证书更新，仅刷新后续 TLS 握手，不打断现有听写。

当前用户已有 Docker 使用权限且本地存在 `ubuntu:24.04` 时，可启用 Docker 签发和自动续期：

```bash
./bin/configure-mobile --port 8097 --docker-cert ubuntu:24.04
```

脚本先通过 `docker image inspect` 固定本地镜像的 `sha256` ID，配置仅保存该 ID；镜像不存在时失败，不拉取镜像。容器使用 root 身份调用静态链接的 `/usr/bin/tailscale`，仅只读绑定这个程序和 `/var/run/tailscale/tailscaled.sock` 文件 socket；不挂载 Docker socket 或主机目录。容器关闭网络、根文件系统只读、移除全部 Linux capabilities，禁止进程提权，并关闭容器日志驱动。证书与密钥通过捕获的 stdout 返回普通用户进程校验并保存，不显示 PEM。

Docker 模式每 12 小时使用固定镜像签发或续期，每分钟检查并重载已更新的证书。需保留该本地镜像、Docker 使用权限及原有 Tailscale 连接；`--docker-cert` 与 `--sudo-cert` 互斥。修改签发方式后，监听配置在下次 OneAxe Voice API 启动时生效；签发命令不创建系统服务、不重启 Tailscale。

第一次配置后，先检查 `./bin/oneaxe-voice desktop-status`，确认 PC 已停止录音，再重启 **OneAxe Voice API** 使第二个监听器生效。配置和签发命令本身不加载模型、不重启服务。

```bash
systemctl --user restart oneaxe-voice.service
```

默认部署地址为 `https://rtx4090.nase-stairs.ts.net:8097`，WS 使用 `wss://`。证书必须通过系统信任和该 DNS 名验证；不得用 `-k`、关闭验证或把此证书用于裸 IP。loopback 仍使用 `http://127.0.0.1:8097`；移动监听器仅开放移动 V1 路径。

## 设备凭据

本机管理员使用 CLI 配发。手机不能使用 PC 的 `runtime/client.token`。

```bash
./bin/oneaxe-voice device issue Pocket --token-file runtime/pocket.token
./bin/oneaxe-voice device list
./bin/oneaxe-voice device rotate CREDENTIAL_ID --token-file runtime/pocket-next.token
./bin/oneaxe-voice device revoke CREDENTIAL_ID
```

`--token-file` 只创建不存在的 0600 文件，终端仅显示设备元数据及文件路径。无该选项时 CLI 会显示本次配发的令牌，适用于用户手动录入，不用于 agent 日志。用户通过可信本机界面把令牌录入 Pocket；不要放入 Git、URL、报告或聊天。服务端仅保存摘要，`device list` 不回显令牌；吊销和轮换会终止旧凭据的移动会话。

对应本机管理端点为 `GET/POST /api/devices`、`POST /api/devices/{credential_id}/revoke` 和 `/rotate`。仅本机 PC 管理凭据可调用。设备具有 `voice.mobile.read`、`voice.mobile.stream`，无模型管理权限。

## Pocket 接入顺序

1. 用独立 Bearer 查询 `GET /api/mobile/v1/capabilities`，分别处理认证、TLS、未就绪、不支持与容量不足。诊断不得替用户加载模型或开始录音。
2. 用户主动开始时携带查询得到的实例和代次建立 V1 WS；等 `ready` 后采集 `16 kHz mono PCM16LE`。独立执行采集、额度控制下的发送和文字接收。
3. 每个 `text` 是累计固定全文，`pending` 只作候选显示；按身份和 `seq` 去重，前缀连续才追加差量。断线、取消和目标变更不自动补贴或重录。

## 可运行的服务端验收

Qwen / R2T2 的现行统一验证入口是 `tests/e2e_unified_stream.py`；它查询当前模型，不替客户端选择模型。脚本参数、官方适配依据与本轮证据见 [统一流式接口](unified-stream-2026-10-03.md)。Qwen 验证显式使用 `--max-processed-lag-seconds 2.5`，保留末段积压增长不超过 0.5 秒；客户端从 capabilities 获取 34560 样本默认窗口，不套用 R2T2 的固定数值。管理验证另需 `--allow-model-management`，且只允许独立 loopback 测试端口。

以下保留 R2T2 基线命令。仅在私有测试 API 和专门测试设备凭据上运行；脚本不会启动或重载模型，但要求 PC 事先把 R2T2 预热好。WAV 是两段内容不同的受控测试音频，不能使用用户私人听写录音。每路关键词必须在自身音频里出现而不在另一条音频里出现。

```bash
.venv/bin/python tests/e2e_concurrent.py \
  --url http://127.0.0.1:18096 \
  --pc-token-file /absolute/path/test-runtime/client.token \
  --mobile-token-file /absolute/path/test-runtime/mobile.token \
  --pc-audio /absolute/path/chinese-test.wav \
  --mobile-audio /absolute/path/english-test.wav \
  --pc-keyword '大家好' --mobile-keyword 'mobile' \
  --seconds 600 --start-order pc-first \
  --output work/concurrency-10min.json
```

可用 `--mobile-url https://HOST:PORT` 验证真实 TLS 入口，用 `--start-order mobile-first` 交换开始顺序。默认积压 p95 不超过 2 秒、后段相对前段 p95 增长不超过 0.5 秒，记录 GPU 占用、显存、功耗、首次固定文字和持续文字间隔。报告只含计数、时间、哈希和校验结果。

这项验收覆盖真实 GPU、实时音频时钟和两种身份的完整 WS 链路，不替代 Android 真机麦克风、跨网络连接及目标输入框填入验收。手机 App 侧由 Pocket 任务按 V1 实现后完成。
