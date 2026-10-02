package com.oneaxe.pocket.voicelab;

import android.Manifest;
import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.InputMethod;
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
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.InputType;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.SurroundingText;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;
import java.io.File;
import java.util.Arrays;
import java.util.concurrent.locks.LockSupport;

public final class VoiceAccessibilityService extends AccessibilityService {
    private static volatile boolean anySessionActive;
    static boolean isAnySessionActive() { return anySessionActive; }
    private final Handler ui = new Handler(Looper.getMainLooper());
    private WindowManager windows;
    private WindowManager.LayoutParams bubbleParams;
    private TextView bubble;
    private LinearLayout menu;
    private AccessibilityNodeInfo target;
    private VoiceInputMethod voiceInputMethod;
    private EditorTarget editorTarget;
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
    private volatile DictationRun activeRun;

    private static final class DictationRun {
        final DictationDraft transcript = new DictationDraft();
        final VoiceClient.Opening opening = new VoiceClient.Opening();
        volatile boolean capturing;
        volatile boolean cancelled;
        volatile AudioRecord audio;
        volatile Thread thread;
        volatile VoiceClient.Stream stream;
        int fixtureSeconds;
        boolean microphoneFixture;
        FixturePlayback playback;
        InputWriteAck pendingWrite;
        EditorWriteAck pendingEditorWrite;
        long pendingSinceMs;
        boolean pollScheduled;
        String finalStatus;
        long finalDeadlineMs;
    }

    private final class VoiceInputMethod extends InputMethod {
        int generation;
        int selectionVersion;
        int selectionStart = -1;
        int selectionEnd = -1;

        VoiceInputMethod() { super(VoiceAccessibilityService.this); }

        @Override public void onStartInput(EditorInfo info, boolean restarting) {
            super.onStartInput(info, restarting);
            generation++;
            selectionVersion++;
            selectionStart = info == null ? -1 : info.initialSelStart;
            selectionEnd = info == null ? -1 : info.initialSelEnd;
        }

        @Override public void onFinishInput() {
            generation++;
            selectionVersion++;
            selectionStart = -1;
            selectionEnd = -1;
            super.onFinishInput();
        }

        @Override public void onUpdateSelection(int oldStart, int oldEnd, int newStart,
                int newEnd, int candidatesStart, int candidatesEnd) {
            super.onUpdateSelection(oldStart, oldEnd, newStart, newEnd, candidatesStart, candidatesEnd);
            selectionVersion++;
            selectionStart = newStart;
            selectionEnd = newEnd;
        }
    }

    private static final class EditorSnapshot {
        final String before;
        final String after;
        final int cursor;
        final boolean absoluteCursor;

        EditorSnapshot(String before, String after, int cursor, boolean absoluteCursor) {
            this.before = before;
            this.after = after;
            this.cursor = cursor;
            this.absoluteCursor = absoluteCursor;
        }
    }

    private static final class EditorTarget {
        final int generation;
        final String packageName;
        final int windowId;
        EditorSnapshot expected;
        int expectedSelection;
        int selectionVersion;

        EditorTarget(int generation, String packageName, int windowId, EditorSnapshot expected,
                int expectedSelection, int selectionVersion) {
            this.generation = generation;
            this.packageName = packageName;
            this.windowId = windowId;
            this.expected = expected;
            this.expectedSelection = expectedSelection;
            this.selectionVersion = selectionVersion;
        }
    }

    @Override public InputMethod onCreateInputMethod() {
        voiceInputMethod = new VoiceInputMethod();
        return voiceInputMethod;
    }

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
        else if (event.getEventType() == AccessibilityEvent.TYPE_VIEW_FOCUSED) lastFocused = null;
        DictationRun run = activeRun;
        if (run != null && target != null &&
                event.getEventType() == AccessibilityEvent.TYPE_VIEW_FOCUSED &&
                !target.equals(source)) {
            run.transcript.detachTarget();
            target = null;
            message("输入目标已变化；识别文字将保留在草稿中");
        }
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
                    if (editorTarget != null) testEditorInsert("Voice Lab 测试");
                    else message(insert("Voice Lab 测试") ? "测试文字已写入" : "该输入框暂不支持辅助功能写入");
                }
            });
            menu.addView(probe);
            if (new File(getFilesDir(), "fixture.wav").isFile()) {
                Button fixture = new Button(this);
                fixture.setText("运行测试音频");
                fixture.setOnClickListener(view -> { hideMenu(); startFixture(); });
                menu.addView(fixture);
                Button microphone = new Button(this);
                microphone.setText("播放样本并听写");
                microphone.setOnClickListener(view -> { hideMenu(); openDictation(false, 0, true); });
                menu.addView(microphone);
                Button continuous = new Button(this);
                continuous.setText("循环测试音频（10 分钟）");
                continuous.setOnClickListener(view -> { hideMenu(); openDictation(true, 600); });
                menu.addView(continuous);
            }
        }
        DictationRun previewRun = activeRun;
        if (previewRun != null && !previewRun.transcript.pending().isEmpty()) {
            TextView preview = new TextView(this);
            preview.setText("候选：" + previewRun.transcript.pending());
            preview.setMaxLines(3);
            preview.setTextColor(Color.DKGRAY);
            menu.addView(preview);
        }
        if (previewRun != null) draft = new StringBuffer(previewRun.transcript.fixed());
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

    private void startRecording() { openDictation(false); }
    private void startFixture() { openDictation(true); }

    private void openDictation(boolean fixture) { openDictation(fixture, 0); }

    private void openDictation(boolean fixture, int fixtureSeconds) { openDictation(fixture, fixtureSeconds, false); }

    private void openDictation(boolean fixture, int fixtureSeconds, boolean microphoneFixture) {
        if (activeRun != null || checkingConnection) return;
        if (!fixture && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            message("请先在 Voice Lab 授予麦克风权限");
            return;
        }
        if (!prepareTarget()) return;
        DictationRun run = new DictationRun();
        run.fixtureSeconds = fixtureSeconds;
        run.microphoneFixture = microphoneFixture;
        activeRun = run;
        checkingConnection = true;
        sessionActive = true;
        anySessionActive = true;
        cancelled = false;
        draft = new StringBuffer();
        Log.i("VoiceLabSession", "opening fixture=" + fixture + " repeatSeconds=" + fixtureSeconds);
        bubble.setText("连");
        new Thread(() -> {
            try {
                VoiceClient.Stream stream = VoiceClient.open(this, new VoiceClient.Listener() {
                    @Override public void onText(String fixedText, String pending) {
                        if (activeRun != run) return;
                        try {
                            // Save before posting to the UI: cancellation may overtake the UI queue.
                            if (!run.transcript.receive(fixedText, pending)) return;
                        } catch (IllegalArgumentException e) {
                            ui.post(() -> failRun(run, "固定文字不连续，本轮已停止自动输入"));
                            return;
                        }
                        ui.post(() -> applyDraft(run));
                    }
                    @Override public void onTerminal(String status, boolean complete) {
                        ui.post(() -> {
                            if (activeRun != run) return;
                            if (!complete) { endRun(run, status); return; }
                            run.finalStatus = status;
                            run.finalDeadlineMs = SystemClock.uptimeMillis() + 2000;
                            applyDraft(run);
                        });
                    }
                }, run.opening);
                run.stream = stream;
                ui.post(() -> {
                    if (activeRun != run || run.cancelled) { stream.cancel(); return; }
                    checkingConnection = false;
                    if (!fixture && !startMicrophoneForeground()) {
                        failRun(run, "无法启动麦克风前台服务，本轮未录音");
                        return;
                    }
                    run.capturing = true;
                    recording = true;
                    bubble.setText(fixture ? "测" : "停");
                    run.thread = new Thread(() -> {
                        if (fixture) fixtureLoop(run, new File(getFilesDir(), "fixture.wav"));
                        else captureLoop(run);
                    }, fixture ? "voice-fixture" : "voice-capture");
                    run.thread.start();
                });
            } catch (Exception e) {
                ui.post(() -> failRun(run, e.getMessage() == null
                        ? "无法建立听写会话，请检查连接和电脑服务" : e.getMessage()));
            }
        }, "voice-v1-connect").start();
    }

    private boolean startMicrophoneForeground() {
        try {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            manager.createNotificationChannel(new NotificationChannel("voice_capture", "Voice Lab 录音", NotificationManager.IMPORTANCE_LOW));
            Notification notification = new Notification.Builder(this, "voice_capture")
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setContentTitle("OneAxe Voice Lab 正在听写")
                    .setOngoing(true).build();
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                startForeground(42, notification, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
            } else startForeground(42, notification);
            foregroundStarted = true;
            return true;
        } catch (Exception e) { return false; }
    }

    private void stopRecording() {
        DictationRun run = activeRun;
        if (run == null) return;
        recording = false;
        if (run.playback != null) run.playback.cancel();
        stopCapture(run);
        if (bubble != null) bubble.setText("收");
    }

    private void cancelRecording() {
        DictationRun run = activeRun;
        if (run == null) return;
        cancelled = true;
        endRun(run, "已取消；已收到的文字保留在草稿中");
    }

    private void failRun(DictationRun run, String status) { endRun(run, status); }

    private void endRun(DictationRun run, String status) {
        if (activeRun != run) return;
        // Close both receive and input gates before the transport or old callbacks can race us.
        run.transcript.close();
        Log.i("VoiceLabSession", "ended receivedChars=" + run.transcript.fixed().length()
                + " insertedChars=" + run.transcript.insertedLength() + " status=" + status);
        draft = new StringBuffer(run.transcript.fixed());
        run.cancelled = true;
        activeRun = null;
        run.opening.cancel();
        if (run.playback != null) run.playback.cancel();
        stopCapture(run);
        if (run.stream != null) run.stream.cancel();
        recording = false;
        sessionActive = false;
        checkingConnection = false;
        anySessionActive = false;
        target = null;
        editorTarget = null;
        if (foregroundStarted) stopForeground(STOP_FOREGROUND_REMOVE);
        foregroundStarted = false;
        if (bubble != null) bubble.setText("麦");
        if (status != null && !status.isEmpty()) message(status);
    }

    private void stopCapture(DictationRun run) {
        synchronized (run) {
            run.capturing = false;
            AudioRecord audio = run.audio;
            if (audio != null) {
                try { audio.stop(); } catch (IllegalStateException ignored) { }
            }
        }
        Thread thread = run.thread;
        if (thread != null) LockSupport.unpark(thread);
    }

    private void applyDraft(DictationRun run) {
        if (activeRun != run || run.cancelled) return;
        draft = new StringBuffer(run.transcript.fixed());
        if (editorTarget != null || run.pendingEditorWrite != null) {
            applyEditorDraft(run);
            return;
        }
        if (run.pendingWrite != null) {
            InputWriteAck ack = run.pendingWrite;
            if (!targetReady()) {
                detachTarget(run, "inactive target");
            } else {
                String actual = target.isShowingHintText() || target.getText() == null ? "" : target.getText().toString();
                int start = target.getTextSelectionStart();
                int end = target.getTextSelectionEnd();
                InputWriteAck.State state = ack.observe(actual, start, end);
                if (state == InputWriteAck.State.CONFIRMED) {
                    expectedText = ack.after;
                    cursor = ack.afterCursor;
                    run.transcript.inserted(ack.addition);
                    run.pendingWrite = null;
                    Log.i("VoiceLabSession", "input receivedChars=" + run.transcript.fixed().length()
                            + " insertedChars=" + run.transcript.insertedLength());
                } else if (state == InputWriteAck.State.WAITING
                        && SystemClock.uptimeMillis() - run.pendingSinceMs < 600
                        && (run.finalStatus == null || SystemClock.uptimeMillis() < run.finalDeadlineMs)) {
                    scheduleInputPoll(run);
                    return;
                } else {
                    Log.i("VoiceLabTarget", "write unconfirmed state=" + state
                            + " expectedLength=" + ack.after.length() + " actualLength=" + actual.length()
                            + " expectedCursor=" + ack.afterCursor + " selection=" + start + ":" + end);
                    detachTarget(run, "write unconfirmed");
                }
            }
        }
        String addition = run.transcript.addition();
        if (target != null && !addition.isEmpty()) {
            if (run.finalStatus != null && SystemClock.uptimeMillis() >= run.finalDeadlineMs)
                detachTarget(run, "final write deadline");
            else if (beginWrite(run, addition)) scheduleInputPoll(run);
            else detachTarget(run, "content or cursor changed");
        }
        if (run.finalStatus != null && run.pendingWrite == null) {
            endRun(run, run.transcript.insertedLength() == run.transcript.fixed().length()
                    ? run.finalStatus : "听写结束；未写入的文字保留在悬浮菜单草稿中");
        }
    }

    private void scheduleInputPoll(DictationRun run) {
        if (run.pollScheduled) return;
        run.pollScheduled = true;
        ui.postDelayed(() -> {
            run.pollScheduled = false;
            applyDraft(run);
        }, 40);
    }

    private void detachTarget(DictationRun run, String reason) {
        Log.i("VoiceLabTarget", "input detached reason=" + reason);
        run.pendingWrite = null;
        run.pendingEditorWrite = null;
        run.transcript.detachTarget();
        target = null;
        editorTarget = null;
        message("输入目标或光标已变化；本轮文字保存在悬浮菜单草稿中");
    }

    private void applyEditorDraft(DictationRun run) {
        EditorTarget editor = editorTarget;
        if (editor == null || !editorReady(editor)) {
            detachTarget(run, "editor session changed");
            finishEditorRun(run);
            return;
        }
        EditorSnapshot actual = editorSnapshot(editorConnection());
        if (actual == null) {
            detachTarget(run, "editor context unavailable");
            finishEditorRun(run);
            return;
        }
        if (run.pendingEditorWrite != null) {
            EditorWriteAck ack = run.pendingEditorWrite;
            EditorWriteAck.State state = ack.observe(actual.before, actual.after, actual.cursor);
            if (state == EditorWriteAck.State.CONFIRMED) {
                editor.expected = actual;
                if (editor.expectedSelection >= 0) editor.expectedSelection += ack.addition.length();
                editor.selectionVersion = voiceInputMethod.selectionVersion;
                run.transcript.inserted(ack.addition);
                run.pendingEditorWrite = null;
                Log.i("VoiceLabSession", "input receivedChars=" + run.transcript.fixed().length()
                        + " insertedChars=" + run.transcript.insertedLength());
            } else if (state == EditorWriteAck.State.WAITING
                    && SystemClock.uptimeMillis() - run.pendingSinceMs < 600
                    && (run.finalStatus == null || SystemClock.uptimeMillis() < run.finalDeadlineMs)) {
                scheduleInputPoll(run);
                return;
            } else {
                Log.i("VoiceLabTarget", "editor write unconfirmed state=" + state
                        + " additionLength=" + ack.addition.length());
                detachTarget(run, "editor write unconfirmed");
                finishEditorRun(run);
                return;
            }
        }
        String addition = run.transcript.addition();
        if (!addition.isEmpty()) {
            if (run.finalStatus != null && SystemClock.uptimeMillis() >= run.finalDeadlineMs) {
                detachTarget(run, "final editor write deadline");
            } else if (!sameEditorContext(editor.expected, actual)
                    || !editorSelectionUnchanged(editor) || !commitEditor(editor, addition)) {
                detachTarget(run, "editor context or cursor changed");
            } else {
                run.pendingEditorWrite = new EditorWriteAck(actual.before, actual.after, actual.cursor,
                        addition, actual.absoluteCursor);
                run.pendingSinceMs = SystemClock.uptimeMillis();
                scheduleInputPoll(run);
            }
        }
        finishEditorRun(run);
    }

    private void finishEditorRun(DictationRun run) {
        if (run.finalStatus != null && run.pendingEditorWrite == null) {
            endRun(run, run.transcript.insertedLength() == run.transcript.fixed().length()
                    ? run.finalStatus : "听写结束；未写入的文字保留在悬浮菜单草稿中");
        }
    }

    private boolean commitEditor(EditorTarget editor, String addition) {
        if (!editorReady(editor)) return false;
        try {
            InputMethod.AccessibilityInputConnection connection = editorConnection();
            if (connection == null) return false;
            connection.commitText(addition, 1, null);
            return true;
        } catch (RuntimeException e) {
            Log.i("VoiceLabTarget", "editor commit failed type=" + e.getClass().getSimpleName());
            return false;
        }
    }

    private void testEditorInsert(String addition) {
        EditorTarget editor = editorTarget;
        if (editor == null || !editorReady(editor)) {
            message("输入框已失去焦点，请重新点选");
            return;
        }
        EditorSnapshot before = editorSnapshot(editorConnection());
        if (before == null || !sameEditorContext(editor.expected, before)
                || !editorSelectionUnchanged(editor) || !commitEditor(editor, addition)) {
            message("该输入框暂不支持写入");
            return;
        }
        Log.i("VoiceLabTarget", "probe editor committed beforeCursor=" + before.cursor
                + " beforeChars=" + before.before.length() + " afterChars=" + before.after.length()
                + " additionLength=" + addition.length());
        EditorWriteAck ack = new EditorWriteAck(before.before, before.after, before.cursor,
                addition, before.absoluteCursor);
        long deadline = SystemClock.uptimeMillis() + 600;
        Runnable poll = new Runnable() {
            @Override public void run() {
                if (!editorReady(editor)) { message("输入框已变化，无法确认写入"); return; }
                EditorSnapshot actual = editorSnapshot(editorConnection());
                if (actual == null) { message("无法读取输入结果"); return; }
                EditorWriteAck.State state = ack.observe(actual.before, actual.after, actual.cursor);
                if (state != EditorWriteAck.State.WAITING || SystemClock.uptimeMillis() >= deadline) {
                    Log.i("VoiceLabTarget", "probe editor result=" + state
                            + " beforeCursor=" + before.cursor + " afterCursor=" + actual.cursor
                            + " beforeChars=" + actual.before.length() + " afterChars=" + actual.after.length()
                            + " additionLength=" + addition.length());
                }
                if (state == EditorWriteAck.State.CONFIRMED) {
                    editor.expected = actual;
                    if (editor.expectedSelection >= 0) editor.expectedSelection += addition.length();
                    editor.selectionVersion = voiceInputMethod.selectionVersion;
                    message("测试文字已写入");
                } else if (state == EditorWriteAck.State.WAITING && SystemClock.uptimeMillis() < deadline) {
                    ui.postDelayed(this, 40);
                } else message("该输入框未确认写入；请检查框内文字");
            }
        };
        ui.postDelayed(poll, 40);
    }

    private static boolean sameEditorContext(EditorSnapshot a, EditorSnapshot b) {
        return a.cursor == b.cursor && a.absoluteCursor == b.absoluteCursor
                && a.before.equals(b.before) && a.after.equals(b.after);
    }

    private boolean editorSelectionUnchanged(EditorTarget editor) {
        if (voiceInputMethod.selectionVersion == editor.selectionVersion
                || editor.expectedSelection < 0) return true;
        boolean unchanged = voiceInputMethod.selectionStart == editor.expectedSelection
                && voiceInputMethod.selectionEnd == editor.expectedSelection;
        if (!unchanged) Log.i("VoiceLabTarget", "editor selection changed expected="
                + editor.expectedSelection + " actual=" + voiceInputMethod.selectionStart
                + ":" + voiceInputMethod.selectionEnd);
        return unchanged;
    }

    private boolean editorReady(EditorTarget editor) {
        if (Build.VERSION.SDK_INT < 33 || cancelled || voiceInputMethod == null) return false;
        boolean started = voiceInputMethod.getCurrentInputStarted();
        boolean generationMatch = voiceInputMethod.generation == editor.generation;
        boolean connectionPresent = editorConnection() != null;
        if (!started || !generationMatch || !connectionPresent) {
            Log.i("VoiceLabTarget", "editor session invalid started=" + started
                    + " generationMatch=" + generationMatch + " connection=" + connectionPresent);
            return false;
        }
        EditorInfo info = voiceInputMethod.getCurrentInputEditorInfo();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (info == null || !editor.packageName.equals(info.packageName) || passwordInput(info)
                || root == null || root.getWindowId() != editor.windowId
                || root.getPackageName() == null
                || !editor.packageName.contentEquals(root.getPackageName())) {
            Log.i("VoiceLabTarget", "editor target invalid info=" + (info != null)
                    + " packageMatch=" + (info != null && editor.packageName.equals(info.packageName))
                    + " windowMatch=" + (root != null && root.getWindowId() == editor.windowId));
            return false;
        }
        AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        return focus == null || !focus.isPassword();
    }

    private InputMethod.AccessibilityInputConnection editorConnection() {
        return voiceInputMethod == null ? null : voiceInputMethod.getCurrentInputConnection();
    }

    private EditorTarget currentEditor(AccessibilityNodeInfo root) {
        if (Build.VERSION.SDK_INT < 33 || root == null) return null;
        if (ensureVoiceInputMethod() == null) return null;
        EditorInfo info = voiceInputMethod.getCurrentInputEditorInfo();
        InputMethod.AccessibilityInputConnection connection = voiceInputMethod.getCurrentInputConnection();
        if (!voiceInputMethod.getCurrentInputStarted() || info == null || passwordInput(info)
                || info.packageName == null || root.getPackageName() == null
                || !info.packageName.contentEquals(root.getPackageName()) || connection == null) {
            Log.i("VoiceLabTarget", "editor API not ready started=" + voiceInputMethod.getCurrentInputStarted()
                    + " info=" + (info != null) + " connection=" + (connection != null)
                    + " packageMatch=" + (info != null && info.packageName != null
                    && root.getPackageName() != null && info.packageName.contentEquals(root.getPackageName())));
            return null;
        }
        EditorSnapshot snapshot = editorSnapshot(connection);
        if (snapshot == null) {
            Log.i("VoiceLabTarget", "editor context has no readable collapsed selection");
            return null;
        }
        return new EditorTarget(voiceInputMethod.generation, info.packageName,
                root.getWindowId(), snapshot, voiceInputMethod.selectionStart,
                voiceInputMethod.selectionVersion);
    }

    private VoiceInputMethod ensureVoiceInputMethod() {
        if (Build.VERSION.SDK_INT < 33) return null;
        if (voiceInputMethod != null) return voiceInputMethod;
        try {
            InputMethod method = getInputMethod();
            if (method instanceof VoiceInputMethod) voiceInputMethod = (VoiceInputMethod) method;
        } catch (RuntimeException e) {
            Log.i("VoiceLabTarget", "editor API unavailable type=" + e.getClass().getSimpleName());
        }
        return voiceInputMethod;
    }

    private static boolean passwordInput(EditorInfo info) {
        return EditorInputPolicy.blocked(info.inputType);
    }

    private static EditorSnapshot editorSnapshot(InputMethod.AccessibilityInputConnection connection) {
        if (connection == null) {
            Log.i("VoiceLabTarget", "editor context unavailable connection=null");
            return null;
        }
        try {
            SurroundingText context = connection.getSurroundingText(
                    EditorWriteAck.CONTEXT_CHARS, EditorWriteAck.CONTEXT_CHARS, 0);
            if (context == null) {
                Log.i("VoiceLabTarget", "editor context unavailable result=null");
                return null;
            }
            CharSequence text = context.getText();
            if (text == null || context.getSelectionStart() < 0
                    || context.getSelectionStart() != context.getSelectionEnd()
                    || context.getSelectionEnd() > text.length()) {
                Log.i("VoiceLabTarget", "editor context invalid offset=" + context.getOffset()
                        + " selection=" + context.getSelectionStart() + ":" + context.getSelectionEnd()
                        + " textChars=" + (text == null ? -1 : text.length()));
                return null;
            }
            int selection = context.getSelectionStart();
            boolean absolute = context.getOffset() >= 0;
            return new EditorSnapshot(text.subSequence(0, selection).toString(),
                    text.subSequence(selection, text.length()).toString(),
                    absolute ? context.getOffset() + selection : selection, absolute);
        } catch (RuntimeException e) {
            Log.i("VoiceLabTarget", "editor context unavailable type=" + e.getClass().getSimpleName());
            return null;
        }
    }

    private boolean targetReady() {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        return !cancelled && target != null && root != null && root.getWindowId() == targetWindow
                && target.refresh() && target.isFocused() && !target.isPassword()
                && target.getWindowId() == targetWindow && target.isEditable();
    }

    private boolean beginWrite(DictationRun run, String addition) {
        if (!targetReady()) return false;
        String actual = target.isShowingHintText() || target.getText() == null ? "" : target.getText().toString();
        int start = target.getTextSelectionStart();
        int end = target.getTextSelectionEnd();
        if (!actual.equals(expectedText) || !(start == cursor && end == cursor
                || actual.isEmpty() && cursor == 0 && start < 0 && end < 0)) {
            Log.i("VoiceLabTarget", "insert rejected expectedLength=" + expectedText.length()
                    + " actualLength=" + actual.length() + " expectedCursor=" + cursor
                    + " selection=" + start + ":" + end);
            return false;
        }
        InputWriteAck ack = new InputWriteAck(actual, cursor, addition);
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, ack.after);
        if (!target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) return false;
        Bundle selection = new Bundle();
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, ack.afterCursor);
        selection.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, ack.afterCursor);
        target.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, selection);
        run.pendingWrite = ack;
        run.pendingSinceMs = SystemClock.uptimeMillis();
        return true;
    }

    private boolean prepareTarget() {
        cancelled = false;
        target = null;
        editorTarget = null;
        AccessibilityNodeInfo root = getRootInActiveWindow();
        AccessibilityNodeInfo focused = root == null ? null : root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (focused != null && focused.isPassword()) {
            message("密码输入框不支持听写");
            return false;
        }
        VoiceInputMethod method = ensureVoiceInputMethod();
        if (method != null && method.getCurrentInputStarted()) {
            EditorInfo info = method.getCurrentInputEditorInfo();
            if (info != null && passwordInput(info)) {
                message((info.inputType & InputType.TYPE_MASK_CLASS) == InputType.TYPE_NULL
                        ? "该输入框未提供可验证的输入类型" : "密码输入框不支持听写");
                return false;
            }
        }
        EditorTarget editor = currentEditor(root);
        if (editor != null) {
            editorTarget = editor;
            Log.i("VoiceLabTarget", "editor input attached window=" + editor.windowId);
            return true;
        }
        if (focused == null || !focused.isEditable()) {
            focused = lastFocused;
            if (focused != null && (!focused.refresh() || !focused.isFocused()
                    || root == null || root.getPackageName() == null
                    || focused.getPackageName() == null
                    || !root.getPackageName().toString().contentEquals(focused.getPackageName()))) {
                focused = null;
            }
        }
        Log.i("VoiceLabTarget", "root=" + (root == null ? -1 : root.getWindowId()) +
                " focused=" + (focused != null) + " editable=" + (focused != null && focused.isEditable()) +
                " selection=" + (focused == null ? -2 : focused.getTextSelectionStart()));
        if (focused == null || root == null || root.getWindowId() != focused.getWindowId() ||
                !focused.isEditable() || !focused.isFocused() || focused.isPassword() ||
                focused.getTextSelectionStart() < 0 && !emptyEditor(focused)) {
            message("请先选中可编辑输入框，或点击输入框显示光标"); return false;
        }
        target = focused;
        targetWindow = focused.getWindowId();
        expectedText = focused.isShowingHintText() || focused.getText() == null ? "" : focused.getText().toString();
        cursor = Math.max(0, focused.getTextSelectionStart());
        return true;
    }

    private boolean emptyEditor(AccessibilityNodeInfo node) {
        return node.isShowingHintText() || node.getText() == null || node.getText().length() == 0;
    }

    private void captureLoop(DictationRun run) {
        AudioRecord audio = null;
        try {
            int frameBytes = run.stream.frameBytes();
            int buffer = Math.max(AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT), frameBytes * 2);
            audio = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, buffer);
            synchronized (run) {
                run.audio = audio;
                if (audio.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException();
                if (run.cancelled || !run.capturing) return;
                audio.startRecording();
                if (audio.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IllegalStateException();
            }
            Log.i("VoiceLabSession", "microphone started rate=16000 channels=1");
            if (run.microphoneFixture) ui.post(() -> {
                if (activeRun != run || run.cancelled || !run.capturing) return;
                run.playback = new FixturePlayback(this);
                run.playback.start(new File(getFilesDir(), "fixture.wav"), () -> ui.postDelayed(() -> {
                    if (activeRun == run && !run.cancelled) stopRecording();
                }, 300), error -> failRun(run, error));
            });
            byte[] frame = new byte[frameBytes];
            int filled = 0;
            while (run.capturing && !run.cancelled) {
                int size = audio.read(frame, filled, frame.length - filled, AudioRecord.READ_BLOCKING);
                if (size < 0) {
                    if (!run.capturing) break;
                    throw new IllegalStateException();
                }
                if (size == 0) continue;
                filled += size;
                if (filled == frame.length) {
                    if (!run.cancelled && !run.stream.offerAudio(Arrays.copyOf(frame, filled))) return;
                    filled = 0;
                }
            }
            // A short tail contains only real samples; no synthetic padding or VAD deletion.
            int tail = filled & ~1;
            if (!run.cancelled && tail > 0 && !run.stream.offerAudio(Arrays.copyOf(frame, tail))) return;
        } catch (Exception e) {
            if (!run.cancelled) {
                run.cancelled = true;
                ui.post(() -> failRun(run, "录音设备读取失败，本轮已停止"));
            }
        } finally {
            run.audio = null;
            if (audio != null) {
                try { audio.stop(); } catch (IllegalStateException ignored) { }
                audio.release();
                Log.i("VoiceLabSession", "microphone released");
            }
            finishCapture(run);
        }
    }

    private void fixtureLoop(DictationRun run, File fixture) {
        try {
            byte[] pcm = WavPcm16.readMono16k(fixture);
            int frameBytes = run.stream.frameBytes();
            long started = System.nanoTime();
            long totalBytes = run.fixtureSeconds > 0 ? run.fixtureSeconds * 32000L : pcm.length;
            for (long sent = 0; sent < totalBytes && run.capturing && !run.cancelled;) {
                int offset = (int) (sent % pcm.length);
                int count = (int) Math.min(Math.min(frameBytes, pcm.length - offset), totalBytes - sent);
                long due = started + (sent + count) * 1_000_000_000L / 32000;
                while (run.capturing && !run.cancelled && System.nanoTime() < due) {
                    LockSupport.parkNanos(Math.min(due - System.nanoTime(), 50_000_000L));
                }
                if (!run.capturing || run.cancelled) break;
                if (!run.stream.offerAudio(Arrays.copyOfRange(pcm, offset, offset + count))) return;
                sent += count;
            }
        } catch (Exception e) {
            run.cancelled = true;
            ui.post(() -> failRun(run, "测试音频无法读取，请检查 16 kHz 单声道 PCM16 WAV 文件"));
        } finally { finishCapture(run); }
    }

    private void finishCapture(DictationRun run) {
        run.capturing = false;
        ui.post(() -> {
            if (activeRun != run) return;
            recording = false;
            if (foregroundStarted) stopForeground(STOP_FOREGROUND_REMOVE);
            foregroundStarted = false;
            if (bubble != null) bubble.setText("收");
        });
        if (!run.cancelled) run.stream.finish();
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
