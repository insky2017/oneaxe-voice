package com.oneaxe.pocket.voicelab;

import java.net.Inet6Address;
import java.net.InetAddress;
import java.util.Locale;

final class TailnetEndpoint {
    final String scheme;
    final String host;
    final int port;

    private TailnetEndpoint(String scheme, String host, int port) {
        this.scheme = scheme;
        this.host = host;
        this.port = port;
    }

    static TailnetEndpoint validate(String scheme, String host, int port) {
        if (!"http".equals(scheme) && !"https".equals(scheme)) {
            throw new IllegalArgumentException("协议只能是 HTTP 或 HTTPS");
        }
        if (port < 1 || port > 65535) throw new IllegalArgumentException("端口必须在 1–65535 之间");
        if (host == null || host.isEmpty() || !host.equals(host.trim())) {
            throw new IllegalArgumentException("请填写完整的 Tailnet 主机名或 IP");
        }
        String normalized = host.toLowerCase(Locale.ROOT);
        if (normalized.endsWith(".")) normalized = normalized.substring(0, normalized.length() - 1);
        if (normalized.indexOf(':') >= 0) {
            if (normalized.indexOf('%') >= 0 || normalized.indexOf('[') >= 0 || normalized.indexOf(']') >= 0) {
                throw new IllegalArgumentException("IPv6 地址不能带区域标识或方括号");
            }
            try {
                InetAddress address = InetAddress.getByName(normalized);
                if (!(address instanceof Inet6Address) || !isTailnetAddress(address.getAddress())) {
                    throw new IllegalArgumentException("只允许 Tailscale 的 fd7a:115c:a1e0::/48 地址");
                }
            } catch (java.net.UnknownHostException e) {
                throw new IllegalArgumentException("无效的 IPv6 地址", e);
            }
        } else if (normalized.matches("[0-9.]+")) {
            String[] parts = normalized.split("\\.", -1);
            if (parts.length != 4) throw new IllegalArgumentException("无效的 IPv4 地址");
            int[] octets = new int[4];
            for (int i = 0; i < 4; i++) {
                if (parts[i].isEmpty() || parts[i].length() > 3 ||
                        (parts[i].length() > 1 && parts[i].charAt(0) == '0')) {
                    throw new IllegalArgumentException("无效的 IPv4 地址");
                }
                octets[i] = Integer.parseInt(parts[i]);
                if (octets[i] > 255) throw new IllegalArgumentException("无效的 IPv4 地址");
            }
            if (octets[0] != 100 || octets[1] < 64 || octets[1] > 127) {
                throw new IllegalArgumentException("只允许 Tailscale 的 100.64.0.0/10 地址");
            }
        } else {
            if (normalized.length() > 253 || !normalized.endsWith(".ts.net") ||
                    normalized.length() <= ".ts.net".length()) {
                throw new IllegalArgumentException("只允许完整的 *.ts.net MagicDNS 主机名");
            }
            for (String label : normalized.split("\\.", -1)) {
                if (label.isEmpty() || label.length() > 63 ||
                        !label.matches("[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")) {
                    throw new IllegalArgumentException("无效的 MagicDNS 主机名");
                }
            }
        }
        return new TailnetEndpoint(scheme, normalized, port);
    }

    static boolean isTailnetAddress(byte[] bytes) {
        if (bytes.length == 4) {
            return (bytes[0] & 255) == 100 && (bytes[1] & 255) >= 64 && (bytes[1] & 255) <= 127;
        }
        return bytes.length == 16 && (bytes[0] & 255) == 0xfd &&
                (bytes[1] & 255) == 0x7a && (bytes[2] & 255) == 0x11 &&
                (bytes[3] & 255) == 0x5c && (bytes[4] & 255) == 0xa1 &&
                (bytes[5] & 255) == 0xe0;
    }

    String baseUrl() {
        String urlHost = host.indexOf(':') >= 0 ? "[" + host + "]" : host;
        return scheme + "://" + urlHost + ":" + port;
    }
}
