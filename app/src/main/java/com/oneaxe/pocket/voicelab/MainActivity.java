package com.oneaxe.pocket.voicelab;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private final ExecutorService connectionChecks = Executors.newSingleThreadExecutor();
    private TextView connectionStatus;
    private TextView savedAddress;
    private EditText host;
    private EditText port;
    private EditText token;
    private Spinner scheme;
    private boolean checking;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        ScrollView scroll = new ScrollView(this);
        getWindow().getDecorView().setSystemUiVisibility(
                android.view.View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR |
                android.view.View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        scroll.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            return insets;
        });
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(pad, pad, pad, pad);
        scroll.addView(layout);
        TextView title = label(layout, "OneAxe Voice Lab");
        title.setTextSize(23);
        label(layout, "请先自行连接 Tailscale，再访问电脑的语音服务。连接失败时会显示原因，不会自动开启 VPN。");
        label(layout, "手机只使用电脑当前加载的模型。当前版本可配置和检查连接、录音回放；手机听写等待电脑端接口接入。");

        TextView section = label(layout, "连接设置");
        section.setTextSize(19);
        label(layout, "协议（默认 HTTPS，仅通过 Tailscale 连接）");
        scheme = new Spinner(this);
        scheme.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"https", "http"}));
        scheme.setSelection("http".equals(Settings.scheme(this)) ? 1 : 0);
        layout.addView(scheme);
        label(layout, "Tailscale 主机");
        host = field(layout, "rtx4090.nase-stairs.ts.net",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        host.setText(Settings.host(this));
        label(layout, "端口");
        port = field(layout, "8097", InputType.TYPE_CLASS_NUMBER);
        port.setText(Integer.toString(Settings.port(this)));
        label(layout, "手机专用凭据（可稍后设置）");
        token = field(layout, "不要填写电脑的模型管理令牌",
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        token.setSaveEnabled(false);
        token.setImportantForAutofill(android.view.View.IMPORTANT_FOR_AUTOFILL_NO);
        label(layout, "同一地址留空保留已有凭据；主机或端口变更后需重新设置。没有凭据也可先检查连接。");
        savedAddress = label(layout, "已保存：" + Settings.url(this));
        connectionStatus = label(layout, "尚未检查连接。默认地址不代表电脑已开放语音服务。");

        Button save = button(layout, "保存连接设置");
        Button check = button(layout, "检查已保存的连接");
        save.setOnClickListener(v -> {
            try {
                int value;
                try { value = Integer.parseInt(port.getText().toString().trim()); }
                catch (NumberFormatException e) { throw new IllegalArgumentException("端口需为 1–65535 的整数"); }
                Settings.save(this, scheme.getSelectedItem().toString(), host.getText().toString().trim(),
                        value, token.getText().toString());
                token.setText("");
                host.setText(Settings.host(this));
                savedAddress.setText("已保存：" + Settings.url(this));
                connectionStatus.setText("设置已保存，请检查连接。");
                Toast.makeText(this, "连接设置已保存", Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                showConnectionResult(e.getMessage() == null ? "保存失败，请检查连接设置" : e.getMessage());
            }
        });
        check.setOnClickListener(v -> {
            if (checking) return;
            checking = true;
            check.setEnabled(false);
            save.setEnabled(false);
            connectionStatus.setText("正在检查 " + Settings.url(this) + " …");
            connectionChecks.execute(() -> {
                String result;
                try { result = VoiceClient.probe(this); }
                catch (Exception e) { result = e.getMessage() == null ? "连接检查失败，请检查 Tailscale 和电脑服务" : e.getMessage(); }
                String message = result;
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    checking = false;
                    check.setEnabled(true);
                    save.setEnabled(true);
                    showConnectionResult(message);
                });
            });
        });
        button(layout, "清除手机凭据").setOnClickListener(v -> {
            Settings.clearToken(this);
            token.setText("");
            showConnectionResult("手机凭据已清除，主机和端口保留");
        });

        TextView inputSection = label(layout, "输入与录音检查");
        inputSection.setTextSize(19);
        button(layout, "授予麦克风权限").setOnClickListener(v ->
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1));
        button(layout, "录音检查与回放").setOnClickListener(v -> {
            if (VoiceAccessibilityService.isAnySessionActive()) {
                Toast.makeText(this, "请先结束或取消听写", Toast.LENGTH_SHORT).show();
            } else {
                startActivity(new Intent(this, RecordCheckActivity.class));
            }
        });
        button(layout, "开启悬浮输入服务").setOnClickListener(v ->
                startActivity(new Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS)));
        EditText test = new EditText(this);
        test.setHint("App 内输入框测试");
        layout.addView(test);
        setContentView(scroll);
    }

    private TextView label(LinearLayout parent, String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setPadding(0, 12, 0, 6);
        parent.addView(view);
        return view;
    }

    private EditText field(LinearLayout parent, String hint, int type) {
        EditText view = new EditText(this);
        view.setSingleLine(true);
        view.setInputType(type);
        view.setHint(hint);
        parent.addView(view, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        return view;
    }

    private Button button(LinearLayout parent, String title) {
        Button view = new Button(this);
        view.setText(title);
        parent.addView(view);
        return view;
    }

    private void showConnectionResult(String message) {
        connectionStatus.setText(message);
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    @Override protected void onDestroy() {
        connectionChecks.shutdownNow();
        super.onDestroy();
    }
}
