package com.oneaxe.pocket.voicelab;

import java.util.ArrayDeque;
import org.json.JSONException;
import org.json.JSONObject;

final class MobileProtocol {
    static final int SAMPLE_RATE = 16000;
    static final int FRAME_BYTES = 3200;

    interface Listener {
        void onText(String fixedText, String pending);
        void onTerminal(String message, boolean complete);
    }

    static final class Capability {
        final String instance;
        final String generation;
        final int maxFrameBytes;
        final int bufferBytes;
        final int windowBytes;
        final int maxSamples;
        final String unavailable;

        Capability(JSONObject json) throws JSONException {
            if (json.getInt("protocol_version") != 1) throw new JSONException("protocol_version");
            instance = required(json, "server_instance_id");
            String state = required(json, "model_state");
            if (!state.equals("unloaded") && !state.equals("loading") && !state.equals("ready")
                    && !state.equals("unloading") && !state.equals("error")) throw new JSONException("model_state");
            if (json.getInt("max_sessions") < 1 || json.getInt("mobile_slots_available") < 0) throw new JSONException("capacity");
            JSONObject audio = json.getJSONObject("audio");
            if (!"pcm_s16le".equals(audio.getString("encoding")) || audio.getInt("sample_rate") != SAMPLE_RATE
                    || audio.getInt("channels") != 1) throw new JSONException("audio format");
            maxFrameBytes = audio.getInt("max_frame_bytes");
            JSONObject flow = json.getJSONObject("flow");
            int bufferMs = flow.getInt("client_buffer_max_ms");
            long windowSamples = flow.getLong("window_samples");
            if (maxFrameBytes < 640 || maxFrameBytes > 65536 || (maxFrameBytes & 1) != 0
                    || windowSamples < 320 || bufferMs < 80 || bufferMs > 30000
                    || json.getInt("session_max_seconds") < 1) throw new JSONException("limits");
            bufferBytes = bufferMs * SAMPLE_RATE * 2 / 1000;
            windowBytes = (int) Math.min(Integer.MAX_VALUE - 1L, windowSamples * 2L);
            maxSamples = Math.multiplyExact(json.getInt("session_max_seconds"), SAMPLE_RATE);
            boolean ready = json.getBoolean("ready");
            boolean supported = json.getBoolean("stream_supported");
            boolean canStart = json.getBoolean("can_start");
            String reason = json.isNull("unavailable_reason") ? "" : json.getString("unavailable_reason");
            generation = ready ? required(json, "model_generation") : "";
            if (ready && !state.equals("ready")) throw new JSONException("ready state");
            if (canStart && (!ready || !supported || json.getInt("mobile_slots_available") < 1))
                throw new JSONException("can_start");
            unavailable = canStart ? "" : (!reason.isEmpty() ? reason :
                    !ready ? "MODEL_NOT_READY" : !supported ? "MODEL_UNSUPPORTED" : "CAPACITY_EXCEEDED");
        }

        int frameBytes() { return Math.min(FRAME_BYTES, Math.min(maxFrameBytes, Math.min(bufferBytes, windowBytes))); }
    }

    static final class Session {
        private final Capability capability;
        private final Listener listener;
        private final ArrayDeque<byte[]> queued = new ArrayDeque<>();
        private String sessionId;
        private String fixed = "";
        private long lastSeq;
        private long sent;
        private long sendLimit;
        private int bufferedBytes;
        private int inFlightBytes;
        private boolean finishing;
        private boolean finishedControl;
        private boolean cancelled;
        private boolean terminal;
        private String terminalMessage;

        Session(Capability capability, Listener listener) {
            this.capability = capability;
            this.listener = listener;
        }

        synchronized JSONObject start() throws JSONException {
            return new JSONObject().put("type", "start").put("protocol_version", 1)
                    .put("expected_server_instance_id", capability.instance)
                    .put("expected_model_generation", capability.generation)
                    .put("audio", new JSONObject().put("encoding", "pcm_s16le")
                            .put("sample_rate", SAMPLE_RATE).put("channels", 1));
        }

        synchronized void receive(JSONObject event) throws JSONException {
            String type = required(event, "type");
            if (sessionId == null) {
                if (type.equals("error")) {
                    if (!event.isNull("session_id") || event.getLong("seq") != 0) throw new JSONException("pre-session error");
                    terminal = true;
                    terminalMessage = errorMessage(event.optString("code"));
                    notifyAll();
                    return;
                }
                if (!type.equals("ready") || !capability.instance.equals(required(event, "server_instance_id"))
                        || !capability.generation.equals(required(event, "model_generation"))
                        || event.getLong("seq") != 1 || event.getLong("audio_received_samples") != 0
                        || event.getLong("audio_processed_samples") != 0) throw new JSONException("ready");
                sessionId = required(event, "session_id");
                sendLimit = nonnegative(event, "audio_send_limit");
                if (sendLimit * 2 < capability.frameBytes()) throw new JSONException("initial flow window");
                lastSeq = 1;
                notifyAll();
                return;
            }
            if (!capability.instance.equals(required(event, "server_instance_id"))
                    || !capability.generation.equals(required(event, "model_generation"))
                    || !sessionId.equals(required(event, "session_id"))) throw new JSONException("session identity");
            long seq = event.getLong("seq");
            if (seq <= lastSeq || cancelled || terminal) return;
            lastSeq = seq;
            if (type.equals("flow") || type.equals("ready")) {
                if (type.equals("ready")) throw new JSONException("duplicate ready");
                long received = nonnegative(event, "audio_received_samples");
                long processed = nonnegative(event, "audio_processed_samples");
                long limit = nonnegative(event, "audio_send_limit");
                if (processed > received || received > sent || limit < sendLimit) throw new JSONException("flow counts");
                sendLimit = limit;
                notifyAll();
            } else if (type.equals("partial") || type.equals("final") || type.equals("error")) {
                long processed = type.equals("error") && !event.has("audio_processed_samples") ? 0
                        : nonnegative(event, "audio_processed_samples");
                if (processed > sent) throw new JSONException("processed count");
                String text = event.getString("text");
                if (!text.startsWith(fixed)) {
                    terminal("识别文字前缀发生变化，已停止自动输入", false);
                    return;
                }
                fixed = text;
                listener.onText(fixed, event.getString("pending"));
                if (type.equals("final")) terminal("finished".equals(event.getString("reason"))
                        && event.getBoolean("complete") ? "听写完成" : "听写已结束，尾部可能未完成",
                        event.getBoolean("complete"));
                else if (type.equals("error")) terminal(errorMessage(event.optString("code")), false);
            } else if (!type.equals("keepalive")) {
                throw new JSONException("event type");
            }
        }

        synchronized boolean offer(byte[] audio, long socketQueuedBytes) {
            if (sessionId == null || cancelled || terminal || finishing || audio == null || audio.length < 2
                    || audio.length > capability.maxFrameBytes || (audio.length & 1) != 0) return false;
            if (sent + (bufferedBytes + audio.length) / 2L > capability.maxSamples
                    || bufferedBytes + inFlightBytes + audio.length + socketQueuedBytes > capability.bufferBytes) {
                cancel("音频积压或时长超过上限，本轮已停止；已收到的文字保留");
                return false;
            }
            queued.add(audio.clone());
            bufferedBytes += audio.length;
            notifyAll();
            return true;
        }

        synchronized byte[] takeSendable() throws InterruptedException {
            while (!cancelled && !terminal) {
                byte[] frame = queued.peek();
                if (frame != null && sent + frame.length / 2 <= sendLimit) {
                    queued.remove();
                    bufferedBytes -= frame.length;
                    inFlightBytes += frame.length;
                    sent += frame.length / 2;
                    return frame;
                }
                if (frame == null && finishing && !finishedControl) return null;
                wait(250);
                return null;
            }
            return null;
        }

        synchronized void finish() {
            if (!cancelled && !terminal) { finishing = true; notifyAll(); }
        }

        synchronized void sentToSocket(int bytes) {
            inFlightBytes -= bytes;
            if (inFlightBytes < 0) throw new IllegalStateException("in-flight audio count");
        }

        synchronized JSONObject finishControl() throws JSONException {
            if (cancelled || terminal || !finishing || !queued.isEmpty() || finishedControl) return null;
            finishedControl = true;
            return new JSONObject().put("type", "finish").put("after_audio_samples", sent);
        }

        synchronized void cancel(String message) {
            if (cancelled || terminal) return;
            cancelled = true;
            queued.clear();
            bufferedBytes = 0;
            listener.onTerminal(message, false);
            notifyAll();
        }

        synchronized boolean isCancelled() { return cancelled; }
        synchronized boolean isTerminal() { return terminal; }
        int bufferBytes() { return capability.bufferBytes; }
        synchronized String terminalMessage() { return terminalMessage; }
        synchronized boolean hasReady() { return sessionId != null; }
        int frameBytes() { return capability.frameBytes(); }
        synchronized long sentSamples() { return sent; }

        synchronized String metricsSummary(long socketQueuedBytes) {
            return "sent=" + sent + " limit=" + sendLimit
                    + " queued=" + ((bufferedBytes + inFlightBytes + socketQueuedBytes) / 2)
                    + " fixed_chars=" + fixed.length();
        }

        synchronized boolean awaitTerminal(long millis) throws InterruptedException {
            long deadline = System.nanoTime() + millis * 1000000L;
            while (!terminal && !cancelled) {
                long remaining = deadline - System.nanoTime();
                if (remaining <= 0) return false;
                wait(Math.max(1L, remaining / 1000000L));
            }
            return terminal;
        }

        synchronized void disconnected() {
            if (!cancelled && !terminal) terminal("连接中断，尾部可能未完成；已收到的文字保留", false);
        }

        private void terminal(String message, boolean complete) {
            if (terminal || cancelled) return;
            terminal = true;
            terminalMessage = message;
            listener.onTerminal(message, complete);
            notifyAll();
        }
    }

    static String errorMessage(String code) {
        switch (code) {
            case "UNAUTHORIZED": return "手机凭据无效或已吊销，请在电脑端处理";
            case "FORBIDDEN": return "手机凭据缺少听写权限";
            case "MODEL_NOT_READY": return "电脑端模型尚未就绪";
            case "MODEL_UNSUPPORTED": return "电脑端当前模型不支持手机流式听写";
            case "MODEL_CHANGED": return "电脑端模型已变化，请重新开始听写";
            case "CAPACITY_EXCEEDED": return "当前没有可用的手机听写名额";
            case "SESSION_TIMEOUT": case "SESSION_LIMIT": return "本轮听写已超时，尾部可能未完成";
            case "FLOW_CONTROL_EXCEEDED": case "RATE_LIMITED": return "本轮音频超出服务端限制";
            default: return "手机听写服务暂时不可用";
        }
    }

    private static String required(JSONObject json, String key) throws JSONException {
        String value = json.getString(key);
        if (value.isEmpty() || value.equals("null")) throw new JSONException(key);
        return value;
    }

    private static long nonnegative(JSONObject json, String key) throws JSONException {
        long value = json.getLong(key);
        if (value < 0) throw new JSONException(key);
        return value;
    }

    private MobileProtocol() {}
}
