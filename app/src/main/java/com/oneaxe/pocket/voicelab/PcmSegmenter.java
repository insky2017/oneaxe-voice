package com.oneaxe.pocket.voicelab;

import android.util.Log;
import java.io.ByteArrayOutputStream;
import java.util.ArrayDeque;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;

final class PcmSegmenter {
    private static final int SPEECH_RMS = 50;
    private final ArrayBlockingQueue<byte[]> queue;
    private final ArrayDeque<byte[]> pre = new ArrayDeque<>();
    private final ByteArrayOutputStream current = new ByteArrayOutputStream();
    private boolean active;
    private int recentSpeech;
    private int quiet;
    private int voiced;
    private int frames;
    private int overThreshold;
    private int maxRms;
    private int segmentCount;
    private long rmsSum;

    PcmSegmenter(ArrayBlockingQueue<byte[]> queue) { this.queue = queue; }

    void feed(byte[] frame) throws InterruptedException {
        if (frame.length != 640) throw new IllegalArgumentException("expected 20 ms PCM frame");
        int level = rms(frame);
        frames++;
        rmsSum += level;
        maxRms = Math.max(maxRms, level);
        boolean speech = level > SPEECH_RMS;
        if (speech) overThreshold++;
        if (!active) {
            pre.addLast(frame);
            if (pre.size() > 12) pre.removeFirst();
            recentSpeech = speech ? recentSpeech + 1 : 0;
            if (recentSpeech < 2) return;
            active = true;
            for (byte[] old : pre) current.write(old, 0, old.length);
            pre.clear();
            voiced = recentSpeech;
            quiet = 0;
            return;
        }
        current.write(frame, 0, frame.length);
        if (speech) { voiced++; quiet = 0; } else quiet++;
        if (quiet >= 35 || current.size() >= 480000) emit(quiet >= 35 ? "pause" : "limit");
    }

    void finish() throws InterruptedException {
        if (active) emit("stop");
        Log.i("VoiceLabAudio", "captured_ms=" + frames * 20 + " mean_rms=" +
                (frames == 0 ? 0 : rmsSum / frames) + " max_rms=" + maxRms +
                " frames_above_50=" + overThreshold + " segments=" + segmentCount);
    }

    private void emit(String reason) throws InterruptedException {
        byte[] pcm = current.toByteArray();
        if (voiced >= ("pause".equals(reason) ? 6 : 1) && pcm.length > 0) {
            if (pcm.length < 3200) pcm = java.util.Arrays.copyOf(pcm, 3200);
            if (!queue.offer(pcm)) {
                Log.i("VoiceLabAudio", "queue_backlog=true");
                if (!queue.offer(pcm, 5, TimeUnit.SECONDS)) throw new IllegalStateException("recognition backlog");
            }
            segmentCount++;
            Log.i("VoiceLabAudio", "segment=" + segmentCount + " reason=" + reason +
                    " duration_ms=" + pcm.length / 32 + " voiced_frames=" + voiced);
        }
        current.reset();
        pre.clear();
        active = false;
        recentSpeech = 0;
        quiet = 0;
        voiced = 0;
    }

    private static int rms(byte[] pcm) {
        long sum = 0;
        for (int i = 0; i < pcm.length; i += 2) {
            short sample = (short) ((pcm[i] & 255) | (pcm[i + 1] << 8));
            sum += (long) sample * sample;
        }
        return (int) Math.sqrt(sum / (pcm.length / 2));
    }
}
