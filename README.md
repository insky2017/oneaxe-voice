# OneAxe Voice Linux

连接 OneAxe Voice 服务器的轻量 Linux 客户端。录音在本机，模型推理在服务器。默认快捷键 **F9**；原有本机服务的 **F8** 保留。

源码目录为 `~/work/touzi/OneAxe/oneaxe-voice/clients/linux`，与同一总目录下 `server/` 的服务端是两个独立 Git 仓库。目录约定及共享模型位置见 [项目总入口](../../README.md)，协议与模型控制边界见 [服务端 README](../../server/README.md)。历史验证记录中的旧路径保留为当时的安装位置。

- [使用与鉴权](docs/usage.md)
- [实现边界](docs/design.md)
- [验证记录](docs/validation.md)
- [重复输入调查与待审核修复](docs/duplicate-input-investigation.md)
- [界面与方案页截图](docs/validation-visual.md)

首版使用 Rust + GTK3，兼容 Ubuntu 20.04 / 24.04 的 GNOME X11。只连接 Tailnet HTTPS/WSS，公网接入留在规划中。客户端只使用现有移动 V1，不调用模型加载、切换或卸载接口。当前服务只提供一个远端名额，Linux 与 Android 共用此名额。

当前源码已支持 R2T2 与 Qwen 的统一移动 V1 契约。客户端根据服务端发布的协议、音频、流控、就绪和可开始能力工作，模型名称仅用于显示。安装版和真实 Qwen 复验状态见 [验证记录](docs/validation.md)。

## 构建和安装

需要 Rust 1.89+、GTK3 / D-Bus 开发包，以及 `parec`、`pactl`、`xdotool`、`xclip`、`xprop`（`x11-utils`）。构建依赖由 Cargo.lock 固定。

```bash
cd ~/work/touzi/OneAxe/oneaxe-voice/clients/linux
cargo build --release --locked
./scripts/install-user
oneaxe-voice-linux --show
```

安装脚本仅安装到当前用户目录，无需 root。发布 `.deb` 用 `./scripts/build-deb`。可分发的二进制应在 Ubuntu 20.04 基线构建，不能把较新 glibc 版本的构建直接用于旧系统。

```bash
cargo test --locked
```

默认不保存录音或转写历史。设备凭据只存系统密钥环；密钥环不可用时，图形客户端可在本次运行中使用，CLI 导入会明确失败。

日常听写前，在服务器原托盘选择已接入移动 V1 的模型并等其就绪；客户端“测试连接”显示当前模型和可用状态。新客户端的 F9 与服务器本机 F8 是两个入口；本机运行这个轻客户端也会占用远端名额。

已复现目标应用繁忙时的增量剪贴板覆盖风险，详见调查记录。当前串行化修复只解决手动复制与自动输入之间的命令交错；目标读取确认尚未实现，不能视为重复输入问题已完全解决。
