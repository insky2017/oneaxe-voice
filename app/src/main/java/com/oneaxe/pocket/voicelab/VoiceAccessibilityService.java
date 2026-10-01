package com.oneaxe.pocket.voicelab;

import android.Manifest;
import android.accessibilityservice.AccessibilityService;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.io.File;
import java.util.Arrays;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public final class VoiceAccessibilityService extends AccessibilityService {
    private static volatile boolean anySessionActive;
    static boolean isAnySessionActive() { return anySessionActive; }
    private final Handler ui = new Handler(Looper.getMainLooper());
    private WindowManager windows;
    private WindowManager.LayoutParams bubbleParams;
    private TextView bubble;
    private LinearLayout menu;
    private AccessibilityNodeInfo target;
    private AccessibilityNodeInfo lastFocused;
    private String expectedText;
    private int cursor;
    private int targetWindow;
    private volatile StringBuffer draft = new StringBuffer();
    private volatile boolean recording;
    private volatile boolean sessionActive;
    private volatile boolean cancelled;
    private volatile boolean foregroundStarted;
    private boolean checkingConnection;
    private int connectionCheckId;
    private Thread recorderThread;
    private Thread uploadThread;
    private ArrayBlockingQueue<byte[]> segments;
    private static final byte[] END = new byte[0];

    @Override protected void onServiceConnected() {
        windows = (WindowManager) getSystemService(WINDOW_SERVICE);
        bubble = new TextView(this);
        bubble.setText("麦");
        bubble.setTextColor(Color.WHITE);
        bubble.setTextSize(19);
        bubble.setGravity(Gravity.CENTER);
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(Color.rgb(25, 110, 101));
        shape.setCornerRadius(dp(26));
        bubble.setBackground(shape);
        bubble.setElevation(dp(5));
        bubbleParams = params(48, 48);
        bubbleParams.gravity = Gravity.TOP | Gravity.END;
        bubbleParams.x = dp(12);
        bubbleParams.y = dp(220);
        windows.addView(bubble, bubbleParams);
        bubble.setOnTouchListener(new View.OnTouchListener() {
            float downX, downY;
            int originX, originY;
            boolean dragged;
            @Override public boolean onTouch(View view, MotionEvent event) {
                switch (event.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downX = event.getRawX(); downY = event.getRawY();
                        originX = bubbleParams.x; originY = bubbleParams.y; dragged = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int dx = (int) (event.getRawX() - downX);
                        int dy = (int) (event.getRawY() - downY);
                        if (Math.abs(dx) + Math.abs(dy) > dp(8)) dragged = true;
                        if (dragged) {
                            bubbleParams.x = Math.max(0, originX - dx);
                            bubbleParams.y = Math.max(0, originY + dy);
                            windows.updateViewLayout(bubble, bubbleParams);
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!dragged) toggleMenu();
                        return true;
                    default: return true;
                }
            }
        });
    }

    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event.getEventType() != AccessibilityEvent.TYPE_VIEW_FOCUSED &&
                event.getEventType() != AccessibilityEvent.TYPE_VIEW_TEXT_SELECTION_CHANGED) return;
        AccessibilityNodeInfo source = event.getSource();
        if (source == null) return;
        if (source.isPassword()) lastFocused = null;
        else if (source.isEditable()) lastFocused = source;
    }
    @Override public void onInterrupt() { cancelRecording(); }
    @Override public void onDestroy() {
        cancelRecording();
        hideMenu();
        if (bubble != null) windows.removeView(bubble);
        super.onDestroy();
    }

    private void toggleMenu() {
        if (menu != null) { hideMenu(); return; }
        menu = new LinearLayout(this);
        menu.setOrientation(LinearLayout.VERTICAL);
        menu.setPadding(dp(5), dp(5), dp(5), dp(5));
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(248, 250, 249));
        background.setCornerRadius(dp(7));
        menu.setBackground(background);
        menu.setElevation(dp(7));
        Button action = new Button(this);
        action.setText(checkingConnection ? "正在检查连接" : recording ? "结束听写" : sessionActive ? "正在收尾" : "开始听写");
        action.setEnabled(!checkingConnection && (recording || !sessionActive));
        action.setOnClickListener(view -> {
            hideMenu();
            if (recording) stopRecording(); else startRecording();
        });
        menu.addView(action);
        if (sessionActive || checkingConnection) {
            Button cancel = new Button(this);
            cancel.setText("取消听写");
            cancel.setOnClickListener(view -> { hideMenu(); cancelRecording(); });
            menu.addView(cancel);
        }
        if (!sessionActive && !checkingConnection) {
            Button probe = new Button(this);
            probe.setText("写入测试文字");
            probe.setOnClickListener(view -> {
                hideMenu();
                if (prepareTarget()) {
                    boolean inserted = insert("Voice Lab 测试");
                    message(inserted ? "测试文字已写入" : "该输入框暂不支持辅助功能写入");
                }
            });
            menu.addView(probe);
            if (new File(getFilesDir(), "fixture.wav").isFile()) {
                Button fixture = new Button(this);
                fixture.setText("运行测试音频");
                fixture.setOnClickListener(view -> { hideMenu(); startFixture(); });
                menu.addView(fixture);
            }
        }
        if (draft.length() > 0) {
            Button copy = new Button(this);
            copy.setText("复制本轮草稿");
            copy.setOnClickListener(view -> {
                ((ClipboardManager) getSystemService(CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("Voice Lab", draft.toString()));
                hideMenu();
                message("草稿已复制");
            });
            menu.addView(copy);
        }
        WindowManager.LayoutParams menuParams = params(160, WindowManager.LayoutParams.WRAP_CONTENT);
        menuParams.gravity = Gravity.TOP | Gravity.END;
        menuParams.x = bubbleParams.x + dp(52);
        menuParams.y = bubbleParams.y;
        windows.addView(menu, menuParams);
    }

    private void hideMenu() {
        if (menu != null) { windows.removeView(menu); menu = null; }
    }

    private void startRecording() {
        checkBeforeDictation(false);
    }

    private void checkBeforeDictation(boolean fixture) {
        if (sessionActive || checkingConnection) return;
        checkingConnection = true;
        anySessionActive = true;
        int request = ++connectionCheckId;
        if (bubble != null) bubble.setText("连");
        new Thread(() -> {
            String failure = null;
            try { VoiceClient.requireDictationReady(this); }
            catch (Exception e) {
                failure = e.getMessage() == null ? "连接检查失败，请检查 Tailscale 和电脑服务" : e.getMessage();
            }
            String result = failure;
            ui.post(() -> {
                if (request != connectionCheckId) return;
                checkingConnection = false;
                anySessionActive = sessionActive;
                if (bubble != null) bubble.setText("麦");
                if (result != null) { message(result); return; }
                if (fixture) startFixtureWithReadyConnection();
                else startRecordingWithReadyConnection();
            });
        }, "voice-connection-check").start();
    }

    private void startRecordingWithReadyConnection() {
        if (sessionActive) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            message("请先在 Voice Lab 授予麦克风权限"); return;
        }
        if (!prepareTarget()) return;
        draft = new StringBuffer();
        segments = new ArrayBlockingQueue<>(8);
        try {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            manager.createNotificationChannel(new NotificationChannel("voice_capture", "Voice Lab 录音", NotificationManager.IMPORTANCE_LOW));
            Notification notification = new Notification.Builder(this, "voice_capture")
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setContentTitle("OneAxe Voice Lab 正在听写")
                    .setOngoing(true).build();
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                startForeground(42, notification, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            } else {
                startForeground(42, notification);
            }
            foregroundStarted = true;
        } catch (Exception e) {
            message("无法启动麦克风前台服务"); return;
        }
        recording = true;
        cancelled = false;
        sessionActive = true;
        anySessionActive = true;
        bubble.setText("停");
        ArrayBlockingQueue<byte[]> queue = segments;
        uploadThread = new Thread(() -> uploadLoop(queue), "voice-upload");
        recorderThread = new Thread(() -> captureLoop(queue), "voice-capture");
        uploadThread.start();
        recorderThread.start();
    }

    private void stopRecording() {
        recording = false;
        if (bubble != null) bubble.setText("麦");
    }

    private void startFixture() {
        checkBeforeDictation(true);
    }

    private void startFixtureWithReadyConnection() {
        if (sessionActive || !prepareTarget()) return;
        File fixture = new File(getFilesDir(), "fixture.wav");
        draft = new StringBuffer();
        cancelled = false;
        sessionActive = true;
        anySessionActive = true;
        segments = new ArrayBlockingQueue<>(8);
        ArrayBlockingQueue<byte[]> queue = segments;
        bubble.setText("测");
        uploadThread = new Thread(() -> uploadLoop(queue), "voice-upload");
        recorderThread = new Thread(() -> fixtureLoop(queue, fixture), "voice-fixture");
        uploadThread.start();
        recorderThread.start();
    }

    private void cancelRecording() {
        ++connectionCheckId;
        checkingConnection = false;
        if (!sessionActive) anySessionActive = false;
        cancelled = true;
        recording = false;
        if (segments != null) segments.clear();
        if (bubble != null) bubble.setText("麦");
    }

    private boolean prepareTarget() {
        if (!sessionActive) cancelled = false;
        target = null;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        AccessibilityNodeInfo focused = root == null ? null : root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (focused == null || !focused.isEditable()) focused = lastFocused;
        Log.i("VoiceLabTarget", "root=" + (root == null ? -1 : root.getWindowId()) +
                " focused=" + (focused != null) + " editable=" + (focused != null && focused.isEditable()) +
                " selection=" + (focused == null ? -2 : focused.getTextSelectionStart()));
        if (focused == null || root == null || root.getWindowId() != focused.getWindowId() ||
                !focused.isEditable() || focused.isPassword() ||
                focused.getTextSelectionStart() < 0 && !focused.isShowingHintText()) {
            message("请先选中标准可编辑输入框"); return false;
        }
        target = focused;
        targetWindow = focused.getWindowId();
        expectedText = focused.isShowingHintText() || focused.getText() == null ? "" : focused.getText().toString();
        cursor = Math.max(0, focused.getTextSelectionStart());
        return true;
    }

    private void captureLoop(ArrayBlockingQueue<byte[]> queue) {
        AudioRecord audio = null;
        PcmSegmenter segmenter = new PcmSegmenter(queue);
        try {
            int buffer = Math.max(AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT), 6400);
            audio = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, buffer);
            if (audio.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("麦克风不可用");
            audio.startRecording();
            Log.i("VoiceLabAudio", "source=mic audio_source=VOICE_RECOGNITION session=" +
                    audio.getAudioSessionId() + " routed_type=" +
                    (audio.getRoutedDevice() == null ? -1 : audio.getRoutedDevice().getType()));
            byte[] frame = new byte[640];
            int filled = 0;
            while (recording) {
                int size = audio.read(frame, filled, frame.length - filled, AudioRecord.READ_BLOCKING);
                if (size < 0) throw new IllegalStateException("录音设备读取失败");
                if (size == 0) continue;
                filled += size;
                if (filled != frame.length) continue;
                segmenter.feed(Arrays.copyOf(frame, frame.length));
                filled = 0;
            }
            if (!cancelled) {
                if (filled > 0) {
                    Arrays.fill(frame, filled, frame.length, (byte) 0);
                    segmenter.feed(Arrays.copyOf(frame, frame.length));
                }
                segmenter.finish();
            }
        } catch (Exception e) {
            ui.post(() -> message("录音失败：" + e.getClass().getSimpleName()));
        } finally {
            if (audio != null) {
                try { audio.stop(); } catch (IllegalStateException ignored) {}
                audio.release();
            }
            recording = false;
            finishQueue(queue);
            ui.post(() -> { if (bubble != null) bubble.setText("麦"); });
        }
    }

    private void fixtureLoop(ArrayBlockingQueue<byte[]> queue, File fixture) {
        try {
            byte[] pcm = WavPcm16.readMono16k(fixture);
            Log.i("VoiceLabAudio", "source=fixture duration_ms=" + pcm.length / 32);
            PcmSegmenter segmenter = new PcmSegmenter(queue);
            for (int offset = 0; offset < pcm.length && !cancelled; offset += 640) {
                byte[] frame = new byte[640];
                System.arraycopy(pcm, offset, frame, 0, Math.min(640, pcm.length - offset));
                segmenter.feed(frame);
            }
            if (!cancelled) segmenter.finish();
        } catch (Exception e) {
            ui.post(() -> message("测试音频失败：" + e.getClass().getSimpleName()));
        } finally {
            finishQueue(queue);
        }
    }

    private void finishQueue(ArrayBlockingQueue<byte[]> queue) {
        try {
            if (!queue.offer(END, 5, TimeUnit.SECONDS)) {
                queue.clear();
                queue.offer(END);
                ui.post(() -> message("识别队列未能排空，本轮请检查草稿"));
            }
        } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
    }

    private void uploadLoop(ArrayBlockingQueue<byte[]> queue) {
        try {
            while (true) {
                byte[] pcm = queue.take();
                if (pcm == END) break;
                if (cancelled) continue;
                String raw = VoiceClient.transcribe(this, pcm);
                if (cancelled) continue;
                String text = cleanText(raw);
                if (text.isEmpty()) continue;
                if (draft.length() > 0 && draft.charAt(draft.length() - 1) < 128 &&
                        !Character.isWhitespace(draft.charAt(draft.length() - 1)) && isAsciiWord(text.charAt(0))) {
                    text = " " + text;
                }
                draft.append(text);
                String addition = text;
                CountDownLatch committed = new CountDownLatch(1);
                ui.post(() -> {
                    if (target != null && !insert(addition)) {
                        target = null;
                        message("目标已变化；本轮文字保存在悬浮菜单草稿中");
                    }
                    committed.countDown();
                });
                if (!committed.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("输入操作超时");
            }
        } catch (Exception e) {
            recording = false;
            queue.clear();
            ui.post(() -> message("听写失败：" +
                    (e.getMessage() != null && e.getMessage().startsWith("Voice HTTP ")
                            ? e.getMessage() : e.getClass().getSimpleName())));
        } finally {
            try { recorderThread.join(6000); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
            sessionActive = false;
            anySessionActive = false;
            ui.post(() -> {
                if (foregroundStarted) stopForeground(STOP_FOREGROUND_REMOVE);
                foregroundStarted = false;
                if (bubble != null) bubble.setText("麦");
            });
        }
    }

    private boolean insert(String addition) {
        AccessibilityNodeInfo activeRoot = getRootInActiveWindow();
        if (cancelled || target == null || activeRoot == null || activeRoot.getWindowId() != targetWindow ||
                !target.refresh() || !target.isFocused() || target.isPassword() ||
                target.getWindowId() != targetWindow || !target.isEditable()) {
            Log.i("VoiceLabTarget", "insert rejected: inactive target");
            return false;
        }
        String actual = target.isShowingHintText() || target.getText() == null ? "" : target.getText().toString();
        int start = target.getTextSelectionStart();
        int end = target.getTextSelectionEnd();
        if (!actual.equals(expectedText) || !(start == cursor && end == cursor ||
                actual.isEmpty() && cursor == 0 && start < 0 && end < 0)) {
            Log.i("VoiceLabTarget", "insert rejected: content or cursor changed");
            return false;
        }
        // SetText replaces the node value, so rebuild it at the verified cursor.
        String updated = actual.substring(0, cursor) + addition + actual.substring(cursor);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, updated);
        if (!target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return false;
        int newCursor = cursor + addition.length();
        Bundle selection = new Bundle();
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, newCursor);
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, newCursor);
        target.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selection);
        expectedText = updated;
        cursor = newCursor;
        return true;
    }

    private static String cleanText(String value) {
        StringBuilder result = new StringBuilder();
        boolean space = false;
        for (int offset = 0; offset < value.length();) {
            int codePoint = value.codePointAt(offset);
            offset += Character.charCount(codePoint);
            if (Character.isWhitespace(codePoint)) { space = result.length() > 0; continue; }
            if (Character.isISOControl(codePoint) || codePoint >= 0xD800 && codePoint <= 0xDFFF) continue;
            if (space) result.append(' ');
            result.appendCodePoint(codePoint);
            space = false;
        }
        return result.toString();
    }

    private static boolean isAsciiWord(char c) {
        return c >= '0' && c <= '9' || c >= 'A' && c <= 'Z' || c >= 'a' && c <= 'z';
    }

    private WindowManager.LayoutParams params(int widthDp, int heightDp) {
        WindowManager.LayoutParams value = new WindowManager.LayoutParams(
                widthDp == WindowManager.LayoutParams.WRAP_CONTENT ? widthDp : dp(widthDp),
                heightDp == WindowManager.LayoutParams.WRAP_CONTENT ? heightDp : dp(heightDp),
                WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        return value;
    }

    private int dp(int value) { return (int) (value * getResources().getDisplayMetrics().density + 0.5f); }
    private void message(String value) { Toast.makeText(this, value, Toast.LENGTH_LONG).show(); }
}
