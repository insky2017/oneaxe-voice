package com.oneaxe.pocket.voicelab;

import static org.junit.Assert.*;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import okhttp3.Request;
import okhttp3.WebSocket;
import okio.ByteString;
import org.json.JSONObject;
import org.junit.Test;

public class VoiceTransportTest {
    private static final class FakeWire implements VoiceClient.Wire {
        final List<String> messages = new ArrayList<>();
        MobileProtocol.Session session;
        volatile long queued;
        volatile int audioCount;
        volatile int cancelCount;
        volatile boolean closed;

        public synchronized boolean sendText(String value) {
            messages.add(value);
            try {
                JSONObject control = new JSONObject(value);
                if ("finish".equals(control.optString("type"))) {
                    session.receive(MobileProtocolTest.event("final", 3)
                            .put("audio_processed_samples", session.sentSamples())
                            .put("text", "完成").put("pending", "")
                            .put("reason", "finished").put("complete", true));
                }
            } catch (Exception e) { throw new AssertionError(e); }
            return true;
        }

        public synchronized boolean sendAudio(byte[] value) {
            audioCount++;
            queued += value.length;
            notifyAll();
            return true;
        }

        public long queueSize() { return queued; }
        public void close(int code, String reason) { closed = true; }
        public void cancel() { cancelCount++; }

        synchronized void awaitAudio(int count) throws InterruptedException {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
            while (audioCount < count && System.nanoTime() < deadline) wait(10);
            assertEquals(count, audioCount);
        }
    }

    private static final class Output implements MobileProtocol.Listener {
        final List<String> text = new ArrayList<>();
        final List<Boolean> terminal = new ArrayList<>();
        public void onText(String fixed, String pending) { text.add(fixed + "|" + pending); }
        public void onTerminal(String message, boolean complete) { terminal.add(complete); }
    }

    private static MobileProtocol.Session ready(Output output) throws Exception {
        MobileProtocol.Session session = new MobileProtocol.Session(
                new MobileProtocol.Capability(MobileProtocolTest.capability(5120, 2000)), output);
        assertEquals("start", session.start().getString("type"));
        session.receive(MobileProtocolTest.event("ready", 1).put("audio_received_samples", 0)
                .put("audio_processed_samples", 0).put("audio_send_limit", 1600));
        return session;
    }

    @Test public void fakeTransportSendsAudioWhileTextArrivesThenFinishesWithActualCount() throws Exception {
        Output output = new Output();
        MobileProtocol.Session session = ready(output);
        FakeWire wire = new FakeWire();
        wire.session = session;
        VoiceClient.Stream stream = new VoiceClient.Stream(session, wire);
        stream.startSender();
        assertTrue(stream.offerAudio(new byte[3200]));
        wire.awaitAudio(1);
        session.receive(MobileProtocolTest.event("partial", 2).put("audio_processed_samples", 1600)
                .put("text", "完成").put("pending", "中"));
        assertEquals(List.of("完成|中"), output.text);
        stream.finish();
        assertEquals(1, wire.audioCount);
        assertEquals("finish", new JSONObject(wire.messages.get(0)).getString("type"));
        assertEquals(1600, new JSONObject(wire.messages.get(0)).getLong("after_audio_samples"));
        assertEquals(List.of("完成|中", "完成|"), output.text);
        assertEquals(List.of(true), output.terminal);
        stream.cancel();
        assertEquals(1, wire.messages.size());
    }

    @Test public void fakeTransportCancelStopsQueuedAudioAndLateText() throws Exception {
        Output output = new Output();
        MobileProtocol.Session session = ready(output);
        FakeWire wire = new FakeWire();
        wire.session = session;
        VoiceClient.Stream stream = new VoiceClient.Stream(session, wire);
        stream.startSender();
        assertTrue(stream.offerAudio(new byte[3200]));
        wire.awaitAudio(1);
        assertTrue(stream.offerAudio(new byte[3200]));
        stream.cancel();
        session.receive(MobileProtocolTest.event("partial", 2).put("audio_processed_samples", 1600)
                .put("text", "迟到").put("pending", ""));
        assertEquals(1, wire.audioCount);
        assertEquals(List.of(false), output.terminal);
        assertTrue(output.text.isEmpty());
        assertTrue(wire.closed);
        assertEquals("cancel", new JSONObject(wire.messages.get(0)).getString("type"));
    }

    @Test public void socketQueueCountsTowardTwoSecondBuffer() throws Exception {
        Output output = new Output();
        MobileProtocol.Session session = ready(output);
        FakeWire wire = new FakeWire();
        wire.session = session;
        wire.queued = session.bufferBytes() - 100;
        VoiceClient.Stream stream = new VoiceClient.Stream(session, wire);
        assertFalse(stream.offerAudio(new byte[3200]));
        assertTrue(wire.closed);
        assertEquals(List.of(false), output.terminal);
    }

    @Test public void openingCancellationClosesHandshakeSocket() throws Exception {
        VoiceClient.Opening opening = new VoiceClient.Opening();
        FakeSocket socket = new FakeSocket();
        CountDownLatch latch = new CountDownLatch(1);
        opening.bind(socket, latch);
        opening.cancel();
        assertEquals(0, latch.getCount());
        assertEquals(1, socket.cancelCount);
        opening.cancel();
        assertEquals(1, socket.cancelCount);
    }

    private static final class FakeSocket implements WebSocket {
        int cancelCount;
        public Request request() { return new Request.Builder().url("https://example.ts.net/").build(); }
        public long queueSize() { return 0; }
        public boolean send(String text) { return true; }
        public boolean send(ByteString bytes) { return true; }
        public boolean close(int code, String reason) { return true; }
        public void cancel() { cancelCount++; }
    }
}
