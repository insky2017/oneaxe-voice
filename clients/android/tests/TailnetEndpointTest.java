package com.oneaxe.pocket.voicelab;

final class TailnetEndpointTest {
    private static void allow(String host) {
        TailnetEndpoint.validate("http", host, 8097);
    }

    private static void reject(String host) {
        try {
            allow(host);
            throw new AssertionError("accepted: " + host);
        } catch (IllegalArgumentException expected) {
            // Expected.
        }
    }

    public static void main(String[] args) {
        allow("100.64.0.1");
        allow("100.127.255.254");
        allow("fd7a:115c:a1e0::1");
        allow("rtx4090.nase-stairs.ts.net");
        allow("RTX4090.NASE-STAIRS.TS.NET.");
        TailnetEndpoint.validate("https", "rtx4090.nase-stairs.ts.net", 8097);
        reject("127.0.0.1");
        reject("localhost");
        reject("192.168.1.6");
        reject("8.8.8.8");
        reject("100.63.255.255");
        reject("100.128.0.1");
        reject("100.076.106.96");
        reject("fd7a:115c:a1df::1");
        reject("fd7a:115c:a1e1::1");
        reject("fd7a:115c:a1e0::1%eth0");
        reject("http://100.76.106.96:8097");
        reject("user@rtx4090.nase-stairs.ts.net");
        reject("rtx4090.nase-stairs.ts.net/path");
        reject("rtx4090.nase-stairs.ts.net?x=1");
        reject("rtx4090.nase-stairs.ts.net#x");
        reject("rtx4090.nase-stairs.ts.net.evil.com");
        reject("-bad.ts.net");
        for (int port : new int[]{0, -1, 65536}) {
            try {
                TailnetEndpoint.validate("http", "100.76.106.96", port);
                throw new AssertionError("accepted port: " + port);
            } catch (IllegalArgumentException expected) {
                // Expected.
            }
        }
        try {
            TailnetEndpoint.validate("ftp", "100.76.106.96", 8097);
            throw new AssertionError("accepted unsupported scheme");
        } catch (IllegalArgumentException expected) {
            // Expected.
        }
    }
}
