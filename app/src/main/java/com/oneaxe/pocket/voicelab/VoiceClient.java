package com.oneaxe.pocket.voicelab;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import okhttp3.Dns;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
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
import java.util.concurrent.TimeUnit;
import javax.net.ssl.SSLException;

final class VoiceClient {
    static String probe(Context context) throws Exception {
        try {
            return probeConnection(context);
        } catch (DiagnosticException e) {
            throw e;
        } catch (SSLException e) {
            throw new DiagnosticException("Voice HTTPS 证书或握手失败，请检查主机名、证书和电脑端 HTTPS 配置", e);
        } catch (SocketTimeoutException e) {
            throw new DiagnosticException("连接 Voice 主机超时，请检查 Tailnet 连接、主机和端口", e);
        } catch (java.net.ConnectException e) {
            throw new DiagnosticException("无法连接 Voice 端口，请检查主机、端口和服务是否开放", e);
        } catch (java.net.SocketException e) {
            throw new DiagnosticException("Voice 网络不可达，请检查 Tailnet 连接、主机路由和端口", e);
        } catch (IOException e) {
            throw new DiagnosticException("无法完成 Voice 连接，请检查 Tailnet 路由和电脑上的主机、端口是否开放", e);
        }
    }

    private static String probeConnection(Context context) throws Exception {
        TailnetEndpoint endpoint = Settings.endpoint(context);
        ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
        Network network = manager == null ? null : manager.getActiveNetwork();
        NetworkCapabilities capabilities = network == null ? null : manager.getNetworkCapabilities(network);
        if (capabilities == null || !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) {
            throw new DiagnosticException("未检测到当前应用可用的 VPN。请手动连接 Pocket/Tailscale 后重试");
        }

        InetAddress[] addresses;
        try {
            addresses = network.getAllByName(endpoint.host);
        } catch (IOException e) {
            throw new DiagnosticException("无法在当前 VPN 中解析 Tailnet 主机，请检查连接和主机名", e);
        }
        if (addresses.length == 0) throw new DiagnosticException("Tailnet 主机没有可用地址，请检查主机名");
        for (InetAddress address : addresses) {
            if (!TailnetEndpoint.isTailnetAddress(address.getAddress())) {
                throw new DiagnosticException("主机解析到非 Tailnet 地址，请检查主机配置");
            }
        }

        List<InetAddress> pinnedAddresses = Arrays.asList(addresses);
        Dns pinnedDns = hostname -> {
            if (!endpoint.host.equalsIgnoreCase(hostname)) {
                throw new java.net.UnknownHostException("Voice 主机名与已验证的 Tailnet 主机不一致");
            }
            return pinnedAddresses;
        };
        OkHttpClient client = new OkHttpClient.Builder()
                .dns(pinnedDns)
                .socketFactory(network.getSocketFactory())
                .proxy(Proxy.NO_PROXY)
                .followRedirects(false)
                .followSslRedirects(false)
                .callTimeout(9, TimeUnit.SECONDS)
                .connectTimeout(4, TimeUnit.SECONDS)
                .readTimeout(4, TimeUnit.SECONDS)
                .build();
        Request request = new Request.Builder().url(endpoint.baseUrl() + "/health").get().build();
        try (Response response = client.newCall(request).execute()) {
            int status = response.code();
            if (status == 401 || status == 403) {
                throw new DiagnosticException("Voice 入口拒绝访问（HTTP " + status + "），请检查服务端认证配置");
            }
            if (status >= 300 && status < 400) {
                throw new DiagnosticException("Voice 入口返回重定向（HTTP " + status + "），已拒绝跟随");
            }
            if (status != 200) {
                throw new DiagnosticException("Voice 健康检查返回 HTTP " + status + "，请检查主机、端口和服务");
            }
            ByteArrayOutputStream responseBuffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[1024];
            int count;
            ResponseBody body = response.body();
            if (body == null) throw new DiagnosticException("Voice 健康响应为空");
            try (InputStream input = body.byteStream()) {
                while ((count = input.read(chunk)) != -1) {
                    if (responseBuffer.size() + count > 4096) throw new DiagnosticException("Voice 健康响应过大");
                    responseBuffer.write(chunk, 0, count);
                }
            }
            JSONObject result;
            try {
                result = new JSONObject(responseBuffer.toString(StandardCharsets.UTF_8.name()));
            } catch (org.json.JSONException e) {
                throw new DiagnosticException("目标端口未返回 OneAxe Voice 健康响应", e);
            }
            if (!result.optBoolean("ok") || !"oneaxe-voice".equals(result.optString("service"))) {
                throw new DiagnosticException("目标端口未返回 OneAxe Voice 健康响应");
            }
            return "Voice 入口可达；模型和听写接口尚未验证";
        }
    }

    private static final class DiagnosticException extends IOException {
        DiagnosticException(String message) { super(message); }
        DiagnosticException(String message, Throwable cause) { super(message, cause); }
    }

    static void requireDictationReady(Context context) throws Exception {
        probe(context);
        throw new IllegalStateException("手机专用的当前模型听写接口尚未接入；当前不会发送录音");
    }

    static String transcribe(Context context, byte[] pcm) {
        throw new IllegalStateException("手机专用的当前模型听写接口尚未接入；当前不会发送录音");
    }

    private VoiceClient() {}
}
