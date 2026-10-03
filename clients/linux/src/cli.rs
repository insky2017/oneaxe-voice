use anyhow::{bail, Context, Result};
use oneaxe_voice_linux::{
    credentials::{CredentialStore, Persistence},
    desktop,
    protocol::{DeviceToken, Endpoint, SessionEvent},
    settings::Config,
    transport,
};
use std::{
    io::{self, Read},
    path::Path,
    sync::mpsc,
    time::{Duration, Instant},
};

pub fn run(args: &[String]) -> Result<Option<i32>> {
    let Some(command) = args.get(1).map(String::as_str) else {
        return Ok(None);
    };
    match command {
        "--help" | "-h" => {
            println!("OneAxe Voice Linux\n\n无参数：打开客户端\n--background：后台托盘\n--toggle：开始 / 停止（默认 F9）\n--cancel：取消本轮\n--show：显示窗口\n--status：状态（不输出转写正文）\n--quit：退出\n--configure URL：配置 Tailnet HTTPS 服务器\n--microphone NAME：选择麦克风（default 表示系统默认）\n--shortcut KEY：保存并安装 GNOME 快捷键\n--import-token：从标准输入导入独立设备凭据到系统密钥环\n--check：查询服务器能力，不加载模型\n--install-hotkey：安装已配置快捷键，不覆盖冲突键\n--probe-pcm FILE：按真实时钟发送测试 PCM16LE/16kHz/mono 音频\n--probe-mic SECONDS：验证所选麦克风采集与识别\n--version：版本");
        }
        "--version" => println!("oneaxe-voice-linux {}", env!("CARGO_PKG_VERSION")),
        "--configure" => {
            let mut config = Config::load()?;
            config.server_url = Endpoint::parse(required(args, 2)?)?.origin().to_owned();
            config.save()?;
            println!("服务器地址已保存；凭据按服务器地址隔离。");
        }
        "--microphone" => {
            let mut config = Config::load()?;
            let value = required(args, 2)?;
            config.microphone = if value == "default" {
                None
            } else {
                Some(value.to_owned())
            };
            config.save()?;
            println!("麦克风配置已保存。");
        }
        "--shortcut" | "--install-hotkey" => {
            gtk::init().map_err(|_| anyhow::anyhow!("无法连接图形会话"))?;
            let mut config = Config::load()?;
            if command == "--shortcut" {
                config.shortcut = required(args, 2)?.to_owned();
            }
            desktop::install_hotkey(&config.shortcut, &std::env::current_exe()?)?;
            config.save()?;
            println!("快捷键 {} 已安装。", config.shortcut);
        }
        "--import-token" => {
            let config = Config::load()?;
            let endpoint = Endpoint::parse(&config.server_url)?;
            let mut secret = zeroize::Zeroizing::new(String::new());
            io::stdin().take(4097).read_to_string(&mut secret)?;
            if secret.len() > 4096 {
                bail!("设备凭据过长");
            }
            let token = DeviceToken::new(secret.trim().to_owned())?;
            let mut store = CredentialStore::new();
            match store.store(&endpoint, token)? {
                Persistence::Keyring => println!("设备凭据已存入系统密钥环。"),
                Persistence::MemoryOnly => bail!(
                    "密钥环不可用；CLI 导入未持久保存。请在图形客户端本次使用或解锁密钥环后重试。"
                ),
            }
        }
        "--check" => {
            let (endpoint, token) = load_connection()?;
            let runtime = tokio::runtime::Runtime::new()?;
            let caps = runtime.block_on(transport::capabilities(&endpoint, &token))?;
            println!(
                "{}",
                serde_json::json!({"protocol_version":caps.protocol_version,"model_id":caps.model_id,"mode":caps.mode,"model_state":caps.model_state,"ready":caps.ready,"stream_supported":caps.stream_supported,"can_start":caps.can_start,"unavailable_reason":caps.unavailable_reason.map(|v|v.to_string()),"mobile_slots_available":caps.mobile_slots_available,"max_sessions":caps.max_sessions})
            );
        }
        "--probe-pcm" => {
            probe(Some(Path::new(required(args, 2)?)), 0, args)?;
        }
        "--probe-mic" => {
            let seconds = required(args, 2)?.parse::<u64>()?;
            if !(1..=600).contains(&seconds) {
                bail!("采集时长须为 1–600 秒");
            }
            probe(None, seconds, args)?;
        }
        _ => return Ok(None),
    }
    Ok(Some(0))
}

fn required(args: &[String], index: usize) -> Result<&str> {
    args.get(index)
        .map(String::as_str)
        .context("缺少命令参数；运行 --help 查看用法")
}

fn load_connection() -> Result<(Endpoint, DeviceToken)> {
    let config = Config::load()?;
    let endpoint = Endpoint::parse(&config.server_url)?;
    let token = CredentialStore::new()
        .read(&endpoint)?
        .context("尚未配置设备凭据；请在设置中填写，或使用 --import-token 从标准输入导入")?;
    Ok((endpoint, token))
}

fn probe(path: Option<&Path>, seconds: u64, args: &[String]) -> Result<()> {
    let config = Config::load()?;
    let data = path.map(std::fs::read).transpose()?;
    if let Some(ref pcm) = data {
        if pcm.is_empty() || pcm.len() % 2 != 0 || pcm.len() > 19_200_000 {
            bail!("测试音频须为非空 PCM16LE，最长 600 秒");
        }
    }
    let runtime = tokio::runtime::Runtime::new()?;
    let (endpoint, token) = load_connection()?;
    let caps = runtime.block_on(transport::capabilities(&endpoint, &token))?;
    let (session, input) = transport::session_channel(&caps)?;
    let (tx, rx) = mpsc::channel();
    let task = runtime.spawn(transport::run_session(endpoint, token, caps, input, tx));
    let (capture_tx, capture_rx) = mpsc::channel();
    let mut capture = None;
    let mut feeder = None;
    let started = Instant::now();
    let mut ready_at = None;
    let mut first_text_ms = None;
    let mut fixed = String::new();
    let mut updates = 0u64;
    let mut max_buffered = 0;
    let mut max_lag = 0;
    let mut sent = 0;
    let mut processed = 0;
    let deadline =
        Duration::from_secs(data.as_ref().map_or(seconds, |p| p.len() as u64 / 32000) + 90);
    let result = (|| -> Result<()> {
        loop {
            if started.elapsed() > deadline {
                bail!("验证超时");
            }
            if let Some(at) = ready_at {
                if path.is_none()
                    && Instant::now().duration_since(at) >= Duration::from_secs(seconds)
                {
                    if let Some(ref c) = capture {
                        oneaxe_voice_linux::audio::CaptureHandle::stop(c);
                    }
                    ready_at = None;
                }
            }
            while let Ok(event) = capture_rx.try_recv() {
                if let oneaxe_voice_linux::audio::CaptureEvent::Failed(error) = event {
                    bail!("麦克风采集失败：{error}");
                }
            }
            let event = match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(e) => e,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(_) => bail!("事件通道已关闭"),
            };
            match event {
                SessionEvent::Ready(_) => {
                    ready_at = Some(Instant::now());
                    if let Some(pcm) = data.clone() {
                        let sender = session.clone();
                        feeder = Some(std::thread::spawn(move || -> Result<()> {
                            let clock = Instant::now();
                            let mut samples = 0u64;
                            for packet in pcm.chunks(5120) {
                                samples += packet.len() as u64 / 2;
                                let due = Duration::from_secs_f64(samples as f64 / 16000.);
                                if let Some(wait) = due.checked_sub(clock.elapsed()) {
                                    std::thread::sleep(wait);
                                }
                                sender.try_audio(packet.to_vec())?;
                            }
                            sender.finish()?;
                            Ok(())
                        }));
                    } else {
                        capture = Some(oneaxe_voice_linux::audio::CaptureHandle::start(
                            oneaxe_voice_linux::audio::CaptureConfig {
                                source: config.microphone.clone(),
                                pause_ms: config.pause_ms,
                                ..Default::default()
                            },
                            session.clone(),
                            capture_tx.clone(),
                        )?);
                    }
                }
                SessionEvent::Progress(p) => {
                    max_buffered = max_buffered.max(p.buffered_samples);
                    max_lag = max_lag.max(p.sent_samples.saturating_sub(p.processed_samples));
                    sent = p.sent_samples;
                    processed = p.processed_samples;
                }
                SessionEvent::Text(update) => {
                    if !update.fixed.is_empty() && first_text_ms.is_none() {
                        first_text_ms = Some(started.elapsed().as_millis());
                    }
                    if fixed != update.fixed {
                        updates += 1;
                    }
                    fixed = update.fixed;
                }
                SessionEvent::Finished {
                    update, complete, ..
                } => {
                    fixed = update.fixed;
                    if !complete {
                        bail!("服务端未正常完成");
                    }
                    break;
                }
                SessionEvent::Failed(error) => bail!("{error}"),
                SessionEvent::Cancelled => bail!("验证会话已取消"),
            }
        }
        Ok(())
    })();
    if result.is_err() {
        session.cancel();
        if let Some(ref c) = capture {
            c.cancel();
        }
    }
    let feeder_result = feeder
        .map(|thread| {
            thread
                .join()
                .map_err(|_| anyhow::anyhow!("音频测试线程异常"))
                .and_then(|value| value)
        })
        .unwrap_or(Ok(()));
    runtime
        .block_on(async { tokio::time::timeout(Duration::from_secs(5), task).await })
        .ok();
    let reap_deadline = Instant::now() + Duration::from_secs(5);
    while capture.as_ref().is_some_and(|c| c.is_active()) && Instant::now() < reap_deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    result?;
    feeder_result?;
    if capture.as_ref().is_some_and(|c| c.is_active()) {
        bail!("录音进程未在期限内退出");
    }
    let expected = args
        .iter()
        .position(|arg| arg == "--expect")
        .map(|i| required(args, i + 1))
        .transpose()?;
    let rejected = args
        .iter()
        .position(|arg| arg == "--reject")
        .map(|i| required(args, i + 1))
        .transpose()?;
    let normalized = fixed.to_lowercase();
    if expected.is_some_and(|value| !normalized.contains(&value.to_lowercase())) {
        bail!("识别结果未包含测试要求的关键文字");
    }
    if rejected.is_some_and(|value| normalized.contains(&value.to_lowercase())) {
        bail!("识别结果出现另一声道的测试文字");
    }
    println!(
        "{}",
        serde_json::json!({"complete":true,"source":if path.is_some(){"test_pcm"}else{"microphone"},"elapsed_ms":started.elapsed().as_millis(),"fixed_characters":fixed.chars().count(),"fixed_updates":updates,"first_fixed_ms":first_text_ms,"sent_samples":sent,"processed_samples":processed,"max_buffered_samples":max_buffered,"max_observed_lag_samples":max_lag,"expected_keyword_checked":expected.is_some(),"foreign_keyword_checked":rejected.is_some()})
    );
    Ok(())
}
