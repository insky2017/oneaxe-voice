package com.oneaxe.pocket.voicelab;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import java.io.File;
import java.io.IOException;
import java.util.function.Consumer;

final class FixturePlayback {
    private final Context context;
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile boolean cancelled;
    private MediaPlayer player;
    private boolean ended;

    FixturePlayback(Context context) {
        this.context = context.getApplicationContext();
    }

    void start(File fixture, Runnable onComplete, Consumer<String> onError) {
        main.post(() -> begin(fixture, onComplete, onError));
    }

    void cancel() {
        cancelled = true;
        main.post(this::release);
    }

    private void begin(File fixture, Runnable onComplete, Consumer<String> onError) {
        if (cancelled) return;
        if (ended || player != null) {
            fail("测试音频已经开始播放", onError);
            return;
        }
        try {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) {
                fail("Android 9 以下无法指定内置扬声器，此录音回放测试不可用", onError);
                return;
            }
            if (fixture == null || !fixture.isFile() || fixture.length() == 0
                    || !fixture.getCanonicalFile().getParentFile().equals(context.getFilesDir().getCanonicalFile())) {
                fail("应用私有目录中没有可播放的测试音频", onError);
                return;
            }
            AudioManager audio = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
            if (audio == null || audio.getStreamVolume(AudioManager.STREAM_MUSIC) == 0
                    || audio.isStreamMute(AudioManager.STREAM_MUSIC)) {
                fail("媒体音量为零或已静音，请调高媒体音量", onError);
                return;
            }
            AudioDeviceInfo speaker = null;
            for (AudioDeviceInfo device : audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
                if (device.getType() == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER) {
                    speaker = device;
                    break;
                }
            }
            if (speaker == null) {
                fail("找不到内置扬声器，无法运行麦克风回放测试", onError);
                return;
            }
            MediaPlayer media = new MediaPlayer();
            player = media;
            AudioDeviceInfo output = speaker;
            media.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
            media.setDataSource(fixture.getAbsolutePath());
            media.setOnPreparedListener(prepared -> {
                if (cancelled || ended) return;
                try {
                    if (!prepared.setPreferredDevice(output)) {
                        fail("无法将测试音频路由到内置扬声器", onError);
                        return;
                    }
                    prepared.start();
                    AudioDeviceInfo routed = prepared.getRoutedDevice();
                    if (routed != null && routed.getId() != output.getId()) {
                        fail("测试音频未从内置扬声器播放", onError);
                    }
                } catch (RuntimeException e) {
                    fail("测试音频播放失败", onError);
                }
            });
            media.setOnCompletionListener(completed -> {
                if (cancelled || ended) return;
                ended = true;
                release();
                onComplete.run();
            });
            media.setOnErrorListener((failed, what, extra) -> {
                fail("测试音频播放失败", onError);
                return true;
            });
            media.prepareAsync();
        } catch (IOException | RuntimeException e) {
            fail("无法准备测试音频", onError);
        }
    }

    private void fail(String message, Consumer<String> onError) {
        if (cancelled || ended) return;
        ended = true;
        release();
        onError.accept(message);
    }

    private void release() {
        MediaPlayer media = player;
        player = null;
        if (media != null) media.release();
    }
}
