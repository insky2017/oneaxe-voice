package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.*;

import java.util.ArrayList;
import java.util.List;
import org.json.JSONException;
import org.json.JSONObject;
import org.junit.Test;

public class MobileProtocolTest {
    static JSONObject capability(int frameBytes, int bufferMs) throws Exception {
        return new JSONObject("{\"protocol_version\":1,\"server_instance_id\":\"boot\","
                + "\"model_generation\":\"gen\",\"model_id\":\"R2T2\",\"mode\":\"r2t2\","
                + "\"model_state\":\"ready\",\"ready\":true,\"stream_supported\":true,"
                + "\"can_start\":true,\"unavailable_reason\":null,\"max_sessions\":2,"
                + "\"mobile_slots_available\":1,\"audio\":{\"encoding\":\"pcm_s16le\","
                + "\"sample_rate\":16000,\"channels\":1,\"max_frame_bytes\":" + frameBytes + "},"
                + "\"flow\":{\"window_samples\":32000,\"client_buffer_max_ms\":" + bufferMs + "},"
                + "\"session_max_seconds\":3600}");
    }

    static JSONObject event(String type, long seq) throws Exception {
        return new JSONObject().put("type", type).put("server_instance_id", "boot")
                .put("model_generation", "gen").put("session_id", "s").put("seq", seq);
    }

    private static MobileProtocol.Session ready(MobileProtocol.Capability capability, List<String> output) throws Exception {
        MobileProtocol.Session session = new MobileProtocol.Session(capability, new MobileProtocol.Listener() {
            public void onText(String text, String pending) { output.add("text:" + text + ":" + pending); }
            public void onTerminal(String message, boolean complete) { output.add("end:" + complete); }
        });
        assertEquals("boot", session.start().getString("expected_server_instance_id"));
        session.receive(event("ready", 1).put("audio_received_samples", 0)
                .put("audio_processed_samples", 0).put("audio_send_limit", 1600));
        return session;
    }

    @Test public void fixedTextIsCumulativeAndEventsAreDeduplicated() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Session session = ready(new MobileProtocol.Capability(capability(5120, 2000)), out);
        session.receive(event("partial", 2).put("audio_processed_samples", 0).put("text", "你好")
                .put("pending", "啊"));
        session.receive(event("partial", 2).put("audio_processed_samples", 0).put("text", "重复")
                .put("pending", ""));
        session.receive(event("final", 4).put("audio_processed_samples", 0).put("text", "你好世界")
                .put("pending", "").put("reason", "finished").put("complete", true));
        assertEquals(List.of("text:你好:啊", "text:你好世界:", "end:true"), out);
    }

    @Test public void prefixConflictStopsCommitAndCancelDropsLateText() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Session session = ready(new MobileProtocol.Capability(capability(5120, 2000)), out);
        session.receive(event("partial", 2).put("audio_processed_samples", 0).put("text", "已确认")
                .put("pending", ""));
        session.receive(event("partial", 3).put("audio_processed_samples", 0).put("text", "被改写")
                .put("pending", ""));
        session.receive(event("final", 4).put("audio_processed_samples", 0).put("text", "被改写更多")
                .put("pending", "").put("reason", "finished").put("complete", true));
        assertEquals(List.of("text:已确认:", "end:false"), out);
        List<String> cancelled = new ArrayList<>();
        MobileProtocol.Session second = ready(new MobileProtocol.Capability(capability(5120, 2000)), cancelled);
        second.cancel("cancelled");
        second.receive(event("partial", 2).put("audio_processed_samples", 0).put("text", "迟到")
                .put("pending", ""));
        assertEquals(List.of("end:false"), cancelled);
    }

    @Test public void generationAndSessionMustMatch() throws Exception {
        MobileProtocol.Session session = ready(new MobileProtocol.Capability(capability(5120, 2000)), new ArrayList<>());
        try {
            session.receive(event("flow", 2).put("model_generation", "other")
                    .put("audio_received_samples", 0).put("audio_processed_samples", 0)
                    .put("audio_send_limit", 3200));
            fail("generation mismatch accepted");
        } catch (JSONException expected) { }
    }

    @Test public void flowLimitIsCumulativeAndFinishUsesSentSamples() throws Exception {
        MobileProtocol.Session session = ready(new MobileProtocol.Capability(capability(5120, 2000)), new ArrayList<>());
        assertTrue(session.offer(new byte[3200], 0));
        assertNotNull(session.takeSendable());
        assertEquals(1600, session.sentSamples());
        assertTrue(session.offer(new byte[3200], 0));
        session.receive(event("flow", 2).put("audio_received_samples", 1600)
                .put("audio_processed_samples", 0).put("audio_send_limit", 1600));
        assertNull(session.takeSendable());
        assertEquals(1600, session.sentSamples());
        session.receive(event("flow", 3).put("audio_received_samples", 1600)
                .put("audio_processed_samples", 1600).put("audio_send_limit", 3200));
        assertNotNull(session.takeSendable());
        session.finish();
        assertEquals(3200, session.finishControl().getLong("after_audio_samples"));
        assertNull(session.finishControl());
    }

    @Test public void fullBufferStopsOnlySessionAndFrameLimitAdapts() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Capability capability = new MobileProtocol.Capability(capability(2560, 80));
        assertEquals(2560, capability.frameBytes());
        MobileProtocol.Session session = ready(capability, out);
        assertTrue(session.offer(new byte[2560], 0));
        assertFalse(session.offer(new byte[2], 0));
        assertTrue(session.isCancelled());
        assertEquals(List.of("end:false"), out);
    }

    @Test public void capabilityDeniesUnavailableAndRejectsMalformedFormats() throws Exception {
        JSONObject unavailable = capability(5120, 2000).put("can_start", false)
                .put("mobile_slots_available", 0).put("unavailable_reason", "CAPACITY_EXCEEDED");
        assertEquals("CAPACITY_EXCEEDED", new MobileProtocol.Capability(unavailable).unavailable);
        try {
            new MobileProtocol.Capability(capability(5120, 2000).put("can_start", true)
                    .put("mobile_slots_available", 0));
            fail("invalid capacity accepted");
        } catch (JSONException expected) { }
        try {
            new MobileProtocol.Capability(capability(5120, 2000).put("audio",
                    new JSONObject().put("encoding", "float32").put("sample_rate", 16000)
                            .put("channels", 1).put("max_frame_bytes", 5120)));
            fail("unsupported audio accepted");
        } catch (JSONException expected) { }
    }

    @Test public void handshakeErrorIsReportedByOpenWithoutEarlyTerminalCallback() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Session session = new MobileProtocol.Session(
                new MobileProtocol.Capability(capability(5120, 2000)), new MobileProtocol.Listener() {
                    public void onText(String fixed, String pending) { out.add(fixed); }
                    public void onTerminal(String message, boolean complete) { out.add(message); }
                });
        session.receive(event("error", 0).put("session_id", JSONObject.NULL)
                .put("code", "CAPACITY_EXCEEDED"));
        assertEquals("当前没有可用的手机听写名额", session.terminalMessage());
        assertTrue(out.isEmpty());
    }

    @Test public void activeErrorMayOmitProcessedCount() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Session session = new MobileProtocol.Session(
                new MobileProtocol.Capability(capability(5120, 2000)), new MobileProtocol.Listener() {
                    public void onText(String fixed, String pending) { out.add("text:" + fixed + ":" + pending); }
                    public void onTerminal(String message, boolean complete) { out.add("end:" + message); }
                });
        session.receive(event("ready", 1).put("audio_received_samples", 0)
                .put("audio_processed_samples", 0).put("audio_send_limit", 1600));
        session.receive(event("partial", 2).put("audio_processed_samples", 0)
                .put("text", "已收到").put("pending", "尾部"));
        session.receive(event("error", 3).put("code", "MODEL_CHANGED")
                .put("text", "已收到").put("pending", "").put("complete", false));
        assertEquals(List.of("text:已收到:尾部", "text:已收到:",
                "end:电脑端模型已变化，请重新开始听写"), out);
    }

    @Test public void inFlightFrameStillCountsAgainstBuffer() throws Exception {
        List<String> out = new ArrayList<>();
        MobileProtocol.Session session = ready(new MobileProtocol.Capability(capability(2560, 80)), out);
        assertTrue(session.offer(new byte[2560], 0));
        assertNotNull(session.takeSendable());
        assertFalse(session.offer(new byte[2], 0));
        assertTrue(session.isCancelled());
        assertEquals(List.of("end:false"), out);
    }
}
