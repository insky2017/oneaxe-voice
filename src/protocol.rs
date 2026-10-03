use std::fmt;

use serde::{Deserialize, Serialize};
use thiserror::Error;
use url::Url;
use zeroize::Zeroizing;

pub const SAMPLE_RATE: u64 = 16_000;
pub const MAX_FRAME_BYTES: usize = 5_120;
pub const MAX_BUFFER_MS: u64 = 2_000;

#[derive(Clone)]
pub struct DeviceToken(Zeroizing<String>);

impl DeviceToken {
    pub fn new(value: impl Into<String>) -> Result<Self, SessionError> {
        let value = Zeroizing::new(value.into());
        if value.is_empty()
            || value.len() > 4096
            || !value.bytes().all(|byte| byte.is_ascii_graphic())
        {
            return Err(SessionError::InvalidToken);
        }
        Ok(Self(value))
    }

    pub fn expose_secret(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for DeviceToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("DeviceToken([REDACTED])")
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Endpoint {
    origin: String,
}

impl Endpoint {
    pub fn parse(value: &str) -> Result<Self, SessionError> {
        let parsed = Url::parse(value).map_err(|_| SessionError::InvalidEndpoint)?;
        let host = parsed.host_str().ok_or(SessionError::InvalidEndpoint)?;
        let original_path = value
            .split_once("://")
            .and_then(|(_, authority)| authority.find('/').map(|index| &authority[index..]));
        if parsed.scheme() != "https"
            || value.contains(['@', '\\'])
            || value.bytes().any(|byte| byte.is_ascii_whitespace())
            || original_path.is_some_and(|path| path != "/")
            || !host.ends_with(".ts.net")
            || host.len() <= ".ts.net".len()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.query().is_some()
            || parsed.fragment().is_some()
            || parsed.path() != "/"
            || parsed.port() == Some(0)
        {
            return Err(SessionError::InvalidEndpoint);
        }
        Ok(Self {
            origin: parsed.origin().ascii_serialization(),
        })
    }

    pub fn origin(&self) -> &str {
        &self.origin
    }

    pub(crate) fn capabilities_url(&self) -> String {
        format!("{}/api/mobile/v1/capabilities", self.origin)
    }

    pub(crate) fn stream_url(&self) -> String {
        format!("wss{}/api/mobile/v1/dictation/stream", &self.origin[5..])
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ServiceCode {
    Unauthorized,
    Forbidden,
    ModelNotReady,
    ModelUnsupported,
    ModelChanged,
    CapacityExceeded,
    InvalidMessage,
    FlowControlExceeded,
    RateLimited,
    SessionTimeout,
    SessionLimit,
    ServiceUnavailable,
    #[serde(other)]
    Unknown,
}

impl fmt::Display for ServiceCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::Unauthorized => "设备凭据无效或已吊销",
            Self::Forbidden => "设备凭据缺少所需权限",
            Self::ModelNotReady => "请等待 PC 模型加载完成",
            Self::ModelUnsupported => "服务端当前模型尚未接入移动 V1 流式协议",
            Self::ModelChanged => "PC 模型已变化，请重新查询后开始",
            Self::CapacityExceeded => "暂时没有可用的移动会话名额",
            Self::InvalidMessage => "语音协议消息无效",
            Self::FlowControlExceeded => "音频超过服务端流控额度",
            Self::RateLimited => "发送消息过于频繁",
            Self::SessionTimeout => "语音会话通信超时",
            Self::SessionLimit => "已达到本轮听写时长上限",
            Self::ServiceUnavailable => "语音服务暂不可用",
            Self::Unknown => "语音服务返回未知错误",
        })
    }
}

#[derive(Clone, Debug, Error, PartialEq)]
pub enum SessionError {
    #[error("服务地址必须为 HTTPS 的 Tailnet DNS 主机，可配置端口")]
    InvalidEndpoint,
    #[error("设备凭据为空或格式无效")]
    InvalidToken,
    #[error("服务端协议或音频能力不兼容")]
    InvalidCapabilities,
    #[error("{code}")]
    Remote {
        code: ServiceCode,
        retryable: bool,
        retry_after_ms: Option<u64>,
    },
    #[error("TLS 证书或服务身份验证失败")]
    TlsIdentity,
    #[error("连接失败，请检查 Tailnet 与服务地址")]
    ConnectionFailed,
    #[error("连接已断开，已收到文字保留，尾部可能未完成")]
    ConnectionLost,
    #[error("连接或服务响应超时")]
    Timeout,
    #[error("服务端消息格式无效")]
    InvalidMessage,
    #[error("服务端实例、模型代次或会话身份不一致")]
    IdentityMismatch,
    #[error("固定文字前缀不一致，已停止自动输入并保留原文")]
    TextPrefixMismatch,
    #[error("服务端累计音频进度不一致")]
    ProgressMismatch,
    #[error("音频帧格式无效")]
    InvalidAudio,
    #[error("未发送音频达到 2 秒上限，本轮已停止，已收到文字保留")]
    BufferOverflow,
    #[error("客户端输入队列已满，本轮已停止")]
    InputQueueFull,
    #[error("本轮已停止接收音频")]
    SessionClosed,
    #[error("已达到本轮听写时长上限")]
    SessionLimit,
    #[error("录音设备不可用或音频采集已中断")]
    CaptureFailed,
}

impl SessionError {
    pub(crate) fn http_status(status: u16) -> Self {
        let code = match status {
            401 => ServiceCode::Unauthorized,
            403 => ServiceCode::Forbidden,
            409 => ServiceCode::ModelNotReady,
            429 => ServiceCode::CapacityExceeded,
            503 => ServiceCode::ServiceUnavailable,
            _ => ServiceCode::Unknown,
        };
        Self::Remote {
            code,
            retryable: matches!(
                code,
                ServiceCode::CapacityExceeded | ServiceCode::ServiceUnavailable
            ),
            retry_after_ms: matches!(
                code,
                ServiceCode::CapacityExceeded | ServiceCode::ServiceUnavailable
            )
            .then_some(2000),
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
pub struct AudioCapabilities {
    pub encoding: String,
    pub sample_rate: u64,
    pub channels: u8,
    pub max_frame_bytes: usize,
}

#[derive(Clone, Debug, Deserialize)]
pub struct FlowCapabilities {
    pub window_samples: u64,
    pub client_buffer_max_ms: u64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct Capabilities {
    pub protocol_version: u8,
    pub server_instance_id: String,
    pub model_generation: Option<String>,
    pub model_id: Option<String>,
    pub mode: Option<String>,
    pub model_state: String,
    pub ready: bool,
    pub stream_supported: bool,
    pub can_start: bool,
    pub unavailable_reason: Option<ServiceCode>,
    pub max_sessions: usize,
    pub mobile_slots_available: usize,
    pub audio: AudioCapabilities,
    pub flow: FlowCapabilities,
    pub session_max_seconds: f64,
}

impl Capabilities {
    pub fn validate(&self) -> Result<(), SessionError> {
        if self.protocol_version != 1
            || !valid_id(&self.server_instance_id)
            || self.audio.encoding != "pcm_s16le"
            || self.audio.sample_rate != SAMPLE_RATE
            || self.audio.channels != 1
            || self.audio.max_frame_bytes < 2
            || self.audio.max_frame_bytes > MAX_FRAME_BYTES
            || self.audio.max_frame_bytes % 2 != 0
            || self.flow.window_samples == 0
            || self.flow.window_samples > SAMPLE_RATE * 3600
            || self.flow.client_buffer_max_ms == 0
            || !self.session_max_seconds.is_finite()
            || self.session_max_seconds <= 0.0
            || self.session_max_seconds > 3600.0
            || !matches!(
                self.model_state.as_str(),
                "unloaded" | "loading" | "ready" | "unloading" | "error"
            )
            || self.mobile_slots_available > 1
        {
            return Err(SessionError::InvalidCapabilities);
        }
        if self.can_start
            && (!self.ready
                || !self.stream_supported
                || self.model_state != "ready"
                || self.mobile_slots_available == 0
                || self.unavailable_reason.is_some()
                || !self.model_generation.as_deref().is_some_and(valid_id))
        {
            return Err(SessionError::InvalidCapabilities);
        }
        Ok(())
    }

    pub fn ensure_can_start(&self) -> Result<(), SessionError> {
        self.validate()?;
        if !self.can_start {
            let code = self
                .unavailable_reason
                .unwrap_or(ServiceCode::ModelNotReady);
            return Err(SessionError::Remote {
                code,
                retryable: matches!(
                    code,
                    ServiceCode::CapacityExceeded | ServiceCode::ServiceUnavailable
                ),
                retry_after_ms: matches!(
                    code,
                    ServiceCode::CapacityExceeded | ServiceCode::ServiceUnavailable
                )
                .then_some(2000),
            });
        }
        Ok(())
    }

    pub(crate) fn buffer_samples(&self) -> u64 {
        self.flow.client_buffer_max_ms.min(MAX_BUFFER_MS) * SAMPLE_RATE / 1000
    }
}

fn valid_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionIdentity {
    pub server_instance_id: String,
    pub model_generation: String,
    pub session_id: String,
}

#[derive(Clone, Default, Eq, PartialEq)]
pub struct TextUpdate {
    pub fixed: String,
    pub pending: String,
    pub new_suffix: String,
    pub seq: u64,
}

impl fmt::Debug for TextUpdate {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("TextUpdate")
            .field("seq", &self.seq)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Progress {
    pub sent_samples: u64,
    pub received_samples: u64,
    pub processed_samples: u64,
    pub send_limit: u64,
    pub buffered_samples: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum FinishReason {
    Finished,
    Cancelled,
    #[serde(other)]
    Other,
}

#[derive(Clone, Debug)]
pub enum SessionEvent {
    Ready(SessionIdentity),
    Progress(Progress),
    Text(TextUpdate),
    Finished {
        update: TextUpdate,
        complete: bool,
        reason: FinishReason,
    },
    Cancelled,
    Failed(SessionError),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SessionOutcome {
    Finished { complete: bool },
    Cancelled,
}

#[derive(Serialize)]
struct AudioFormat {
    encoding: &'static str,
    sample_rate: u64,
    channels: u8,
}

#[derive(Serialize)]
struct StartMessage<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    protocol_version: u8,
    expected_server_instance_id: &'a str,
    expected_model_generation: &'a str,
    audio: AudioFormat,
}

pub(crate) fn start_message(capabilities: &Capabilities) -> Result<String, SessionError> {
    capabilities.ensure_can_start()?;
    let message = StartMessage {
        kind: "start",
        protocol_version: 1,
        expected_server_instance_id: &capabilities.server_instance_id,
        expected_model_generation: capabilities
            .model_generation
            .as_deref()
            .ok_or(SessionError::InvalidCapabilities)?,
        audio: AudioFormat {
            encoding: "pcm_s16le",
            sample_rate: SAMPLE_RATE,
            channels: 1,
        },
    };
    serde_json::to_string(&message).map_err(|_| SessionError::InvalidMessage)
}

#[derive(Deserialize)]
pub(crate) struct WireEvent {
    #[serde(rename = "type")]
    kind: String,
    server_instance_id: Option<String>,
    model_generation: Option<String>,
    session_id: Option<String>,
    seq: u64,
    audio_received_samples: Option<u64>,
    audio_processed_samples: Option<u64>,
    audio_send_limit: Option<u64>,
    text: Option<String>,
    pending: Option<String>,
    reason: Option<FinishReason>,
    complete: Option<bool>,
    code: Option<ServiceCode>,
    retryable: Option<bool>,
    retry_after_ms: Option<u64>,
}

pub(crate) enum AcceptedEvent {
    Stale,
    Ready(SessionIdentity),
    Progress,
    Text(TextUpdate),
    Final {
        update: TextUpdate,
        complete: bool,
        reason: FinishReason,
    },
    Error {
        update: TextUpdate,
        error: SessionError,
    },
}

pub(crate) struct SessionValidator {
    expected_instance: String,
    expected_generation: String,
    window: u64,
    identity: Option<SessionIdentity>,
    last_seq: u64,
    fixed: String,
    pub(crate) received: u64,
    pub(crate) processed: u64,
    pub(crate) send_limit: u64,
}

impl SessionValidator {
    pub(crate) fn new(capabilities: &Capabilities) -> Result<Self, SessionError> {
        capabilities.ensure_can_start()?;
        Ok(Self {
            expected_instance: capabilities.server_instance_id.clone(),
            expected_generation: capabilities
                .model_generation
                .clone()
                .ok_or(SessionError::InvalidCapabilities)?,
            window: capabilities.flow.window_samples,
            identity: None,
            last_seq: 0,
            fixed: String::new(),
            received: 0,
            processed: 0,
            send_limit: 0,
        })
    }

    pub(crate) fn is_ready(&self) -> bool {
        self.identity.is_some()
    }

    pub(crate) fn accept(&mut self, raw: &str, sent: u64) -> Result<AcceptedEvent, SessionError> {
        let event: WireEvent =
            serde_json::from_str(raw).map_err(|_| SessionError::InvalidMessage)?;
        if self.identity.is_none() && event.kind == "error" {
            if event.session_id.is_some()
                || event.seq != 0
                || event.text.as_deref() != Some("")
                || event.pending.as_deref() != Some("")
                || event.complete != Some(false)
            {
                return Err(SessionError::InvalidMessage);
            }
            return Ok(AcceptedEvent::Error {
                update: TextUpdate::default(),
                error: remote_error(&event)?,
            });
        }
        if event.server_instance_id.as_deref() != Some(&self.expected_instance)
            || event.model_generation.as_deref() != Some(&self.expected_generation)
            || !event.session_id.as_deref().is_some_and(valid_id)
        {
            return Err(SessionError::IdentityMismatch);
        }
        if let Some(identity) = &self.identity {
            if event.session_id.as_deref() != Some(&identity.session_id) {
                return Err(SessionError::IdentityMismatch);
            }
            if event.seq <= self.last_seq {
                return Ok(AcceptedEvent::Stale);
            }
            if event.kind == "ready" {
                return Err(SessionError::InvalidMessage);
            }
        } else if event.kind != "ready" || event.seq == 0 {
            return Err(SessionError::InvalidMessage);
        }
        let has_progress = matches!(event.kind.as_str(), "ready" | "flow" | "keepalive");
        if has_progress
            && (event.audio_received_samples.is_none()
                || event.audio_processed_samples.is_none()
                || event.audio_send_limit.is_none())
        {
            return Err(SessionError::InvalidMessage);
        }
        if matches!(event.kind.as_str(), "partial" | "final")
            && event.audio_processed_samples.is_none()
        {
            return Err(SessionError::InvalidMessage);
        }
        let processed = event.audio_processed_samples.unwrap_or(self.processed);
        let received = event
            .audio_received_samples
            .unwrap_or(self.received.max(processed));
        let limit = event.audio_send_limit.unwrap_or(self.send_limit);
        if processed < self.processed
            || received < self.received
            || processed > received
            || received > sent
            || limit < self.send_limit
            || event.audio_send_limit.is_some() && processed.checked_add(self.window) != Some(limit)
        {
            return Err(SessionError::ProgressMismatch);
        }
        let accepted = match event.kind.as_str() {
            "ready" => {
                if received != 0 || processed != 0 || limit != self.window {
                    return Err(SessionError::ProgressMismatch);
                }
                let identity = SessionIdentity {
                    server_instance_id: self.expected_instance.clone(),
                    model_generation: self.expected_generation.clone(),
                    session_id: event
                        .session_id
                        .clone()
                        .ok_or(SessionError::IdentityMismatch)?,
                };
                self.identity = Some(identity.clone());
                AcceptedEvent::Ready(identity)
            }
            "flow" | "keepalive" => AcceptedEvent::Progress,
            "partial" => AcceptedEvent::Text(self.text_update(&event)?),
            "final" => {
                let complete = event.complete.ok_or(SessionError::InvalidMessage)?;
                let reason = event.reason.ok_or(SessionError::InvalidMessage)?;
                if event.pending.as_deref() != Some("")
                    || complete && (reason != FinishReason::Finished || processed != sent)
                {
                    return Err(SessionError::ProgressMismatch);
                }
                AcceptedEvent::Final {
                    update: self.text_update(&event)?,
                    complete,
                    reason,
                }
            }
            "error" => {
                if event.complete != Some(false) || event.pending.as_deref() != Some("") {
                    return Err(SessionError::InvalidMessage);
                }
                let error = remote_error(&event)?;
                AcceptedEvent::Error {
                    update: self.text_update(&event)?,
                    error,
                }
            }
            _ => return Err(SessionError::InvalidMessage),
        };
        self.last_seq = event.seq;
        self.received = received;
        self.processed = processed;
        self.send_limit = limit;
        Ok(accepted)
    }

    fn text_update(&mut self, event: &WireEvent) -> Result<TextUpdate, SessionError> {
        let fixed = event.text.as_ref().ok_or(SessionError::InvalidMessage)?;
        let pending = event.pending.as_ref().ok_or(SessionError::InvalidMessage)?;
        if !fixed.starts_with(&self.fixed) {
            return Err(SessionError::TextPrefixMismatch);
        }
        let update = TextUpdate {
            fixed: fixed.clone(),
            pending: pending.clone(),
            new_suffix: fixed[self.fixed.len()..].to_owned(),
            seq: event.seq,
        };
        self.fixed = fixed.clone();
        Ok(update)
    }
}

fn remote_error(event: &WireEvent) -> Result<SessionError, SessionError> {
    Ok(SessionError::Remote {
        code: event.code.ok_or(SessionError::InvalidMessage)?,
        retryable: event.retryable.ok_or(SessionError::InvalidMessage)?,
        retry_after_ms: event.retry_after_ms,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn capabilities() -> Capabilities {
        serde_json::from_value(serde_json::json!({
            "protocol_version":1,"server_instance_id":"boot","model_generation":"generation",
            "model_id":"R2T2","mode":"r2t2","model_state":"ready","ready":true,
            "stream_supported":true,"can_start":true,"unavailable_reason":null,
            "max_sessions":2,"mobile_slots_available":1,
            "audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1,"max_frame_bytes":5120},
            "flow":{"window_samples":32000,"client_buffer_max_ms":2000},"session_max_seconds":3600
        })).unwrap()
    }

    pub(crate) fn event(kind: &str, seq: u64, fields: serde_json::Value) -> String {
        let mut value = serde_json::json!({"type":kind,"seq":seq,"server_instance_id":"boot","model_generation":"generation","session_id":"session"});
        value
            .as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        value.to_string()
    }

    fn ready(validator: &mut SessionValidator) {
        validator.accept(&event("ready",1,serde_json::json!({"audio_received_samples":0,"audio_processed_samples":0,"audio_send_limit":32000})),0).unwrap();
    }

    #[test]
    fn endpoint_requires_tailnet_https_and_no_credentials_or_url_extras() {
        let endpoint = Endpoint::parse("https://HOST.tail.ts.net:8097/").unwrap();
        assert_eq!(endpoint.origin(), "https://host.tail.ts.net:8097");
        assert_eq!(
            endpoint.stream_url(),
            "wss://host.tail.ts.net:8097/api/mobile/v1/dictation/stream"
        );
        for value in [
            "http://host.ts.net",
            "https://127.0.0.1:8097",
            "https://host.example",
            "https://host.ts.net.evil",
            "https://host.ts.net/?token=secret",
            "https://host.ts.net/#fragment",
            "https://user:secret@host.ts.net",
            "https://@host.ts.net",
            "https://host.ts.net/a/..",
            "https://host.ts.net\\path",
            "https://host.ts.net/path",
            "https://host.ts.net:0",
        ] {
            assert_eq!(Endpoint::parse(value), Err(SessionError::InvalidEndpoint));
        }
    }

    #[test]
    fn token_and_transcript_debug_are_redacted() {
        let token = DeviceToken::new("not-a-real-token").unwrap();
        assert!(!format!("{token:?}").contains("not-a-real-token"));
        assert!(DeviceToken::new("secret\r\nheader: value").is_err());
        assert!(DeviceToken::new(" secret").is_err());
        let text = TextUpdate {
            fixed: "private words".into(),
            ..Default::default()
        };
        assert!(!format!("{:?}", SessionEvent::Text(text)).contains("private words"));
    }

    #[test]
    fn start_contains_only_mobile_contract_fields() {
        for mode in ["r2t2", "qwen-stream", "future-stream"] {
            let mut caps = capabilities();
            caps.mode = Some(mode.into());
            let value: serde_json::Value =
                serde_json::from_str(&start_message(&caps).unwrap()).unwrap();
            assert_eq!(
                value,
                serde_json::json!({
                    "type":"start", "protocol_version":1,
                    "expected_server_instance_id":"boot",
                    "expected_model_generation":"generation",
                    "audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1}
                })
            );
        }
    }

    #[test]
    fn can_start_depends_on_capabilities_instead_of_model_labels() {
        for mode in [
            Some("r2t2"),
            Some("qwen-stream"),
            Some("future-stream"),
            None,
        ] {
            let mut caps = capabilities();
            caps.mode = mode.map(str::to_owned);
            caps.model_id = None;
            caps.ensure_can_start().unwrap();
            SessionValidator::new(&caps).unwrap();
        }
    }

    #[test]
    fn advertised_start_requires_consistent_protocol_audio_flow_and_readiness() {
        let invalid: [fn(&mut Capabilities); 10] = [
            |caps| caps.protocol_version = 2,
            |caps| caps.audio.sample_rate = 48000,
            |caps| caps.audio.max_frame_bytes = 3,
            |caps| caps.flow.window_samples = 0,
            |caps| caps.flow.client_buffer_max_ms = 0,
            |caps| caps.ready = false,
            |caps| caps.stream_supported = false,
            |caps| caps.model_state = "loading".into(),
            |caps| caps.mobile_slots_available = 0,
            |caps| caps.model_generation = None,
        ];
        for mutate in invalid {
            let mut caps = capabilities();
            caps.mode = Some("qwen-stream".into());
            mutate(&mut caps);
            assert_eq!(
                caps.ensure_can_start(),
                Err(SessionError::InvalidCapabilities)
            );
        }
    }

    #[test]
    fn unavailable_qwen_honors_service_capabilities() {
        let mut caps = capabilities();
        caps.mode = Some("qwen-stream".into());
        caps.can_start = false;
        caps.stream_supported = false;
        caps.unavailable_reason = Some(ServiceCode::ModelUnsupported);
        assert_eq!(
            caps.ensure_can_start(),
            Err(SessionError::Remote {
                code: ServiceCode::ModelUnsupported,
                retryable: false,
                retry_after_ms: None,
            })
        );
    }

    #[test]
    fn cumulative_flow_does_not_add_duplicate_credit() {
        let mut validator = SessionValidator::new(&capabilities()).unwrap();
        ready(&mut validator);
        let flow = event(
            "flow",
            3,
            serde_json::json!({"audio_received_samples":1600,"audio_processed_samples":1600,"audio_send_limit":33600}),
        );
        validator.accept(&flow, 1600).unwrap();
        assert!(matches!(
            validator.accept(&flow, 1600).unwrap(),
            AcceptedEvent::Stale
        ));
        assert_eq!(validator.send_limit, 33600);
    }

    #[test]
    fn rejects_identity_prefix_and_counter_regression() {
        let mut validator = SessionValidator::new(&capabilities()).unwrap();
        ready(&mut validator);
        let update = event(
            "partial",
            2,
            serde_json::json!({"text":"测试","pending":"候选","audio_processed_samples":1}),
        );
        match validator.accept(&update, 1).unwrap() {
            AcceptedEvent::Text(update) => assert_eq!(update.new_suffix, "测试"),
            _ => panic!("missing text"),
        }
        let wrong = event(
            "partial",
            3,
            serde_json::json!({"text":"错误","pending":"","audio_processed_samples":1}),
        );
        assert!(matches!(
            validator.accept(&wrong, 1),
            Err(SessionError::TextPrefixMismatch)
        ));
        let wrong_identity = event(
            "flow",
            1,
            serde_json::json!({"session_id":"other","audio_received_samples":1,"audio_processed_samples":1,"audio_send_limit":32001}),
        );
        assert!(matches!(
            validator.accept(&wrong_identity, 1),
            Err(SessionError::IdentityMismatch)
        ));
        let regression = event(
            "flow",
            4,
            serde_json::json!({"audio_received_samples":1,"audio_processed_samples":0,"audio_send_limit":32000}),
        );
        assert!(matches!(
            validator.accept(&regression, 1),
            Err(SessionError::ProgressMismatch)
        ));
    }

    #[test]
    fn final_unchanged_text_still_has_new_sequence() {
        let mut validator = SessionValidator::new(&capabilities()).unwrap();
        ready(&mut validator);
        validator
            .accept(
                &event(
                    "partial",
                    2,
                    serde_json::json!({"text":"固定","pending":"候选","audio_processed_samples":1}),
                ),
                1,
            )
            .unwrap();
        match validator.accept(&event("final",5,serde_json::json!({"text":"固定","pending":"","audio_processed_samples":1,"reason":"finished","complete":true})),1).unwrap() {
            AcceptedEvent::Final{update,complete,..}=>{ assert_eq!(update.seq,5); assert_eq!(update.new_suffix,""); assert!(complete); },
            _=>panic!("missing final")
        }
    }

    #[test]
    fn startup_error_accepts_null_identity_without_leaking_message() {
        let mut validator = SessionValidator::new(&capabilities()).unwrap();
        let event = serde_json::json!({"type":"error","session_id":null,"seq":0,"text":"","pending":"","complete":false,"code":"MODEL_CHANGED","retryable":false,"retry_after_ms":null,"message":"private-token"});
        match validator.accept(&event.to_string(), 0).unwrap() {
            AcceptedEvent::Error { error, .. } => {
                assert!(!format!("{error:?}").contains("private-token"))
            }
            _ => panic!("missing error"),
        }
    }

    #[test]
    fn partial_with_processed_only_can_precede_coalesced_flow() {
        let mut validator = SessionValidator::new(&capabilities()).unwrap();
        ready(&mut validator);
        validator
            .accept(
                &event(
                    "partial",
                    2,
                    serde_json::json!({"text":"x","pending":"","audio_processed_samples":10}),
                ),
                10,
            )
            .unwrap();
        validator.accept(&event("flow",4,serde_json::json!({"audio_received_samples":10,"audio_processed_samples":10,"audio_send_limit":32010})),10).unwrap();
        assert_eq!(validator.send_limit, 32010);
    }
}
