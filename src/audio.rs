use crate::protocol::SessionError;
use crate::transport::SessionHandle;
use std::collections::VecDeque;
use std::io::Read;
use std::os::fd::AsRawFd;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};
use webrtc_vad::{SampleRate, Vad, VadMode};

pub const SAMPLE_RATE: usize = 16_000;
pub const FRAME_MS: u32 = 20;
pub const FRAME_BYTES: usize = 640;
const PACKET_BYTES: usize = 5120;
const PREROLL_FRAMES: usize = 12;
const RUNNING: u8 = 0;
const STOP: u8 = 1;
const CANCEL: u8 = 2;

#[derive(Debug, Clone)]
pub struct CaptureConfig {
    pub source: Option<String>,
    pub pause_ms: u64,
    pub vad_mode: u8,
    pub min_dbfs: f64,
    pub max_seconds: u32,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self {
            source: None,
            pause_ms: 1000,
            vad_mode: 2,
            min_dbfs: -60.0,
            max_seconds: 3600,
        }
    }
}

impl CaptureConfig {
    fn validate(&self) -> Result<(), AudioError> {
        if !(200..=10000).contains(&self.pause_ms)
            || self.vad_mode > 3
            || !self.min_dbfs.is_finite()
            || !(-100.0..=0.0).contains(&self.min_dbfs)
            || !(10..=3600).contains(&self.max_seconds)
            || self.source.as_ref().is_some_and(|s| s.is_empty())
        {
            return Err(AudioError::InvalidConfig);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum AudioError {
    #[error("录音设置无效")]
    InvalidConfig,
    #[error("无法读取录音设备，请检查 pactl 和音频服务")]
    SourceUnavailable,
    #[error("所选麦克风不存在，请重新选择录音设备")]
    SourceMissing,
    #[error("所选麦克风已静音")]
    SourceMuted,
    #[error("无法启动录音，请检查 parec 和麦克风权限")]
    StartFailed,
    #[error("麦克风超过 3 秒未提供音频")]
    NoAudio,
    #[error("麦克风录音已中断")]
    Interrupted,
    #[error("无法处理麦克风音频")]
    VadFailed,
    #[error("音频发送拥堵或会话已关闭，本轮录音已停止")]
    SendFailed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Source {
    pub name: String,
    pub description: String,
    pub muted: bool,
}

#[derive(Debug, Clone)]
pub enum CaptureEvent {
    Started,
    Level {
        rms_dbfs: f64,
        voice_active: bool,
        seconds: f64,
        buffered_seconds: f64,
    },
    Stopped,
    Failed(AudioError),
}

/// Invoke only after the server's Ready event; no device access occurs on the caller thread.
pub struct CaptureHandle {
    control: Arc<AtomicU8>,
    active: Arc<AtomicBool>,
    session: SessionHandle,
}

impl CaptureHandle {
    pub fn start(
        config: CaptureConfig,
        session: SessionHandle,
        events: mpsc::Sender<CaptureEvent>,
    ) -> Result<Self, AudioError> {
        config.validate()?;
        let control = Arc::new(AtomicU8::new(RUNNING));
        let active = Arc::new(AtomicBool::new(true));
        let worker_control = control.clone();
        let worker_active = active.clone();
        let worker_session = session.clone();
        thread::Builder::new()
            .name("oneaxe-audio".into())
            .spawn(move || {
                let result = capture(&config, &worker_session, &worker_control, &events);
                worker_active.store(false, Ordering::Release);
                if let Err(error) = result {
                    if worker_control.load(Ordering::Acquire) == CANCEL
                        || !worker_session.delivery_gate().is_open()
                    {
                        let _ = events.send(CaptureEvent::Stopped);
                        return;
                    }
                    worker_session.fail_client(SessionError::CaptureFailed);
                    let _ = events.send(CaptureEvent::Failed(error));
                } else {
                    let _ = events.send(CaptureEvent::Stopped);
                }
            })
            .map_err(|_| AudioError::StartFailed)?;
        Ok(Self {
            control,
            active,
            session,
        })
    }

    pub fn stop(&self) {
        let _ = self
            .control
            .compare_exchange(RUNNING, STOP, Ordering::AcqRel, Ordering::Acquire);
    }

    pub fn cancel(&self) {
        self.session.cancel();
        self.control.store(CANCEL, Ordering::Release);
    }

    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
}

impl Drop for CaptureHandle {
    fn drop(&mut self) {
        if self.is_active() {
            self.cancel();
        }
    }
}

pub fn sources() -> Result<Vec<Source>, AudioError> {
    parse_sources(&pactl_output(&["list", "sources"])?)
}

fn pactl_output(args: &[&str]) -> Result<String, AudioError> {
    pactl_output_interruptible(args, None)
}

fn pactl_output_interruptible(
    args: &[&str],
    control: Option<&AtomicU8>,
) -> Result<String, AudioError> {
    if control.is_some_and(|control| control.load(Ordering::Acquire) != RUNNING) {
        return Err(AudioError::Interrupted);
    }
    let process = Command::new("pactl")
        .args(args)
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| AudioError::SourceUnavailable)?;
    let mut process = ReapedChild(process);
    let stdout = process
        .0
        .stdout
        .take()
        .ok_or(AudioError::SourceUnavailable)?;
    let reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(1024 * 1024)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if control.is_some_and(|control| control.load(Ordering::Acquire) != RUNNING) {
            return Err(AudioError::Interrupted);
        }
        match process.0.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(_)) | Err(_) => return Err(AudioError::SourceUnavailable),
            Ok(None) if Instant::now() >= deadline => return Err(AudioError::SourceUnavailable),
            Ok(None) => thread::sleep(Duration::from_millis(10)),
        }
    }
    let output = reader
        .join()
        .map_err(|_| AudioError::SourceUnavailable)?
        .map_err(|_| AudioError::SourceUnavailable)?;
    String::from_utf8(output).map_err(|_| AudioError::SourceUnavailable)
}

fn parse_sources(text: &str) -> Result<Vec<Source>, AudioError> {
    let mut result = Vec::new();
    let mut current: Option<Source> = None;
    let mut monitor = false;
    let mut saw_mute = false;
    let push = |source: Option<Source>, monitor: bool, saw_mute: bool, result: &mut Vec<Source>| {
        if let Some(source) = source {
            if !monitor && saw_mute && !source.name.is_empty() && !source.name.ends_with(".monitor")
            {
                result.push(source);
            }
        }
    };
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with("Source #") {
            push(current.take(), monitor, saw_mute, &mut result);
            current = Some(Source {
                name: String::new(),
                description: String::new(),
                muted: true,
            });
            monitor = false;
            saw_mute = false;
        } else if let Some(source) = current.as_mut() {
            if let Some(value) = line.strip_prefix("Name: ") {
                source.name = value.to_string();
            } else if let Some(value) = line.strip_prefix("Description: ") {
                source.description = value.to_string();
            } else if let Some(value) = line.strip_prefix("Mute: ") {
                saw_mute = value == "yes" || value == "no";
                source.muted = value != "no";
            } else if let Some(value) = line.strip_prefix("Monitor of Sink: ") {
                monitor |= value != "n/a";
            } else if line == "device.class = \"monitor\"" {
                monitor = true;
            }
        }
    }
    push(current, monitor, saw_mute, &mut result);
    Ok(result)
}

fn selected_source(config: &CaptureConfig, control: &AtomicU8) -> Result<Source, AudioError> {
    let sources = parse_sources(&pactl_output_interruptible(
        &["list", "sources"],
        Some(control),
    )?)?;
    let name = match &config.source {
        Some(name) => name.clone(),
        None => pactl_output_interruptible(&["info"], Some(control))?
            .lines()
            .find_map(|line| line.strip_prefix("Default Source: "))
            .map(str::to_string)
            .ok_or(AudioError::SourceUnavailable)?,
    };
    let source = sources
        .into_iter()
        .find(|s| s.name == name)
        .ok_or(AudioError::SourceMissing)?;
    if source.muted {
        return Err(AudioError::SourceMuted);
    }
    Ok(source)
}

struct ReapedChild(Child);

impl Drop for ReapedChild {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn capture(
    config: &CaptureConfig,
    session: &SessionHandle,
    control: &AtomicU8,
    events: &mpsc::Sender<CaptureEvent>,
) -> Result<(), AudioError> {
    let gate = session.delivery_gate();
    if control.load(Ordering::Acquire) == CANCEL || !gate.is_open() {
        return Ok(());
    }
    let source = match selected_source(config, control) {
        Ok(source) => source,
        Err(_) if control.load(Ordering::Acquire) == STOP && gate.is_open() => {
            session.finish().map_err(|_| AudioError::SendFailed)?;
            return Ok(());
        }
        Err(_) if control.load(Ordering::Acquire) == CANCEL || !gate.is_open() => return Ok(()),
        Err(error) => return Err(error),
    };
    if control.load(Ordering::Acquire) != RUNNING || !gate.is_open() {
        if control.load(Ordering::Acquire) == STOP && gate.is_open() {
            session.finish().map_err(|_| AudioError::SendFailed)?;
        }
        return Ok(());
    }
    let detector = WebRtcDetector::new(config.vad_mode);
    let mut endpoint = Endpoint::new(config, detector);
    let process = Command::new("parec")
        .args([
            "--raw",
            "--format=s16le",
            "--rate=16000",
            "--channels=1",
            "--latency-msec=50",
            "--client-name=OneAxe Voice Linux",
            "--stream-name=Remote Dictation",
        ])
        .arg(format!("--device={}", source.name))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| AudioError::StartFailed)?;
    let mut process = ReapedChild(process);
    let mut stdout = process.0.stdout.take().ok_or(AudioError::StartFailed)?;
    let started = Instant::now();
    let mut announced_start = false;
    let mut last_audio = started;
    let mut last_meter = started;
    let mut buffer = [0u8; 4096];
    loop {
        let action = control.load(Ordering::Acquire);
        if action == CANCEL || !gate.is_open() {
            return Ok(());
        }
        if action == STOP || started.elapsed() >= Duration::from_secs(config.max_seconds.into()) {
            break;
        }
        let mut pollfd = libc::pollfd {
            fd: stdout.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // A short poll keeps stop/cancel responsive even when the device stops producing samples.
        let ready = unsafe { libc::poll(&mut pollfd, 1, 100) };
        if ready < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(AudioError::Interrupted);
        }
        if ready == 0 {
            if last_audio.elapsed() >= Duration::from_secs(3) {
                return Err(AudioError::NoAudio);
            }
            continue;
        }
        if control.load(Ordering::Acquire) != RUNNING || !gate.is_open() {
            continue;
        }
        let size = stdout
            .read(&mut buffer)
            .map_err(|_| AudioError::Interrupted)?;
        if size == 0 {
            return Err(AudioError::Interrupted);
        }
        if !announced_start {
            let _ = events.send(CaptureEvent::Started);
            announced_start = true;
        }
        last_audio = Instant::now();
        send_endpoint(session, endpoint.feed(&buffer[..size])?)?;
        if last_meter.elapsed() >= Duration::from_millis(100) {
            let _ = events.send(CaptureEvent::Level {
                rms_dbfs: endpoint.last_dbfs,
                voice_active: endpoint.active,
                seconds: endpoint.total_frames as f64 * 0.02,
                buffered_seconds: session.buffered_samples() as f64 / SAMPLE_RATE as f64,
            });
            last_meter = Instant::now();
        }
    }
    // Stop the producer, then preserve PCM already in its pipe before padding the final frame.
    let _ = process.0.kill();
    let _ = process.0.wait();
    loop {
        if control.load(Ordering::Acquire) == CANCEL || !gate.is_open() {
            return Ok(());
        }
        let size = stdout
            .read(&mut buffer)
            .map_err(|_| AudioError::Interrupted)?;
        if size == 0 {
            break;
        }
        send_endpoint(session, endpoint.feed(&buffer[..size])?)?;
    }
    drop(stdout);
    drop(process);
    if control.load(Ordering::Acquire) == CANCEL || !gate.is_open() {
        return Ok(());
    }
    send_endpoint(session, endpoint.finish()?)?;
    session.finish().map_err(|_| AudioError::SendFailed)
}

fn send_endpoint(session: &SessionHandle, output: Vec<EndpointEvent>) -> Result<(), AudioError> {
    for event in output {
        match event {
            EndpointEvent::Pcm(pcm) => session.try_audio(pcm),
            EndpointEvent::Flush => session.flush(),
        }
        .map_err(|_| AudioError::SendFailed)?;
    }
    Ok(())
}

trait SpeechDetector {
    fn speech(&mut self, samples: &[i16]) -> Result<bool, AudioError>;
}

struct WebRtcDetector(Vad);

impl WebRtcDetector {
    fn new(mode: u8) -> Self {
        let mode = match mode {
            0 => VadMode::Quality,
            1 => VadMode::LowBitrate,
            2 => VadMode::Aggressive,
            _ => VadMode::VeryAggressive,
        };
        Self(Vad::new_with_rate_and_mode(SampleRate::Rate16kHz, mode))
    }
}

impl SpeechDetector for WebRtcDetector {
    fn speech(&mut self, samples: &[i16]) -> Result<bool, AudioError> {
        self.0
            .is_voice_segment(samples)
            .map_err(|_| AudioError::VadFailed)
    }
}

#[derive(Debug, PartialEq, Eq)]
enum EndpointEvent {
    Pcm(Vec<u8>),
    Flush,
}

struct Endpoint<D> {
    detector: D,
    pending: Vec<u8>,
    packet: Vec<u8>,
    preroll: VecDeque<Vec<u8>>,
    pause_frames: u64,
    min_dbfs: f64,
    quiet: u64,
    active: bool,
    total_frames: u64,
    last_dbfs: f64,
}

impl<D: SpeechDetector> Endpoint<D> {
    fn new(config: &CaptureConfig, detector: D) -> Self {
        Self {
            detector,
            pending: Vec::with_capacity(FRAME_BYTES + 4096),
            packet: Vec::with_capacity(PACKET_BYTES),
            preroll: VecDeque::with_capacity(PREROLL_FRAMES),
            pause_frames: config.pause_ms.div_ceil(FRAME_MS as u64),
            min_dbfs: config.min_dbfs,
            quiet: 0,
            active: false,
            total_frames: 0,
            last_dbfs: -120.0,
        }
    }

    fn drain(&mut self, output: &mut Vec<EndpointEvent>, tail: bool) {
        while self.packet.len() >= PACKET_BYTES {
            output.push(EndpointEvent::Pcm(
                self.packet.drain(..PACKET_BYTES).collect(),
            ));
        }
        if tail && !self.packet.is_empty() {
            output.push(EndpointEvent::Pcm(std::mem::take(&mut self.packet)));
        }
    }

    fn feed(&mut self, bytes: &[u8]) -> Result<Vec<EndpointEvent>, AudioError> {
        self.pending.extend_from_slice(bytes);
        let mut output = Vec::new();
        while self.pending.len() >= FRAME_BYTES {
            let frame: Vec<u8> = self.pending.drain(..FRAME_BYTES).collect();
            let samples: Vec<i16> = frame
                .chunks_exact(2)
                .map(|s| i16::from_le_bytes([s[0], s[1]]))
                .collect();
            let power =
                samples.iter().map(|s| (*s as f64).powi(2)).sum::<f64>() / samples.len() as f64;
            self.last_dbfs = 10.0 * (power.max(1e-12) / 32768.0f64.powi(2)).log10();
            let speech = self.last_dbfs >= self.min_dbfs && self.detector.speech(&samples)?;
            self.total_frames += 1;
            if !self.active {
                if self.preroll.len() == PREROLL_FRAMES {
                    self.preroll.pop_front();
                }
                self.preroll.push_back(frame);
                if !speech {
                    continue;
                }
                for frame in self.preroll.drain(..) {
                    self.packet.extend_from_slice(&frame);
                }
                self.active = true;
                self.quiet = 0;
            } else {
                self.packet.extend_from_slice(&frame);
                self.quiet = if speech { 0 } else { self.quiet + 1 };
            }
            self.drain(&mut output, false);
            if self.quiet >= self.pause_frames {
                self.drain(&mut output, true);
                output.push(EndpointEvent::Flush);
                self.active = false;
                self.quiet = 0;
            }
        }
        Ok(output)
    }

    fn finish(&mut self) -> Result<Vec<EndpointEvent>, AudioError> {
        let mut output = Vec::new();
        self.pending.truncate(self.pending.len() / 2 * 2);
        if !self.pending.is_empty() {
            self.pending.resize(FRAME_BYTES, 0);
            output.extend(self.feed(&[])?);
        }
        self.drain(&mut output, true);
        Ok(output)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct MarkerDetector;
    impl SpeechDetector for MarkerDetector {
        fn speech(&mut self, samples: &[i16]) -> Result<bool, AudioError> {
            Ok(samples[0] == 255)
        }
    }
    fn frame(marker: i16) -> Vec<u8> {
        marker.to_le_bytes().repeat(FRAME_BYTES / 2)
    }
    fn endpoint() -> Endpoint<MarkerDetector> {
        Endpoint::new(
            &CaptureConfig {
                min_dbfs: -100.0,
                ..Default::default()
            },
            MarkerDetector,
        )
    }
    fn pcm(events: &[EndpointEvent]) -> Vec<u8> {
        events
            .iter()
            .filter_map(|e| match e {
                EndpointEvent::Pcm(p) => Some(p.clone()),
                _ => None,
            })
            .flatten()
            .collect()
    }

    #[test]
    fn pulse_text_supports_old_sources_and_filters_monitors() {
        let text = "Source #0\n Name: alsa.mic\n Description: Internal Microphone\n Mute: no\n Monitor of Sink: n/a\nSource #1\n Name: sink.monitor\n Description: Monitor\n Mute: no\n Monitor of Sink: 0\nSource #2\n Name: usb.mic\n Description: USB DJI Microphone\n Mute: yes\n Monitor of Sink: n/a\n";
        let result = parse_sources(text).unwrap();
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].name, "alsa.mic");
        assert!(!result[0].muted);
        assert!(result[1].muted);
        assert_eq!(result[1].description, "USB DJI Microphone");
    }

    #[test]
    fn incomplete_or_monitor_property_sources_are_excluded() {
        let result = parse_sources("Source #0\n Name: weird\n Mute: no\n device.class = \"monitor\"\nSource #1\n Name: missing.mute\n").unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn preroll_keeps_exactly_240ms_including_onset() {
        let mut e = endpoint();
        for marker in 1..=20 {
            assert!(e.feed(&frame(marker)).unwrap().is_empty());
        }
        let mut events = e.feed(&frame(255)).unwrap();
        events.extend(e.finish().unwrap());
        let expected: Vec<u8> = (10..=20).flat_map(frame).chain(frame(255)).collect();
        assert_eq!(pcm(&events), expected);
        assert!(e.active);
        assert!(events.iter().all(|v| !matches!(v, EndpointEvent::Flush)));
    }

    #[test]
    fn one_second_pause_follows_all_audio_and_flushes_once() {
        let mut e = endpoint();
        let mut events = e.feed(&frame(255)).unwrap();
        for _ in 0..49 {
            events.extend(e.feed(&frame(0)).unwrap());
        }
        assert!(!events.contains(&EndpointEvent::Flush));
        events.extend(e.feed(&frame(0)).unwrap());
        assert_eq!(events.last(), Some(&EndpointEvent::Flush));
        assert_eq!(pcm(&events).len(), 51 * FRAME_BYTES);
        assert_eq!(
            events
                .iter()
                .filter(|e| matches!(e, EndpointEvent::Flush))
                .count(),
            1
        );
        assert!(!e.active);
        for _ in 0..100 {
            assert!(e.feed(&frame(0)).unwrap().is_empty());
        }
    }

    #[test]
    fn arbitrary_chunks_match_contiguous_feed_and_preserve_partial_tail() {
        let input = [frame(255), frame(255), frame(0), frame(255)[..126].to_vec()].concat();
        let mut contiguous = endpoint();
        let mut expected = contiguous.feed(&input).unwrap();
        expected.extend(contiguous.finish().unwrap());
        let mut chunked = endpoint();
        let mut actual = Vec::new();
        for chunk in input.chunks(173) {
            actual.extend(chunked.feed(chunk).unwrap());
        }
        actual.extend(chunked.finish().unwrap());
        assert_eq!(actual, expected);
        assert_eq!(pcm(&actual).len(), 4 * FRAME_BYTES);
        assert_eq!(
            &pcm(&actual)[3 * FRAME_BYTES..3 * FRAME_BYTES + 126],
            &input[3 * FRAME_BYTES..]
        );
        assert!(actual.iter().all(|e| match e {
            EndpointEvent::Pcm(bytes) => bytes.len() <= PACKET_BYTES && bytes.len() % 2 == 0,
            _ => true,
        }));
    }

    #[test]
    fn finish_discards_single_odd_byte_and_idle_silence() {
        let mut e = endpoint();
        assert!(e.feed(&[1]).unwrap().is_empty());
        assert!(e.finish().unwrap().is_empty());
        let mut e = endpoint();
        e.feed(&frame(0)).unwrap();
        assert!(e.finish().unwrap().is_empty());
    }

    #[test]
    fn renewed_speech_resets_pause_and_digital_silence_stays_idle() {
        let mut e = endpoint();
        e.feed(&frame(0)).unwrap();
        assert!(!e.active);
        e.feed(&frame(255)).unwrap();
        for _ in 0..49 {
            e.feed(&frame(0)).unwrap();
        }
        assert!(e
            .feed(&frame(255))
            .unwrap()
            .iter()
            .all(|e| !matches!(e, EndpointEvent::Flush)));
        assert_eq!(e.quiet, 0);
        assert!(e.active);
    }
}
