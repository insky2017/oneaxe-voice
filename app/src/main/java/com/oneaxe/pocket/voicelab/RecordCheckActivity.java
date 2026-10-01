package com.oneaxe.pocket.voicelab;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MediaPlayer;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.util.concurrent.atomic.AtomicBoolean;

/** A manual microphone check; audio stays in this app's cache and is never uploaded. */
public final class RecordCheckActivity extends Activity {
    private static final int SAMPLE_RATE = 16000;
    private static final int MAX_SECONDS = 60;
    private static final int MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean capturing = new AtomicBoolean();
    private volatile AudioRecord recorder;
    private volatile boolean workerRunning;
    private volatile double dbfs = -90;
    private volatile int generation;
    private long startedAt;
    private File recordingFile;
    private MediaPlayer player;
    private MediaPlayer fixturePlayer;
    private Runnable fixtureStopTask;
    private Button recordButton;
    private Button fixtureButton;
    private Button stopButton;
    private Button playButton;
    private Button deleteButton;
    private TextView status;
    private TextView details;
    private ProgressBar meter;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setTitle("录音检查");
        LinearLayout content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(18);
        content.setPadding(pad, pad, pad, pad);

        TextView note = new TextView(this);
        note.setText("手动录制并回放，最长 60 秒。录音仅保存在本应用缓存，离开页面即删除，不会上传。");
        note.setTextColor(Color.DKGRAY);
        content.addView(note);

        status = new TextView(this);
        status.setTextSize(18);
        status.setPadding(0, dp(20), 0, dp(6));
        content.addView(status);
        details = new TextView(this);
        details.setPadding(0, 0, 0, dp(10));
        content.addView(details);
        meter = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        meter.setMax(100);
        content.addView(meter, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(10)));

        recordButton = button(content, "开始录音", () -> startCapture());
        fixtureButton = button(content, "播放样本并录制", () -> startFixtureCapture());
        stopButton = button(content, "停止录音", () -> stopCapture());
        playButton = button(content, "回放", () -> startPlayback());
        deleteButton = button(content, "删除录音", () -> deleteRecording());

        ScrollView scroll = new ScrollView(this);
        scroll.addView(content);
        setContentView(scroll);
        showIdle("尚未录音");
    }

    private Button button(LinearLayout parent, String label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setOnClickListener(view -> action.run());
        parent.addView(button, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        return button;
    }

    private void startCapture() {
        if (capturing.get() || workerRunning) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            showIdle("请先在 Voice Lab 授予麦克风权限");
            return;
        }
        AudioManager audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        if (audioManager != null && !audioManager.getActiveRecordingConfigurations().isEmpty()) {
            showIdle("当前有录音进行中，请先结束听写或其他录音");
            return;
        }
        stopPlayback();
        deleteFile();
        int minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) {
            showIdle("无法获取录音缓冲区");
            return;
        }
        AudioRecord audio = null;
        try {
            audio = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(minimum, 6400));
            if (audio.getState() != AudioRecord.STATE_INITIALIZED) {
                audio.release();
                showIdle("麦克风不可用");
                return;
            }
            audio.startRecording();
        } catch (Exception error) {
            if (audio != null) audio.release();
            showIdle("录音启动失败：" + error.getClass().getSimpleName());
            return;
        }
        int session = ++generation;
        recorder = audio;
        dbfs = -90;
        startedAt = SystemClock.elapsedRealtime();
        capturing.set(true);
        workerRunning = true;
        recordButton.setEnabled(false);
        fixtureButton.setEnabled(false);
        stopButton.setEnabled(true);
        playButton.setEnabled(false);
        deleteButton.setEnabled(false);
        status.setText("正在录音");
        AudioRecord activeAudio = audio;
        new Thread(() -> capture(activeAudio, session), "voice-record-check").start();
        updateLiveStatus(session);
    }

    private void startFixtureCapture() {
        if (capturing.get() || workerRunning) return;
        File fixture = new File(getFilesDir(), "fixture.wav");
        if (!fixture.isFile() || fixture.length() == 0) {
            showIdle("未找到应用私有文件 fixture.wav");
            return;
        }
        AudioManager audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        if (audioManager == null || audioManager.getStreamVolume(AudioManager.STREAM_MUSIC) == 0) {
            showIdle("媒体音量为 0，请先手动调高再测试");
            return;
        }
        startCapture();
        if (!capturing.get()) return;
        int session = generation;
        MediaPlayer playback = new MediaPlayer();
        fixturePlayer = playback;
        try {
            boolean speakerSelectionFailed = false;
            playback.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                for (AudioDeviceInfo device : audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                    if (device.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {
                        speakerSelectionFailed = !playback.setPreferredDevice(device);
                        break;
                    }
                }
            }
            boolean routePreferenceFailed = speakerSelectionFailed;
            playback.setDataSource(fixture.getAbsolutePath());
            playback.setOnPreparedListener(ready -> {
                if (fixturePlayer == ready && session == generation && capturing.get()) {
                    ready.start();
                    AudioDeviceInfo route = ready.getRoutedDevice();
                    status.setText(!routePreferenceFailed && route != null
                            && route.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
                            ? "正在播放样本并录音"
                            : "正在播放样本并录音；输出未确认是扬声器，请检查音频路由");
                }
            });
            playback.setOnCompletionListener(done -> {
                if (fixturePlayer != done || session != generation) return;
                done.release();
                fixturePlayer = null;
                fixtureStopTask = () -> {
                    fixtureStopTask = null;
                    if (session == generation && capturing.get()) stopCapture();
                };
                ui.postDelayed(fixtureStopTask, 300);
            });
            playback.setOnErrorListener((failed, what, extra) -> {
                if (fixturePlayer == failed) {
                    stopFixturePlayback();
                    stopCapture();
                    status.setText("样本播放失败，正在保存已录音频");
                }
                return true;
            });
            playback.prepareAsync();
        } catch (Exception error) {
            stopFixturePlayback();
            stopCapture();
            status.setText("样本播放失败，正在保存已录音频");
        }
    }

    private void stopFixturePlayback() {
        if (fixtureStopTask != null) {
            ui.removeCallbacks(fixtureStopTask);
            fixtureStopTask = null;
        }
        if (fixturePlayer != null) {
            fixturePlayer.release();
            fixturePlayer = null;
        }
    }

    private void capture(AudioRecord audio, int session) {
        ByteArrayOutputStream pcm = new ByteArrayOutputStream(MAX_PCM_BYTES);
        String failure = null;
        byte[] frame = new byte[640];
        try {
            while (capturing.get() && session == generation && pcm.size() < MAX_PCM_BYTES) {
                int count = audio.read(frame, 0, Math.min(frame.length, MAX_PCM_BYTES - pcm.size()),
                        AudioRecord.READ_BLOCKING);
                if (count < 0) throw new IOException("AudioRecord.read: " + count);
                if (count == 0) continue;
                pcm.write(frame, 0, count);
                dbfs = volumeDbfs(frame, count);
                if (SystemClock.elapsedRealtime() - startedAt >= MAX_SECONDS * 1000L) break;
            }
        } catch (Exception error) {
            failure = error.getClass().getSimpleName();
        } finally {
            try { audio.stop(); } catch (IllegalStateException ignored) {}
            audio.release();
            if (recorder == audio) recorder = null;
            capturing.set(false);
        }
        File saved = null;
        if (session == generation && pcm.size() > 0 && failure == null) {
            try {
                saved = File.createTempFile("record-check-", ".wav", getCacheDir());
                writeWav(saved, pcm.toByteArray());
            } catch (IOException error) {
                if (saved != null) saved.delete();
                saved = null;
                failure = "保存失败";
            }
        }
        File result = saved;
        String problem = failure;
        workerRunning = false;
        ui.post(() -> {
            if (session != generation) {
                if (result != null) result.delete();
                if (!workerRunning && !capturing.get()) showIdle("录音已停止，临时文件已清理");
                return;
            }
            stopFixturePlayback();
            recordingFile = result;
            showIdle(problem != null ? "录音失败：" + problem :
                    result != null ? "录音已保存，可回放" : "没有录到声音");
        });
    }

    private void stopCapture() {
        if (!capturing.get()) return;
        stopFixturePlayback();
        capturing.set(false);
        stopButton.setEnabled(false);
        status.setText("正在保存录音");
    }

    private void updateLiveStatus(int session) {
        if (session != generation || !capturing.get()) return;
        long elapsed = Math.min(MAX_SECONDS * 1000L, SystemClock.elapsedRealtime() - startedAt);
        String route = "未知";
        AudioRecord audio = recorder;
        if (audio != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                AudioDeviceInfo device = audio.getRoutedDevice();
                if (device != null) route = device.getProductName().toString();
            } catch (IllegalStateException ignored) {}
        }
        String silenced = "不可获取";
        if (audio != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                AudioRecordingConfiguration config = audio.getActiveRecordingConfiguration();
                if (config != null) silenced = config.isClientSilenced() ? "是" : "否";
            }
            catch (IllegalStateException ignored) {}
        }
        details.setText(String.format(java.util.Locale.ROOT,
                "时长 %.1f / 60 秒 · 电平 %.1f dBFS\n系统静音：%s · 输入设备：%s",
                elapsed / 1000.0, dbfs, silenced, route));
        meter.setProgress((int) Math.max(0, Math.min(100, (dbfs + 60) * 100 / 60)));
        ui.postDelayed(() -> updateLiveStatus(session), 200);
    }

    private static double volumeDbfs(byte[] pcm, int count) {
        double sum = 0;
        for (int i = 0; i + 1 < count; i += 2) {
            short sample = (short) ((pcm[i] & 255) | (pcm[i + 1] << 8));
            sum += (double) sample * sample;
        }
        int samples = count / 2;
        if (samples == 0 || sum == 0) return -90;
        return Math.max(-90, 20 * Math.log10(Math.sqrt(sum / samples) / 32768));
    }

    private void startPlayback() {
        if (recordingFile == null || !recordingFile.isFile()) return;
        stopPlayback();
        MediaPlayer playback = new MediaPlayer();
        player = playback;
        try {
            playback.setDataSource(recordingFile.getAbsolutePath());
            playback.setOnPreparedListener(ready -> {
                if (player == ready) {
                    ready.start();
                    status.setText("正在回放");
                    playButton.setText("停止回放");
                }
            });
            playback.setOnCompletionListener(done -> stopPlayback());
            playback.setOnErrorListener((failed, what, extra) -> {
                stopPlayback();
                showIdle("回放失败");
                return true;
            });
            playback.prepareAsync();
            playButton.setOnClickListener(view -> stopPlayback());
        } catch (Exception error) {
            stopPlayback();
            showIdle("回放失败：" + error.getClass().getSimpleName());
        }
    }

    private void stopPlayback() {
        if (player != null) {
            player.release();
            player = null;
        }
        if (playButton != null) {
            playButton.setText("回放");
            playButton.setOnClickListener(view -> startPlayback());
        }
        if (!capturing.get() && recordingFile != null) status.setText("录音已保存，可回放");
    }

    private void deleteRecording() {
        stopPlayback();
        deleteFile();
        showIdle("录音已删除");
    }

    private void deleteFile() {
        if (recordingFile != null) recordingFile.delete();
        recordingFile = null;
        new File(getCacheDir(), "record-check.wav").delete();
    }

    private void showIdle(String message) {
        status.setText(message);
        details.setText("音源：VOICE_RECOGNITION · 16 kHz · 单声道 PCM16");
        meter.setProgress(0);
        recordButton.setEnabled(true);
        fixtureButton.setEnabled(true);
        stopButton.setEnabled(false);
        playButton.setEnabled(recordingFile != null);
        deleteButton.setEnabled(recordingFile != null);
    }

    private static void writeWav(File file, byte[] pcm) throws IOException {
        try (OutputStream output = new FileOutputStream(file)) {
            output.write("RIFF".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
            little32(output, pcm.length + 36);
            output.write("WAVEfmt ".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
            little32(output, 16);
            little16(output, 1);
            little16(output, 1);
            little32(output, SAMPLE_RATE);
            little32(output, SAMPLE_RATE * 2);
            little16(output, 2);
            little16(output, 16);
            output.write("data".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
            little32(output, pcm.length);
            output.write(pcm);
        }
    }

    private static void little16(OutputStream output, int value) throws IOException {
        output.write(value & 255);
        output.write((value >>> 8) & 255);
    }

    private static void little32(OutputStream output, int value) throws IOException {
        little16(output, value);
        little16(output, value >>> 16);
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    @Override protected void onStop() {
        generation++;
        capturing.set(false);
        stopFixturePlayback();
        stopPlayback();
        deleteFile();
        showIdle("录音已停止，临时文件已清理");
        recordButton.setEnabled(!workerRunning);
        super.onStop();
    }
}
