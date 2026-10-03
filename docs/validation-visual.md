# 隔离视觉复核

日期：2026-10-03。范围：GTK3 主窗、设置窗及方案 HTML。此记录只证明隔离环境中的布局与文档交互，不替代本机和 e15l 的真实 X11、音频、快捷键、托盘、识别及文字交付验收。

## 环境与隔离

- GTK3 Broadway 后端，监听 `127.0.0.1:34010`，独立 D-Bus 与 XDG config/data/cache/runtime；浏览器视口 `1280×720`。
- 使用 `agent-browser` 操作 localhost 页面。未连接语音服务器，未录音，未填入设备凭据，未保存设置或应用快捷键。
- 被检客户端 GUI 为此前编译通过的版本；本次未修改 `src/ui.rs` 或 `src/tray.rs`，无需因本次复核重新编译。
- 隔离日志中的 inotify 配额、GVFS、Broadway/AppIndicator scale-factor 警告未阻止窗口渲染；它们不构成真实桌面托盘的验收结论。

## GTK3 窗口结果

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 主窗 | 非空渲染；状态、模型提示、开始/取消/复制可读；无真实转写或凭据 | [main.png](evidence/visual-2026-10-03/main.png) |
| 默认设置窗 | 默认内容尺寸 `600×560`，含装饰 canvas `652×649`；所有字段、三项开关、密钥环提示、测试连接与保存完整可见 | [settings-default-final.png](evidence/visual-2026-10-03/settings-default-final.png) |
| 缩小设置窗 | 高度减少 205px 后 canvas `652×444`；表单裁切进入滚动区，底部状态与按钮仍完整可见 | [settings-small-top.png](evidence/visual-2026-10-03/settings-small-top.png) |
| 表单滚动与固定底部 | 从地址输入框用 Tab 遍历到最底部“登录后启动”，字段及三项开关均可到达；状态约 y343、按钮 y361–395 在滚动前后不变 | [settings-small-bottom.png](evidence/visual-2026-10-03/settings-small-bottom.png) |

默认设置窗最初位于 `(100,100)`，外框底部超出 720px 视口少许；仅移动标题栏到顶部后取得完整窗口截图，未放大窗口。滚轮命令受到当前浏览器工具接口限制，实际滚动证据来自 GTK 焦点遍历。

## 方案页结果

检查地址：`http://127.0.0.1:33979/linux-client-proposal.html`。

- 四个页签均可切换，选中态随切换更新。截图：[工作框架](evidence/visual-2026-10-03/proposal-framework.png)、[工作流程](evidence/visual-2026-10-03/proposal-workflow.png)、[接口与消息](evidence/visual-2026-10-03/proposal-api.png)、[设置与首版范围](evidence/visual-2026-10-03/proposal-scope-full.png)。
- 当前文案明确首版仅 `*.ts.net` HTTPS/WSS；公网示例标为“仅规划 / 尚未实现”；Linux 默认 F9、服务器本机 F8 保留；采用 Rust + GTK3，并列出两台目标机的 GTK3 基线。
- 鉴权图包含每机独立签发、安全传递、Secret Service 保存、HTTP/WS Authorization 请求头、服务端摘要/状态/权限检查、权限范围及吊销/轮换。
- `architecture-map.html`、`mobile-api-v1.md`、`mobile-deployment.md`、`audio-pipeline.html` 四个本地链接均返回 HTTP 200。
- 页面保持“方案已审核 / 开发中”，未把隔离渲染或文档检查表述为双机真实识别交付完成。

## 尚需真实环境证据

真实 X11 快捷键、托盘菜单、字幕置顶、输入目标核对、凭据写入/重新读取、麦克风采集、服务端识别及双机文字交付由主验收记录覆盖。本记录未运行 GPU 测试，也未操作或停止正在进行的真实客户端。
