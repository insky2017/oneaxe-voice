use std::collections::VecDeque;
use std::error::Error;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc as events, Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::{mpsc, watch, Notify};
use tokio::time::{timeout, Instant};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::{header::AUTHORIZATION, HeaderValue, Request};
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::{Error as WebSocketError, Message};
use tokio_tungstenite::{connect_async_with_config, WebSocketStream};

use crate::protocol::{
    start_message, AcceptedEvent, Capabilities, DeviceToken, Endpoint, Progress, SessionError,
    SessionEvent, SessionOutcome, SessionValidator, SAMPLE_RATE,
};

const INPUT_QUEUE_CAPACITY: usize = 128;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const READY_TIMEOUT: Duration = Duration::from_secs(10);
const SEND_TIMEOUT: Duration = Duration::from_secs(5);
const KEEPALIVE_INTERVAL: Duration = Duration::from_secs(10);
const SERVER_SILENCE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Clone)]
pub struct DeliveryGate(Arc<AtomicBool>);

impl DeliveryGate {
    pub fn new() -> Self {
        Self(Arc::new(AtomicBool::new(true)))
    }

    pub fn is_open(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }

    pub fn close(&self) {
        self.0.store(false, Ordering::Release);
    }
}

impl Default for DeliveryGate {
    fn default() -> Self {
        Self::new()
    }
}

struct Shared {
    gate: DeliveryGate,
    buffered: AtomicU64,
    sent: AtomicU64,
    cancelled: AtomicBool,
    ended: AtomicBool,
    failure_reported: AtomicBool,
    cancel_reason: Mutex<Option<SessionError>>,
    cancellation: Notify,
    max_buffer_samples: u64,
    max_frame_bytes: usize,
    max_samples: u64,
}

impl Shared {
    fn stop(&self, error: Option<SessionError>) {
        self.gate.close();
        self.ended.store(true, Ordering::Release);
        let mut reason = self
            .cancel_reason
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if !self.cancelled.load(Ordering::Acquire) {
            *reason = error;
            self.cancelled.store(true, Ordering::Release);
        }
        drop(reason);
        self.cancellation.notify_waiters();
    }

    async fn cancelled(&self) {
        loop {
            let notified = self.cancellation.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.cancelled.load(Ordering::Acquire) {
                return;
            }
            notified.await;
        }
    }

    fn cancellation_result(
        &self,
        sender: &events::Sender<SessionEvent>,
    ) -> Result<SessionOutcome, SessionError> {
        let reason = self
            .cancel_reason
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .clone();
        if let Some(error) = reason {
            self.report_failure(sender, &error);
            Err(error)
        } else {
            let _ = sender.send(SessionEvent::Cancelled);
            Ok(SessionOutcome::Cancelled)
        }
    }

    fn report_failure(&self, sender: &events::Sender<SessionEvent>, error: &SessionError) {
        self.gate.close();
        self.ended.store(true, Ordering::Release);
        if !self.failure_reported.swap(true, Ordering::AcqRel) {
            let _ = sender.send(SessionEvent::Failed(error.clone()));
        }
    }
}

struct SamplePermit {
    shared: Arc<Shared>,
    samples: u64,
}

impl Drop for SamplePermit {
    fn drop(&mut self) {
        self.shared
            .buffered
            .fetch_sub(self.samples, Ordering::AcqRel);
    }
}

struct Frame {
    pcm: Vec<u8>,
    permit: SamplePermit,
}

enum InputPacket {
    Audio(Frame),
    Flush,
    Finish,
}

struct InputState {
    sender: mpsc::Sender<InputPacket>,
    accepting: bool,
    captured_samples: u64,
}

#[derive(Clone)]
pub struct SessionHandle {
    shared: Arc<Shared>,
    input: Arc<Mutex<InputState>>,
}

pub struct SessionInput {
    shared: Arc<Shared>,
    receiver: mpsc::Receiver<InputPacket>,
}

pub fn session_channel(
    capabilities: &Capabilities,
) -> Result<(SessionHandle, SessionInput), SessionError> {
    capabilities.ensure_can_start()?;
    let (sender, receiver) = mpsc::channel(INPUT_QUEUE_CAPACITY);
    let shared = Arc::new(Shared {
        gate: DeliveryGate::new(),
        buffered: AtomicU64::new(0),
        sent: AtomicU64::new(0),
        cancelled: AtomicBool::new(false),
        ended: AtomicBool::new(false),
        failure_reported: AtomicBool::new(false),
        cancel_reason: Mutex::new(None),
        cancellation: Notify::new(),
        max_buffer_samples: capabilities.buffer_samples(),
        max_frame_bytes: capabilities.audio.max_frame_bytes,
        max_samples: (capabilities.session_max_seconds * SAMPLE_RATE as f64).floor() as u64,
    });
    Ok((
        SessionHandle {
            shared: shared.clone(),
            input: Arc::new(Mutex::new(InputState {
                sender,
                accepting: true,
                captured_samples: 0,
            })),
        },
        SessionInput { shared, receiver },
    ))
}

impl SessionHandle {
    pub fn delivery_gate(&self) -> DeliveryGate {
        self.shared.gate.clone()
    }

    pub fn buffered_samples(&self) -> u64 {
        self.shared.buffered.load(Ordering::Acquire)
    }

    pub fn cancel(&self) {
        self.shared.stop(None);
    }

    pub fn fail_client(&self, error: SessionError) {
        self.shared.stop(Some(error));
    }

    pub fn try_audio(&self, pcm: Vec<u8>) -> Result<(), SessionError> {
        let result = self.enqueue_audio(pcm);
        if let Err(error) = &result {
            if *error != SessionError::SessionClosed {
                self.fail_client(error.clone());
            }
        }
        result
    }

    fn enqueue_audio(&self, pcm: Vec<u8>) -> Result<(), SessionError> {
        let mut input = self
            .input
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if !input.accepting || self.shared.ended.load(Ordering::Acquire) {
            return Err(SessionError::SessionClosed);
        }
        if pcm.len() < 2 || pcm.len() > self.shared.max_frame_bytes || pcm.len() % 2 != 0 {
            return Err(SessionError::InvalidAudio);
        }
        let samples = (pcm.len() / 2) as u64;
        if input.captured_samples + samples > self.shared.max_samples {
            return Err(SessionError::SessionLimit);
        }
        if self.buffered_samples() + samples > self.shared.max_buffer_samples {
            return Err(SessionError::BufferOverflow);
        }
        self.shared.buffered.fetch_add(samples, Ordering::AcqRel);
        let frame = Frame {
            pcm,
            permit: SamplePermit {
                shared: self.shared.clone(),
                samples,
            },
        };
        input
            .sender
            .try_send(InputPacket::Audio(frame))
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => SessionError::InputQueueFull,
                mpsc::error::TrySendError::Closed(_) => SessionError::SessionClosed,
            })?;
        input.captured_samples += samples;
        Ok(())
    }

    pub fn flush(&self) -> Result<(), SessionError> {
        self.enqueue_control(false)
    }

    pub fn finish(&self) -> Result<(), SessionError> {
        self.enqueue_control(true)
    }

    fn enqueue_control(&self, finish: bool) -> Result<(), SessionError> {
        let result = {
            let mut input = self
                .input
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            if !input.accepting || self.shared.ended.load(Ordering::Acquire) {
                return Err(SessionError::SessionClosed);
            }
            if finish {
                input.accepting = false;
            }
            input
                .sender
                .try_send(if finish {
                    InputPacket::Finish
                } else {
                    InputPacket::Flush
                })
                .map_err(|error| match error {
                    mpsc::error::TrySendError::Full(_) => SessionError::InputQueueFull,
                    mpsc::error::TrySendError::Closed(_) => SessionError::SessionClosed,
                })
        };
        if let Err(error) = &result {
            if *error != SessionError::SessionClosed {
                self.fail_client(error.clone());
            }
        }
        result
    }
}

pub async fn capabilities(
    endpoint: &Endpoint,
    token: &DeviceToken,
) -> Result<Capabilities, SessionError> {
    let client = reqwest::Client::builder()
        .https_only(true)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|_| SessionError::ConnectionFailed)?;
    let mut authorization = HeaderValue::from_str(&format!("Bearer {}", token.expose_secret()))
        .map_err(|_| SessionError::InvalidToken)?;
    authorization.set_sensitive(true);
    let mut response = client
        .get(endpoint.capabilities_url())
        .header(AUTHORIZATION, authorization)
        .send()
        .await
        .map_err(http_error)?;
    if !response.status().is_success() {
        return Err(SessionError::http_status(response.status().as_u16()));
    }
    if response
        .content_length()
        .is_some_and(|length| length > 65_536)
    {
        return Err(SessionError::InvalidCapabilities);
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(http_error)? {
        if bytes.len() + chunk.len() > 65_536 {
            return Err(SessionError::InvalidCapabilities);
        }
        bytes.extend_from_slice(&chunk);
    }
    let value: Capabilities =
        serde_json::from_slice(&bytes).map_err(|_| SessionError::InvalidCapabilities)?;
    value.validate()?;
    Ok(value)
}

fn ws_request(endpoint: &Endpoint, token: &DeviceToken) -> Result<Request<()>, SessionError> {
    let mut request = endpoint
        .stream_url()
        .into_client_request()
        .map_err(|_| SessionError::InvalidEndpoint)?;
    let mut authorization = HeaderValue::from_str(&format!("Bearer {}", token.expose_secret()))
        .map_err(|_| SessionError::InvalidToken)?;
    authorization.set_sensitive(true);
    request.headers_mut().insert(AUTHORIZATION, authorization);
    Ok(request)
}

fn has_tls_source(error: &(dyn Error + 'static)) -> bool {
    let mut source = Some(error);
    while let Some(value) = source {
        let kind = value.to_string().to_ascii_lowercase();
        if [
            "certificate",
            "invalidcert",
            "invalidpeer",
            "tls",
            "hostname",
            "unknownissuer",
        ]
        .iter()
        .any(|needle| kind.contains(needle))
        {
            return true;
        }
        source = value.source();
    }
    false
}

fn http_error(error: reqwest::Error) -> SessionError {
    if error.is_timeout() {
        SessionError::Timeout
    } else if error.source().is_some_and(has_tls_source) {
        SessionError::TlsIdentity
    } else {
        SessionError::ConnectionFailed
    }
}

fn websocket_error(error: WebSocketError, established: bool) -> SessionError {
    match error {
        WebSocketError::Http(response) => SessionError::http_status(response.status().as_u16()),
        WebSocketError::Tls(_) => SessionError::TlsIdentity,
        WebSocketError::Io(error) if has_tls_source(&error) => SessionError::TlsIdentity,
        _ if established => SessionError::ConnectionLost,
        _ => SessionError::ConnectionFailed,
    }
}

pub async fn run_session(
    endpoint: Endpoint,
    token: DeviceToken,
    capabilities: Capabilities,
    input: SessionInput,
    sender: events::Sender<SessionEvent>,
) -> Result<SessionOutcome, SessionError> {
    let shared = input.shared.clone();
    let result = async {
        capabilities.ensure_can_start()?;
        let request = ws_request(&endpoint, &token)?;
        let websocket_config = WebSocketConfig {max_message_size:Some(4 * 1024 * 1024),max_frame_size:Some(4 * 1024 * 1024),..Default::default()};
        let connection = tokio::select! {
            biased;
            _ = shared.cancelled() => return shared.cancellation_result(&sender),
            connection = timeout(CONNECT_TIMEOUT, connect_async_with_config(request,Some(websocket_config),false)) => connection,
        };
        let (stream, _) = connection.map_err(|_| SessionError::Timeout)?.map_err(|error| websocket_error(error, false))?;
        run_connected(stream, capabilities, input, sender.clone()).await
    }.await;
    if let Err(error) = &result {
        shared.report_failure(&sender, error);
    }
    result
}

#[derive(Clone, Copy, Default)]
struct Credit {
    ready: bool,
    limit: u64,
}

async fn run_connected<S>(
    stream: WebSocketStream<S>,
    capabilities: Capabilities,
    input: SessionInput,
    sender: events::Sender<SessionEvent>,
) -> Result<SessionOutcome, SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    run_connected_with_ready_deadline(
        stream,
        capabilities,
        input,
        sender,
        Instant::now() + READY_TIMEOUT,
    )
    .await
}

async fn run_connected_with_ready_deadline<S>(
    mut stream: WebSocketStream<S>,
    capabilities: Capabilities,
    input: SessionInput,
    sender: events::Sender<SessionEvent>,
    ready_deadline: Instant,
) -> Result<SessionOutcome, SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let shared = input.shared.clone();
    let start = start_message(&capabilities)?;
    tokio::select! {
        biased;
        _ = shared.cancelled() => return shared.cancellation_result(&sender),
        result = timeout(SEND_TIMEOUT, stream.send(Message::Text(start))) => {
            result.map_err(|_| SessionError::Timeout)?.map_err(|error| websocket_error(error,true))?;
        }
    }
    let (sink, mut source) = stream.split();
    let (credit_sender, credit_receiver) = watch::channel(Credit::default());
    let (pong_sender, pong_receiver) = mpsc::channel(8);
    let (stop_sender, stop_receiver) = watch::channel(false);
    let writer_shared = shared.clone();
    let mut writer = tokio::spawn(writer_loop(
        sink,
        input.receiver,
        credit_receiver,
        pong_receiver,
        stop_receiver,
        writer_shared,
    ));
    let mut validator = SessionValidator::new(&capabilities)?;
    let mut deadline = ready_deadline;
    let mut writer_finished = false;
    let session_deadline =
        Instant::now() + Duration::from_secs_f64(capabilities.session_max_seconds);
    let result = loop {
        tokio::select! {
            biased;
            _ = shared.cancelled() => break shared.cancellation_result(&sender),
            _ = tokio::time::sleep_until(deadline) => break Err(SessionError::Timeout),
            _ = tokio::time::sleep_until(session_deadline) => break Err(SessionError::SessionLimit),
            writer_result = &mut writer => {
                writer_finished = true;
                break match writer_result {
                    Ok(Ok(())) if shared.cancelled.load(Ordering::Acquire) => shared.cancellation_result(&sender),
                    Ok(Err(error)) => Err(error),
                    _ => Err(SessionError::ConnectionLost),
                };
            }
            received = source.next() => {
                if shared.cancelled.load(Ordering::Acquire) {
                    continue;
                }
                let message = match received {
                    Some(Ok(message)) => message,
                    Some(Err(error)) => break Err(websocket_error(error,true)),
                    None => break Err(SessionError::ConnectionLost),
                };
                let raw = match message {
                    Message::Text(raw) if raw.len() <= 4 * 1024 * 1024 => raw,
                    Message::Ping(payload) => {
                        if validator.is_ready() {
                            deadline = Instant::now() + SERVER_SILENCE_TIMEOUT;
                        }
                        if pong_sender.try_send(payload).is_err() {
                            break Err(SessionError::InvalidMessage);
                        }
                        continue;
                    }
                    Message::Pong(_) => {
                        if validator.is_ready() {
                            deadline = Instant::now() + SERVER_SILENCE_TIMEOUT;
                        }
                        continue;
                    }
                    Message::Close(_) => break Err(SessionError::ConnectionLost),
                    _ => break Err(SessionError::InvalidMessage),
                };
                let accepted = match validator.accept(&raw,shared.sent.load(Ordering::Acquire)) {
                    Ok(accepted) => accepted,
                    Err(error) => break Err(error),
                };
                let progress = Progress {
                    sent_samples:shared.sent.load(Ordering::Acquire),
                    received_samples:validator.received,
                    processed_samples:validator.processed,
                    send_limit:validator.send_limit,
                    buffered_samples:shared.buffered.load(Ordering::Acquire),
                };
                match accepted {
                    AcceptedEvent::Stale => continue,
                    AcceptedEvent::Ready(identity) => {
                        let _ = sender.send(SessionEvent::Ready(identity));
                    }
                    AcceptedEvent::Progress => {}
                    AcceptedEvent::Text(update) => { let _ = sender.send(SessionEvent::Text(update)); }
                    AcceptedEvent::Final{update,complete,reason} => {
                        let _ = sender.send(SessionEvent::Progress(progress));
                        let _ = sender.send(SessionEvent::Finished{update,complete,reason});
                        break Ok(SessionOutcome::Finished{complete});
                    }
                    AcceptedEvent::Error{update,error} => {
                        let _ = sender.send(SessionEvent::Progress(progress));
                        if !update.fixed.is_empty() || !update.pending.is_empty() {
                            let _ = sender.send(SessionEvent::Text(update));
                        }
                        break Err(error);
                    }
                }
                deadline = Instant::now() + SERVER_SILENCE_TIMEOUT;
                credit_sender.send_replace(Credit {ready:validator.is_ready(),limit:validator.send_limit});
                let _ = sender.send(SessionEvent::Progress(progress));
            }
        }
    };
    let _ = stop_sender.send(true);
    shared.ended.store(true, Ordering::Release);
    if !writer_finished && shared.cancelled.load(Ordering::Acquire) {
        if timeout(Duration::from_millis(500), &mut writer)
            .await
            .is_err()
        {
            writer.abort();
            let _ = writer.await;
        }
    } else if !writer_finished {
        writer.abort();
        let _ = writer.await;
    }
    if let Err(error) = &result {
        shared.report_failure(&sender, error);
    }
    result
}

async fn writer_loop<S>(
    mut sink: futures_util::stream::SplitSink<WebSocketStream<S>, Message>,
    mut input: mpsc::Receiver<InputPacket>,
    mut credits: watch::Receiver<Credit>,
    mut pongs: mpsc::Receiver<Vec<u8>>,
    mut stop: watch::Receiver<bool>,
    shared: Arc<Shared>,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let mut pending = None;
    let mut finishing = false;
    let mut last_flushed = None;
    let mut controls = VecDeque::new();
    let mut keepalive =
        tokio::time::interval_at(Instant::now() + KEEPALIVE_INTERVAL, KEEPALIVE_INTERVAL);
    keepalive.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut next_send = Instant::now();
    loop {
        let credit = *credits.borrow();
        let available = match pending.as_ref() {
            Some(InputPacket::Audio(frame)) => shared
                .sent
                .load(Ordering::Acquire)
                .checked_add(frame.permit.samples)
                .is_some_and(|sent| sent <= credit.limit),
            Some(_) => true,
            None => false,
        };
        tokio::select! {
            biased;
            _ = shared.cancelled() => {
                let _ = timeout(Duration::from_millis(250),sink.send(Message::Text("{\"type\":\"cancel\"}".into()))).await;
                return Ok(());
            }
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() { return Ok(()); }
            }
            pong = pongs.recv() => {
                if let Some(payload) = pong {
                    controls.push_back(Message::Pong(payload));
                }
            }
            _ = keepalive.tick(), if credit.ready => {
                if !controls.iter().any(|message| matches!(message,Message::Text(_))) {
                    controls.push_back(Message::Text("{\"type\":\"keepalive\"}".into()));
                }
            }
            changed = credits.changed() => { if changed.is_err() { return Ok(()); } }
            packet = input.recv(), if pending.is_none() && !finishing => {
                match packet {
                    Some(packet) => pending = Some(packet),
                    None => {
                        shared.stop(Some(SessionError::SessionClosed));
                    }
                }
            }
            _ = tokio::time::sleep_until(next_send), if credit.ready && (available || !controls.is_empty()) => {
                let (message, frame) = if let Some(control) = controls.pop_front() {
                    (control,None)
                } else {
                    match pending.take().ok_or(SessionError::InvalidMessage)? {
                        InputPacket::Audio(frame) => {
                            // Reserve the wire count before await so a fast response cannot outrun validation.
                            shared.sent.fetch_add(frame.permit.samples,Ordering::AcqRel);
                            (Message::Binary(frame.pcm),Some(frame.permit))
                        }
                        InputPacket::Flush => {
                            let sent = shared.sent.load(Ordering::Acquire);
                            if last_flushed == Some(sent) { continue; }
                            last_flushed = Some(sent);
                            (Message::Text(serde_json::json!({"type":"flush","after_audio_samples":sent}).to_string()),None)
                        }
                        InputPacket::Finish => {
                            finishing = true;
                            (Message::Text(serde_json::json!({"type":"finish","after_audio_samples":shared.sent.load(Ordering::Acquire)}).to_string()),None)
                        }
                    }
                };
                // Includes audio and controls; at most 91 application messages in any second.
                next_send = Instant::now() + Duration::from_millis(11);
                tokio::select! {
                    biased;
                    _ = shared.cancelled() => {
                        drop(frame);
                        let _ = timeout(Duration::from_millis(250),sink.send(Message::Text("{\"type\":\"cancel\"}".into()))).await;
                        return Ok(());
                    }
                    result = timeout(SEND_TIMEOUT,sink.send(message)) => {
                        result.map_err(|_| SessionError::Timeout)?.map_err(|error| websocket_error(error,true))?;
                    }
                }
                drop(frame);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio_tungstenite::tungstenite::protocol::Role;

    fn caps(window: u64) -> Capabilities {
        serde_json::from_value(serde_json::json!({
            "protocol_version":1,"server_instance_id":"boot","model_generation":"generation",
            "model_id":"R2T2","mode":"r2t2","model_state":"ready","ready":true,
            "stream_supported":true,"can_start":true,"unavailable_reason":null,
            "max_sessions":2,"mobile_slots_available":1,
            "audio":{"encoding":"pcm_s16le","sample_rate":16000,"channels":1,"max_frame_bytes":5120},
            "flow":{"window_samples":window,"client_buffer_max_ms":2000},"session_max_seconds":3600
        })).unwrap()
    }

    fn event(kind: &str, seq: u64, fields: serde_json::Value) -> Message {
        let mut value = serde_json::json!({"type":kind,"seq":seq,"server_instance_id":"boot","model_generation":"generation","session_id":"session"});
        value
            .as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        Message::Text(value.to_string())
    }

    async fn pair() -> (
        WebSocketStream<tokio::io::DuplexStream>,
        WebSocketStream<tokio::io::DuplexStream>,
    ) {
        let (client, server) = tokio::io::duplex(65536);
        tokio::join!(
            WebSocketStream::from_raw_socket(client, Role::Client, None),
            WebSocketStream::from_raw_socket(server, Role::Server, None)
        )
    }

    async fn hello(server: &mut WebSocketStream<tokio::io::DuplexStream>, window: u64) {
        let start = server.next().await.unwrap().unwrap();
        let start: serde_json::Value = serde_json::from_str(start.to_text().unwrap()).unwrap();
        assert_eq!(start["type"], "start");
        assert!(start.get("mode").is_none());
        server.send(event("ready",1,serde_json::json!({"audio_received_samples":0,"audio_processed_samples":0,"audio_send_limit":window}))).await.unwrap();
    }

    #[test]
    fn websocket_auth_is_header_only_and_does_not_add_origin() {
        let endpoint = Endpoint::parse("https://voice.ts.net:8097").unwrap();
        let token = DeviceToken::new("not-a-real-token").unwrap();
        let request = ws_request(&endpoint, &token).unwrap();
        assert_eq!(request.headers()[AUTHORIZATION], "Bearer not-a-real-token");
        assert!(request.headers()[AUTHORIZATION].is_sensitive());
        assert!(request.headers().get("origin").is_none());
        assert!(!request.uri().to_string().contains("not-a-real-token"));
    }

    #[test]
    fn buffer_budget_includes_all_queued_samples_and_cancel_closes_gate() {
        let (handle, input) = session_channel(&caps(32000)).unwrap();
        for _ in 0..12 {
            handle.try_audio(vec![0; 5120]).unwrap();
        }
        handle.try_audio(vec![0; 2560]).unwrap();
        assert_eq!(handle.buffered_samples(), 32000);
        assert_eq!(
            handle.try_audio(vec![0; 2]),
            Err(SessionError::BufferOverflow)
        );
        assert!(!handle.delivery_gate().is_open());
        drop(input);
        assert_eq!(handle.buffered_samples(), 0);
    }

    #[tokio::test]
    async fn ping_and_pong_before_ready_cannot_extend_handshake_deadline() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (_handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let deadline = Instant::now() + Duration::from_millis(150);
        let mut task = tokio::spawn(run_connected_with_ready_deadline(
            client,
            capabilities,
            input,
            sender,
            deadline,
        ));
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Text(_)
        ));
        let mut messages = 0;
        let mut heartbeat = tokio::time::interval(Duration::from_millis(10));
        let result = timeout(Duration::from_millis(350), async {
            loop {
                tokio::select! {
                    biased;
                    result = &mut task => break result.unwrap(),
                    _ = heartbeat.tick() => {
                        let message = if messages % 2 == 0 { Message::Ping(vec![1]) } else { Message::Pong(vec![1]) };
                        if server.send(message).await.is_ok() {
                            messages += 1;
                        }
                    }
                }
            }
        }).await;
        if result.is_err() {
            task.abort();
            let _ = task.await;
        }
        assert_eq!(result.unwrap(), Err(SessionError::Timeout));
        assert!(messages >= 2);
        assert_eq!(
            receiver
                .try_iter()
                .filter(|event| matches!(event, SessionEvent::Failed(SessionError::Timeout)))
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn cumulative_credit_blocks_audio_but_keeps_receiver_independent() {
        let (client, mut server) = pair().await;
        let capabilities = caps(4);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 4).await;
        handle.try_audio(vec![1; 8]).unwrap();
        handle.try_audio(vec![2; 8]).unwrap();
        handle.try_audio(vec![3; 8]).unwrap();
        handle.finish().unwrap();
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(_)
        ));
        assert!(timeout(Duration::from_millis(40), server.next())
            .await
            .is_err());
        let flow = event(
            "flow",
            2,
            serde_json::json!({"audio_received_samples":4,"audio_processed_samples":4,"audio_send_limit":8}),
        );
        server.send(flow.clone()).await.unwrap();
        server.send(flow).await.unwrap();
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(_)
        ));
        assert!(timeout(Duration::from_millis(40), server.next())
            .await
            .is_err());
        server.send(event("flow",4,serde_json::json!({"audio_received_samples":8,"audio_processed_samples":8,"audio_send_limit":12}))).await.unwrap();
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(_)
        ));
        let finish: serde_json::Value =
            serde_json::from_str(server.next().await.unwrap().unwrap().to_text().unwrap()).unwrap();
        assert_eq!(
            finish,
            serde_json::json!({"type":"finish","after_audio_samples":12})
        );
        server.send(event("final",5,serde_json::json!({"text":"done","pending":"","audio_processed_samples":12,"reason":"finished","complete":true}))).await.unwrap();
        assert_eq!(
            task.await.unwrap().unwrap(),
            SessionOutcome::Finished { complete: true }
        );
        assert_eq!(handle.buffered_samples(), 0);
        let seen: Vec<_> = receiver.try_iter().collect();
        let finished = seen
            .iter()
            .position(|event| matches!(event, SessionEvent::Finished { .. }))
            .unwrap();
        assert!(matches!(
            seen[finished - 1],
            SessionEvent::Progress(Progress {
                processed_samples: 12,
                received_samples: 12,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn flush_and_finish_follow_audio_in_the_same_fifo() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, _) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 32000).await;
        handle.try_audio(vec![1; 4]).unwrap();
        handle.try_audio(vec![2; 4]).unwrap();
        handle.flush().unwrap();
        handle.try_audio(vec![3; 4]).unwrap();
        handle.finish().unwrap();
        assert_eq!(
            handle.try_audio(vec![4; 4]),
            Err(SessionError::SessionClosed)
        );
        assert_eq!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(vec![1; 4])
        );
        assert_eq!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(vec![2; 4])
        );
        let flush: serde_json::Value =
            serde_json::from_str(server.next().await.unwrap().unwrap().to_text().unwrap()).unwrap();
        assert_eq!(
            flush,
            serde_json::json!({"type":"flush","after_audio_samples":4})
        );
        assert_eq!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(vec![3; 4])
        );
        let finish: serde_json::Value =
            serde_json::from_str(server.next().await.unwrap().unwrap().to_text().unwrap()).unwrap();
        assert_eq!(
            finish,
            serde_json::json!({"type":"finish","after_audio_samples":6})
        );
        server.send(event("final",2,serde_json::json!({"text":"","pending":"","audio_processed_samples":6,"reason":"finished","complete":true}))).await.unwrap();
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn cancel_during_exhausted_credit_ignores_late_text() {
        let (client, mut server) = pair().await;
        let capabilities = caps(1);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 1).await;
        handle.try_audio(vec![1; 2]).unwrap();
        handle.try_audio(vec![2; 2]).unwrap();
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(_)
        ));
        handle.cancel();
        assert!(!handle.delivery_gate().is_open());
        let _ = server
            .send(event(
                "partial",
                2,
                serde_json::json!({"text":"late","pending":"","audio_processed_samples":1}),
            ))
            .await;
        assert_eq!(
            timeout(Duration::from_secs(1), task)
                .await
                .unwrap()
                .unwrap()
                .unwrap(),
            SessionOutcome::Cancelled
        );
        assert!(!receiver
            .try_iter()
            .any(|event| matches!(event, SessionEvent::Text(_) | SessionEvent::Finished { .. })));
        assert_eq!(handle.buffered_samples(), 0);
    }

    #[tokio::test]
    async fn disconnect_ends_without_replay_and_reports_once() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (_handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 32000).await;
        drop(server);
        assert_eq!(task.await.unwrap(), Err(SessionError::ConnectionLost));
        assert_eq!(
            receiver
                .try_iter()
                .filter(|event| matches!(event, SessionEvent::Failed(_)))
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn client_capture_failure_is_typed_and_reported_once() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 32000).await;
        handle.fail_client(SessionError::CaptureFailed);
        assert_eq!(task.await.unwrap(), Err(SessionError::CaptureFailed));
        assert!(!handle.delivery_gate().is_open());
        assert_eq!(
            receiver
                .try_iter()
                .filter(|event| matches!(event, SessionEvent::Failed(SessionError::CaptureFailed)))
                .count(),
            1
        );
    }

    #[tokio::test]
    async fn server_model_change_keeps_valid_fixed_snapshot_without_success_final() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 32000).await;
        handle.try_audio(vec![1; 4]).unwrap();
        assert!(matches!(
            server.next().await.unwrap().unwrap(),
            Message::Binary(_)
        ));
        server.send(event("error",2,serde_json::json!({"text":"fixed","pending":"","audio_processed_samples":2,"code":"MODEL_CHANGED","message":"untrusted message","complete":false,"retryable":false,"retry_after_ms":null}))).await.unwrap();
        assert_eq!(
            task.await.unwrap(),
            Err(SessionError::Remote {
                code: crate::protocol::ServiceCode::ModelChanged,
                retryable: false,
                retry_after_ms: None
            })
        );
        let seen: Vec<_> = receiver.try_iter().collect();
        assert!(seen
            .iter()
            .any(|event| matches!(event,SessionEvent::Text(update) if update.fixed == "fixed")));
        assert_eq!(
            seen.iter()
                .filter(|event| matches!(event, SessionEvent::Failed(_)))
                .count(),
            1
        );
        assert!(!seen
            .iter()
            .any(|event| matches!(event, SessionEvent::Finished { .. })));
        assert!(!handle.delivery_gate().is_open());
    }

    #[tokio::test]
    async fn prefix_failure_keeps_previous_text_and_closes_delivery_gate() {
        let (client, mut server) = pair().await;
        let capabilities = caps(32000);
        let (handle, input) = session_channel(&capabilities).unwrap();
        let (sender, receiver) = events::channel();
        let task = tokio::spawn(run_connected(client, capabilities, input, sender));
        hello(&mut server, 32000).await;
        server.send(event("partial",2,serde_json::json!({"text":"original","pending":"candidate","audio_processed_samples":0}))).await.unwrap();
        server
            .send(event(
                "partial",
                3,
                serde_json::json!({"text":"replacement","pending":"","audio_processed_samples":0}),
            ))
            .await
            .unwrap();
        assert_eq!(task.await.unwrap(), Err(SessionError::TextPrefixMismatch));
        let fixed: Vec<_> = receiver
            .try_iter()
            .filter_map(|event| match event {
                SessionEvent::Text(update) => Some(update.fixed),
                _ => None,
            })
            .collect();
        assert_eq!(fixed, vec!["original"]);
        assert!(!handle.delivery_gate().is_open());
    }
}
