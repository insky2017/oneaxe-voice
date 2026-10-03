package com.oneaxe.pocket.voicelab;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.util.Log;
import okhttp3.Dns;
import okhttp3.Call;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetAddress;
import java.net.Proxy;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.List;
import java.util.ArrayDeque;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import javax.net.ssl.SSLException;

final class VoiceClient {
    interface Listener extends MobileProtocol.Listener {}

    static final class Opening {
        private boolean cancelled;
        private Call call;
        private WebSocket socket;
        private Stream stream;
        private CountDownLatch waiting;

        synchronized boolean isCancelled() { return cancelled; }

        synchronized void bind(Call pending) {
            call = pending;
            if (cancelled) pending.cancel();
        }

        synchronized void clear(Call completed) {
            if (call == completed) call = null;
        }

        synchronized void bind(WebSocket pending, CountDownLatch latch) {
            socket = pending;
            waiting = latch;
            if (cancelled) { pending.cancel(); latch.countDown(); }
        }

        synchronized boolean handoff(Stream readyStream) {
            if (cancelled) { readyStream.cancel(); return false; }
            stream = readyStream;
            socket = null;
            waiting = null;
            return true;
        }

        synchronized void cancel() {
            if (cancelled) return;
            cancelled = true;
            if (call != null) call.cancel();
            if (socket != null) socket.cancel();
            if (stream != null) stream.cancel();
            if (waiting != null) waiting.countDown();
        }
    }

    static String capabilitySummary(Context context) throws Exception {
        try {
            ConnectionSettings settings = settingsSnapshot(context);
            MobileProtocol.Capability capability = capabilities(context, settings, null);
            return capability.unavailable.isEmpty() ? "电脑端模型已就绪，可开始手机听写" :
                    MobileProtocol.errorMessage(capability.unavailable);
        } catch (IOException e) {
            throw connectionError(e);
        }
    }

    static Stream open(Context context, Listener listener) throws Exception {
        return open(context, listener, new Opening());
    }

    static Stream open(Context context, Listener listener, Opening opening) throws Exception {
        if (opening.isCancelled()) throw new DiagnosticException("听写连接已取消");
        ConnectionSettings settings = settingsSnapshot(context);
        TailnetEndpoint endpoint = settings.endpoint;
        MobileProtocol.Capability capability;
        try { capability = capabilities(context, settings, opening); }
        catch (IOException e) { throw connectionError(e); }
        if (opening.isCancelled()) throw new DiagnosticException("听写连接已取消");
        if (!capability.unavailable.isEmpty()) throw new DiagnosticException(MobileProtocol.errorMessage(capability.unavailable));
        String token = settings.token;
        MobileProtocol.Session session = new MobileProtocol.Session(capability, listener);
        OkHttpClient client = mobileClient(context, endpoint).newBuilder().readTimeout(0, TimeUnit.SECONDS).build();
        CountDownLatch ready = new CountDownLatch(1);
        AtomicReference<Exception> failure = new AtomicReference<>();
        AtomicReference<Stream> result = new AtomicReference<>();
        AtomicLong lastFlowLogNs = new AtomicLong();
        Request request = new Request.Builder().url(endpoint.baseUrl() + "/api/mobile/v1/dictation/stream")
                .header("Authorization", "Bearer " + token).build();
        WebSocket socket = client.newWebSocket(request, new WebSocketListener() {
            @Override public void onOpen(WebSocket socket, Response response) {
                if (opening.isCancelled()) { socket.cancel(); ready.countDown(); return; }
                result.set(new Stream(session, new OkHttpWire(socket)));
                try { if (!socket.send(session.start().toString())) throw new IOException("start send failed"); }
                catch (Exception e) { failure.set(new DiagnosticException("无法发送手机听写开始请求", e)); ready.countDown(); socket.cancel(); }
            }

            @Override public void onMessage(WebSocket socket, String message) {
                try {
                    JSONObject event = new JSONObject(message);
                    session.receive(event);
                    String type = event.optString("type");
                    long now = System.nanoTime();
                    if (!"flow".equals(type) || now - lastFlowLogNs.get() >= TimeUnit.SECONDS.toNanos(1)) {
                        if ("flow".equals(type)) lastFlowLogNs.set(now);
                        Log.d("VoiceLabStream", "type=" + type + " seq=" + event.optLong("seq", -1)
                                + " session=" + event.optString("session_id", "")
                                + " processed=" + event.optLong("audio_processed_samples", -1)
                                + " " + session.metricsSummary(socket.queueSize()));
                    }
                    if (session.hasReady() || session.isTerminal()) ready.countDown();
                } catch (Exception e) {
                    failure.set(new DiagnosticException("手机听写协议响应不正确", e));
                    ready.countDown();
                    if (session.hasReady()) session.disconnected();
                    socket.cancel();
                }
            }

            @Override public void onMessage(WebSocket socket, ByteString bytes) {
                failure.set(new DiagnosticException("手机听写协议收到意外二进制响应"));
                ready.countDown();
                if (session.hasReady()) session.disconnected();
                socket.cancel();
            }

            @Override public void onFailure(WebSocket socket, Throwable error, Response response) {
                if (response != null && response.code() == 401) failure.set(new DiagnosticException("手机凭据无效或已吊销"));
                else if (response != null && response.code() == 403) failure.set(new DiagnosticException("手机凭据缺少听写权限"));
                else if (error instanceof SSLException) failure.set(new DiagnosticException("Voice HTTPS 证书或握手失败，请检查主机名和证书"));
                else failure.set(new DiagnosticException("手机听写连接失败，请检查 Tailnet、主机和端口"));
                ready.countDown();
                if (session.hasReady()) session.disconnected();
            }

            @Override public void onClosing(WebSocket socket, int code, String reason) {
                socket.close(code, null);
                if (session.hasReady()) session.disconnected();
            }

            @Override public void onClosed(WebSocket socket, int code, String reason) {
                ready.countDown();
                if (session.hasReady()) session.disconnected();
            }
        });
        opening.bind(socket, ready);
        if (!ready.await(8, TimeUnit.SECONDS)) {
            opening.cancel();
            throw new DiagnosticException("等待手机听写会话就绪超时");
        }
        if (failure.get() != null) throw failure.get();
        if (opening.isCancelled()) throw new DiagnosticException("听写连接已取消");
        if (!session.hasReady()) throw new DiagnosticException(session.terminalMessage() == null ?
                "电脑端未能建立手机听写会话" : session.terminalMessage());
        Stream stream = result.get();
        if (!opening.handoff(stream)) throw new DiagnosticException("听写连接已取消");
        stream.startSender();
        return stream;
    }

    static final class Stream {
        private final MobileProtocol.Session session;
        private final Wire wire;
        private final AtomicBoolean cancelSent = new AtomicBoolean();
        private final Object sendLock = new Object();
        private final ArrayDeque<Long> messageTimes = new ArrayDeque<>();
        private Thread sender;

        Stream(MobileProtocol.Session session, Wire wire) {
            this.session = session;
            this.wire = wire;
        }

        int frameBytes() { return session.frameBytes(); }

        boolean offerAudio(byte[] frame) {
            boolean accepted;
            synchronized (sendLock) {
                accepted = session.offer(frame, wire.queueSize());
            }
            if (!accepted && session.isCancelled()) sendCancel();
            return accepted;
        }

        void finish() {
            session.finish();
            Thread thread = sender;
            if (thread == null) return;
            try {
                thread.join(30000);
                if (thread.isAlive() || !session.awaitTerminal(30000)) {
                    session.disconnected();
                    wire.cancel();
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                cancel();
            }
        }

        void cancel() {
            synchronized (sendLock) {
                if (session.isTerminal()) return;
                session.cancel("听写已取消；已收到的文字保留");
                sendCancel();
            }
        }

        private void sendCancel() {
            if (!cancelSent.compareAndSet(false, true)) return;
            synchronized (sendLock) {
                wire.sendText("{\"type\":\"cancel\"}");
                wire.close(1000, "cancelled");
            }
        }

        void startSender() {
            sender = new Thread(() -> {
                try {
                    long lastKeepalive = System.nanoTime();
                    while (!session.isCancelled() && !session.isTerminal()) {
                        byte[] frame = session.takeSendable();
                        if (frame == null) {
                            JSONObject control = session.finishControl();
                            if (control != null) {
                                synchronized (sendLock) {
                                    if (!session.isCancelled() && !session.isTerminal() && !wire.sendText(control.toString()))
                                        throw new IOException("finish send failed");
                                }
                            }
                            if (System.nanoTime() - lastKeepalive >= TimeUnit.SECONDS.toNanos(10)) {
                                synchronized (sendLock) {
                                    if (!session.isCancelled() && !session.isTerminal()
                                            && messageTimes.size() < 100) wire.sendText("{\"type\":\"keepalive\"}");
                                }
                                lastKeepalive = System.nanoTime();
                            }
                            continue;
                        }
                        synchronized (sendLock) {
                            if (session.isCancelled() || session.isTerminal()) break;
                            long now = System.nanoTime();
                            while (!messageTimes.isEmpty() && now - messageTimes.peekFirst() >= TimeUnit.SECONDS.toNanos(1))
                                messageTimes.removeFirst();
                            if (messageTimes.size() >= 99 || wire.queueSize() + frame.length > session.bufferBytes()) {
                                session.cancel("音频发送积压或速率超限，本轮已停止；已收到的文字保留");
                                sendCancel();
                                break;
                            }
                            if (!wire.sendAudio(frame)) throw new IOException("audio send failed");
                            session.sentToSocket(frame.length);
                            messageTimes.addLast(now);
                        }
                    }
                } catch (Exception e) {
                    session.disconnected();
                    wire.cancel();
                }
            }, "voice-mobile-sender");
            sender.start();
        }
    }

    interface Wire {
        boolean sendText(String value);
        boolean sendAudio(byte[] value);
        long queueSize();
        void close(int code, String reason);
        void cancel();
    }

    private static final class OkHttpWire implements Wire {
        private final WebSocket socket;
        OkHttpWire(WebSocket socket) { this.socket = socket; }
        public boolean sendText(String value) { return socket.send(value); }
        public boolean sendAudio(byte[] value) { return socket.send(ByteString.of(value)); }
        public long queueSize() { return socket.queueSize(); }
        public void close(int code, String reason) { socket.close(code, reason); }
        public void cancel() { socket.cancel(); }
    }

    private static final class ConnectionSettings {
        final TailnetEndpoint endpoint;
        final String token;
        ConnectionSettings(TailnetEndpoint endpoint, String token) {
            this.endpoint = endpoint;
            this.token = token;
        }
    }

    private static ConnectionSettings settingsSnapshot(Context context) throws Exception {
        TailnetEndpoint endpoint = Settings.endpoint(context);
        String token = Settings.token(context);
        if (!endpoint.baseUrl().equals(Settings.endpoint(context).baseUrl()))
            throw new DiagnosticException("Voice 连接设置刚刚变化，请重试");
        requireFormalEndpoint(endpoint);
        if (token.isEmpty()) throw new DiagnosticException("请先在设置中填写手机专用凭据");
        return new ConnectionSettings(endpoint, token);
    }

    private static MobileProtocol.Capability capabilities(Context context, ConnectionSettings settings,
            Opening opening) throws Exception {
        Request request = new Request.Builder().url(settings.endpoint.baseUrl() + "/api/mobile/v1/capabilities")
                .header("Authorization", "Bearer " + settings.token).get().build();
        Call call = mobileClient(context, settings.endpoint).newCall(request);
        if (opening != null) opening.bind(call);
        try (Response response = call.execute()) {
            if (response.code() == 401) throw new DiagnosticException("手机凭据无效或已吊销");
            if (response.code() == 403) throw new DiagnosticException("手机凭据缺少能力查询权限");
            if (!response.isSuccessful()) throw new DiagnosticException("Voice 能力查询失败（HTTP " + response.code() + "）");
            ResponseBody body = response.body();
            if (body == null || body.contentLength() > 16384) throw new DiagnosticException("Voice 能力响应为空或过大");
            try (InputStream input = body.byteStream()) {
                ByteArrayOutputStream buffer = new ByteArrayOutputStream();
                byte[] chunk = new byte[1024];
                int count;
                while ((count = input.read(chunk)) != -1) {
                    if (buffer.size() + count > 16384) throw new DiagnosticException("Voice 能力响应过大");
                    buffer.write(chunk, 0, count);
                }
                try { return new MobileProtocol.Capability(new JSONObject(buffer.toString(StandardCharsets.UTF_8.name()))); }
                catch (Exception e) { throw new DiagnosticException("Voice 能力响应格式不正确", e); }
            }
        } finally {
            if (opening != null) opening.clear(call);
        }
    }

    private static void requireFormalEndpoint(TailnetEndpoint endpoint) throws DiagnosticException {
        if (!"https".equals(endpoint.scheme) || !endpoint.host.endsWith(".ts.net"))
            throw new DiagnosticException("正式听写只支持 Tailnet DNS 主机名和 HTTPS；IP/HTTP 仅用于无凭据诊断");
    }

    private static OkHttpClient mobileClient(Context context, TailnetEndpoint endpoint) throws Exception {
        Network network = vpnNetwork(context);
        InetAddress[] addresses = tailnetAddresses(network, endpoint);
        List<InetAddress> pinned = Arrays.asList(addresses);
        Dns dns = hostname -> {
            if (!endpoint.host.equalsIgnoreCase(hostname))
                throw new java.net.UnknownHostException("Voice 主机名不匹配");
            return pinned;
        };
        return new OkHttpClient.Builder().dns(dns).socketFactory(network.getSocketFactory())
                .proxy(Proxy.NO_PROXY).followRedirects(false).followSslRedirects(false)
                .callTimeout(9, TimeUnit.SECONDS).connectTimeout(4, TimeUnit.SECONDS)
                .readTimeout(4, TimeUnit.SECONDS).build();
    }

    private static Network vpnNetwork(Context context) throws DiagnosticException {
        ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        Network network = manager == null ? null : manager.getActiveNetwork();
        NetworkCapabilities capabilities = network == null ? null : manager.getNetworkCapabilities(network);
        if (capabilities == null || !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN))
            throw new DiagnosticException("未检测到当前应用可用的 VPN。请手动连接 Pocket/Tailscale 后重试");
        return network;
    }

    private static InetAddress[] tailnetAddresses(Network network, TailnetEndpoint endpoint) throws Exception {
        InetAddress[] addresses;
        try { addresses = network.getAllByName(endpoint.host); }
        catch (IOException e) { throw new DiagnosticException("无法在当前 VPN 中解析 Tailnet 主机，请检查连接和主机名", e); }
        if (addresses.length == 0) throw new DiagnosticException("Tailnet 主机没有可用地址");
        for (InetAddress address : addresses) {
            if (!TailnetEndpoint.isTailnetAddress(address.getAddress()))
                throw new DiagnosticException("主机解析到非 Tailnet 地址，请检查主机配置");
        }
        return addresses;
    }
    private static DiagnosticException connectionError(IOException error) {
        if (error instanceof DiagnosticException) return (DiagnosticException) error;
        if (error instanceof SSLException)
            return new DiagnosticException("Voice HTTPS 证书或握手失败，请检查主机名和证书", error);
        if (error instanceof SocketTimeoutException)
            return new DiagnosticException("连接 Voice 主机超时，请检查 Tailnet、主机和端口", error);
        if (error instanceof java.net.ConnectException)
            return new DiagnosticException("无法连接 Voice 端口，请检查主机、端口和服务", error);
        if (error instanceof java.net.SocketException)
            return new DiagnosticException("Voice 网络不可达，请检查 Tailnet 连接和主机路由", error);
        return new DiagnosticException("无法查询 Voice 听写能力，请检查 Tailnet、主机和端口", error);
    }

    private static final class DiagnosticException extends IOException {
        DiagnosticException(String message) { super(message); }
        DiagnosticException(String message, Throwable cause) { super(message, cause); }
    }

    private VoiceClient() {}
}
