use crate::tray::Tray;
use gio::prelude::*;
use glib::translate::ToGlibPtr;
use gtk::prelude::*;
use oneaxe_voice_linux::{audio, credentials, desktop, protocol, settings, transport};
use std::cell::RefCell;
use std::collections::VecDeque;
use std::ffi::CString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Phase {
    Idle,
    Connecting,
    Recording,
    Finishing,
    Error,
}

impl Phase {
    fn name(self) -> &'static str {
        match self {
            Self::Idle => "idle",
            Self::Connecting => "connecting",
            Self::Recording => "recording",
            Self::Finishing => "finishing",
            Self::Error => "error",
        }
    }
    fn active(self) -> bool {
        matches!(self, Self::Connecting | Self::Recording | Self::Finishing)
    }
}

#[derive(Clone)]
struct Widgets {
    window: gtk::ApplicationWindow,
    settings: gtk::ApplicationWindow,
    preview: gtk::ApplicationWindow,
    status: gtk::Label,
    model: gtk::Label,
    delivery: gtk::Label,
    level: gtk::LevelBar,
    elapsed: gtk::Label,
    fixed: gtk::TextBuffer,
    pending: gtk::Label,
    preview_fixed: gtk::Label,
    preview_pending: gtk::Label,
    toggle: gtk::Button,
    cancel: gtk::Button,
    copy: gtk::Button,
    server: gtk::Entry,
    token: gtk::Entry,
    credential: gtk::Label,
    microphone: gtk::ComboBoxText,
    shortcut: gtk::Entry,
    auto_paste: gtk::CheckButton,
    show_preview: gtk::CheckButton,
    autostart: gtk::CheckButton,
    pause: gtk::SpinButton,
    save: gtk::Button,
    check: gtk::Button,
    hotkey: gtk::Button,
    settings_status: gtk::Label,
    tray_status: gtk::MenuItem,
    tray_toggle: gtk::MenuItem,
    tray_cancel: gtk::MenuItem,
    tray_copy: gtk::MenuItem,
}

struct State {
    app: gtk::Application,
    _hold: gio::ApplicationHoldGuard,
    ui: Widgets,
    tray: Option<Tray>,
    tx: mpsc::Sender<Event>,
    delivery_tx: DeliverySender,
    credentials: Arc<Mutex<credentials::CredentialStore>>,
    config: settings::Config,
    token: Option<protocol::DeviceToken>,
    persistence: Option<credentials::Persistence>,
    loaded: bool,
    settings_busy: bool,
    request: u64,
    phase: Phase,
    status: String,
    model: String,
    generation: u64,
    cancelled: Arc<AtomicBool>,
    target_ready: bool,
    target: Option<desktop::Target>,
    session: Option<transport::SessionHandle>,
    capture: Option<audio::CaptureHandle>,
    capture_started: bool,
    terminal: bool,
    fixed: String,
    pending: String,
    seconds: f64,
    buffered_seconds: f64,
    sent_samples: u64,
    delivery_state: String,
    quitting: bool,
}

enum Event {
    Loaded {
        config: settings::Config,
        token: Option<protocol::DeviceToken>,
        persistence: Option<credentials::Persistence>,
        sources: Vec<audio::Source>,
        warning: Option<String>,
    },
    Saved {
        request: u64,
        config: settings::Config,
        token: Option<protocol::DeviceToken>,
        persistence: Option<credentials::Persistence>,
        warning: Option<String>,
    },
    SettingsFailed {
        request: u64,
        message: String,
    },
    Checked {
        request: u64,
        result: Result<protocol::Capabilities, String>,
    },
    Hotkey {
        request: u64,
        result: Result<(), String>,
    },
    Sources(Result<Vec<audio::Source>, String>),
    Target {
        generation: u64,
        target: Option<desktop::Target>,
        warning: Option<String>,
    },
    Prepared {
        generation: u64,
        session: transport::SessionHandle,
        caps: protocol::Capabilities,
    },
    Network {
        generation: u64,
        event: protocol::SessionEvent,
    },
    CopyOnly {
        generation: u64,
    },
    StartFailed {
        generation: u64,
        message: String,
    },
    Capture {
        generation: u64,
        event: audio::CaptureEvent,
    },
    CaptureReady {
        generation: u64,
        capture: Result<audio::CaptureHandle, String>,
    },
    Delivered {
        generation: u64,
        result: Result<desktop::DeliveryOutcome, String>,
        final_text: bool,
    },
    Copied {
        generation: u64,
        result: Result<(), String>,
    },
    QuitReady,
}

enum DeliveryJob {
    Start {
        generation: u64,
        target: Option<desktop::Target>,
        auto_paste: bool,
    },
    OnlyCopy {
        generation: u64,
    },
    Text {
        generation: u64,
        fixed: String,
        gate: transport::DeliveryGate,
        final_text: bool,
    },
    ManualCopy {
        generation: u64,
        fixed: String,
    },
    Quit,
}

#[derive(Clone)]
struct DeliverySender {
    queue: Arc<Mutex<VecDeque<DeliveryJob>>>,
    wake: mpsc::SyncSender<()>,
}

impl DeliverySender {
    fn send(&self, job: DeliveryJob) -> Result<(), ()> {
        let mut queue = self.queue.lock().map_err(|_| ())?;
        if let DeliveryJob::Text {
            generation,
            final_text,
            ..
        } = &job
        {
            if let Some(DeliveryJob::Text {
                generation: previous,
                final_text: was_final,
                ..
            }) = queue.back()
            {
                if previous == generation && (!was_final || *final_text) {
                    queue.pop_back();
                }
            }
        }
        queue.push_back(job);
        drop(queue);
        match self.wake.try_send(()) {
            Ok(()) | Err(mpsc::TrySendError::Full(_)) => Ok(()),
            Err(_) => Err(()),
        }
    }
}

enum TextPlan {
    Skip,
    Copy,
    Paste {
        body: String,
        prefix: &'static str,
        next: String,
    },
}

#[derive(Default)]
struct DeliveryPlan {
    clipboard_only: bool,
    received: String,
    pasted: String,
}

impl DeliveryPlan {
    fn manual_copy(&mut self) {
        self.clipboard_only = true;
    }

    fn plan(&mut self, fixed: &str) -> Result<TextPlan, ()> {
        let normalized = desktop::plain_text(fixed);
        if normalized == self.received {
            return Ok(TextPlan::Skip);
        }
        self.received = normalized.clone();
        if self.clipboard_only {
            return Ok(TextPlan::Copy);
        }
        let delta = normalized.strip_prefix(&self.pasted).ok_or(())?;
        let prefix = if delta.starts_with(' ') { " " } else { "" };
        Ok(TextPlan::Paste {
            body: delta.trim_start().into(),
            prefix,
            next: normalized,
        })
    }
    fn complete(&mut self, outcome: desktop::DeliveryOutcome, next: Option<String>) {
        match outcome {
            desktop::DeliveryOutcome::Pasted => {
                if let Some(next) = next {
                    self.pasted = next;
                }
            }
            desktop::DeliveryOutcome::FocusChanged => self.clipboard_only = true,
            _ => {}
        }
    }
}

fn should_deliver(previous: &str, fixed: &str, final_text: bool) -> bool {
    final_text || previous != fixed
}

type Shared = Rc<RefCell<State>>;

pub fn run(args: Vec<String>) -> i32 {
    if args
        .iter()
        .any(|a| matches!(a.as_str(), "--status" | "--quit" | "--cancel"))
        && !instance_running()
    {
        if args.iter().any(|a| a == "--status") {
            println!(
                "{}",
                serde_json::json!({"running":false,"state":"stopped","pid":null,"active":false,"capture_active":false,"preparing":false,"recognizing":false,"delivery_state":"none","audio_sent_samples":0,"bytes_sent":0,"fixed_chars":0,"pending_chars":0})
            );
        }
        return 0;
    }
    let app = gtk::Application::new(
        Some("org.oneaxe.VoiceLinux"),
        gio::ApplicationFlags::HANDLES_COMMAND_LINE,
    );
    let owner: Rc<RefCell<Option<Shared>>> = Rc::new(RefCell::new(None));
    let startup_owner = owner.clone();
    app.connect_startup(move |app| {
        let (tx, rx) = mpsc::channel();
        let delivery_tx = delivery_worker(tx.clone());
        let ui = build_widgets(app);
        let state = Rc::new(RefCell::new(State {
            app: app.clone(),
            _hold: app.hold(),
            ui,
            tray: None,
            tx: tx.clone(),
            delivery_tx,
            credentials: Arc::new(Mutex::new(credentials::CredentialStore::new())),
            config: settings::Config::default(),
            token: None,
            persistence: None,
            loaded: false,
            settings_busy: false,
            request: 0,
            phase: Phase::Idle,
            status: "正在读取设置".into(),
            model: "未查询".into(),
            generation: 0,
            cancelled: Arc::new(AtomicBool::new(false)),
            target_ready: false,
            target: None,
            session: None,
            capture: None,
            capture_started: false,
            terminal: false,
            fixed: String::new(),
            pending: String::new(),
            seconds: 0.0,
            buffered_seconds: 0.0,
            sent_samples: 0,
            delivery_state: "none".into(),
            quitting: false,
        }));
        connect_widgets(&state);
        let menu = build_tray_menu(&state);
        state.borrow_mut().tray = Tray::new(&menu);
        refresh(&state.borrow());
        load_settings(&state);
        let poll = state.clone();
        glib::timeout_add_local(Duration::from_millis(40), move || {
            for event in rx.try_iter().take(100) {
                process_event(&poll, event);
            }
            glib::ControlFlow::Continue
        });
        *startup_owner.borrow_mut() = Some(state);
    });
    let commands = owner.clone();
    app.connect_command_line(move |app, command| {
        let Some(state) = commands.borrow().clone() else { return 1; };
        let args: Vec<_> = command.arguments().into_iter().skip(1).map(|s| s.to_string_lossy().into_owned()).collect();
        if args.iter().any(|s| s == "--status") {
            let s = state.borrow();
            let report = serde_json::json!({"running":true,"pid":std::process::id(),"state":s.phase.name(),"active":s.phase.active(),"capture_active":s.capture_started&&s.capture.as_ref().is_some_and(|c|c.is_active()),"preparing":s.phase==Phase::Connecting,"recognizing":matches!(s.phase,Phase::Recording|Phase::Finishing),"delivery_state":s.delivery_state,"audio_sent_samples":s.sent_samples,"bytes_sent":s.sent_samples.saturating_mul(2),"configured":s.loaded&&s.token.is_some(),"recording":s.phase==Phase::Recording,"server":s.config.server_url,"model":s.model,"fixed_chars":s.fixed.chars().count(),"pending_chars":s.pending.chars().count(),"buffered_seconds":s.buffered_seconds,"tray_available":s.tray.is_some()});
            command_print(command, &report.to_string());
        } else if args.iter().any(|s| s == "--quit") { quit(&state); }
        else if args.iter().any(|s| s == "--cancel") { cancel(&state, "已取消，已确认文字保留"); }
        else if args.iter().any(|s| s == "--toggle") { toggle(&state, true); }
        else if args.iter().any(|s| s == "--background") {
            if state.borrow().tray.is_none() { show_without_focus(&state.borrow().ui.window); }
        } else { let s = state.borrow(); s.ui.window.show_all(); s.ui.window.present(); }
        if args.iter().any(|s| s == "--show") { let s = state.borrow(); s.ui.window.show_all(); s.ui.window.present(); }
        let _ = app;
        0
    });
    let result = app.run_with_args(&args);
    if let Some(state) = owner.borrow().as_ref() {
        let s = state.borrow();
        s.cancelled.store(true, Ordering::Release);
        if let Some(session) = &s.session {
            session.cancel();
        }
        if let Some(capture) = &s.capture {
            capture.cancel();
        }
        let _ = s.delivery_tx.send(DeliveryJob::Quit);
    }
    result.value()
}

fn instance_running() -> bool {
    let Ok(bus) = gio::bus_get_sync(gio::BusType::Session, None::<&gio::Cancellable>) else {
        return false;
    };
    bus.call_sync(
        Some("org.freedesktop.DBus"),
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "NameHasOwner",
        Some(&("org.oneaxe.VoiceLinux",).to_variant()),
        None,
        gio::DBusCallFlags::NONE,
        1000,
        None::<&gio::Cancellable>,
    )
    .ok()
    .and_then(|reply| reply.get::<(bool,)>())
    .is_some_and(|value| value.0)
}

fn command_print(command: &gio::ApplicationCommandLine, value: &str) {
    let Ok(value) = CString::new(value) else {
        return;
    };
    unsafe {
        gio::ffi::g_application_command_line_print(
            command.to_glib_none().0,
            b"%s\n\0".as_ptr().cast(),
            value.as_ptr(),
        );
    }
}

fn icon_button(icon: &str, label: &str) -> gtk::Button {
    let button = gtk::Button::new();
    button.set_image(Some(&gtk::Image::from_icon_name(
        Some(icon),
        gtk::IconSize::Button,
    )));
    if !label.is_empty() {
        button.set_label(label);
        button.set_always_show_image(true);
    }
    button.set_tooltip_text(Some(if label.is_empty() { icon } else { label }));
    button
}

fn text_label(text: &str) -> gtk::Label {
    let label = gtk::Label::new(Some(text));
    label.set_xalign(0.0);
    label.set_line_wrap(true);
    label.set_line_wrap_mode(gtk::pango::WrapMode::WordChar);
    label
}

fn build_widgets(app: &gtk::Application) -> Widgets {
    let window = gtk::ApplicationWindow::new(app);
    window.set_title("OneAxe Voice");
    window.set_default_size(640, 480);
    window.set_size_request(440, 360);
    if let Some(path) = crate::tray::icon_path(false) {
        let _ = window.set_icon_from_file(path);
    }
    let header = gtk::HeaderBar::new();
    header.set_title(Some("OneAxe Voice"));
    header.set_subtitle(Some("Linux"));
    header.set_show_close_button(true);
    let settings_button = icon_button("preferences-system-symbolic", "");
    settings_button.set_tooltip_text(Some("设置"));
    header.pack_end(&settings_button);
    window.set_titlebar(Some(&header));
    let layout = gtk::Box::new(gtk::Orientation::Vertical, 12);
    layout.set_border_width(18);
    let status = text_label("正在读取设置");
    status.style_context().add_class("title");
    let model = text_label("服务器模型：未查询");
    model.style_context().add_class("dim-label");
    let delivery = text_label("仅复制");
    delivery.style_context().add_class("dim-label");
    layout.pack_start(&status, false, false, 0);
    layout.pack_start(&model, false, false, 0);
    let meter = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    let level = gtk::LevelBar::new();
    level.set_hexpand(true);
    level.set_min_value(0.0);
    level.set_max_value(1.0);
    let elapsed = text_label("00:00");
    elapsed.set_width_chars(10);
    meter.pack_start(&level, true, true, 0);
    meter.pack_end(&elapsed, false, false, 0);
    layout.pack_start(&meter, false, false, 0);
    let transcript = gtk::TextView::new();
    transcript.set_editable(false);
    transcript.set_cursor_visible(false);
    transcript.set_wrap_mode(gtk::WrapMode::WordChar);
    transcript.set_left_margin(10);
    transcript.set_right_margin(10);
    transcript.set_top_margin(10);
    transcript.set_bottom_margin(10);
    let fixed = transcript.buffer().unwrap();
    let scroll = gtk::ScrolledWindow::new(None::<&gtk::Adjustment>, None::<&gtk::Adjustment>);
    scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
    scroll.set_shadow_type(gtk::ShadowType::In);
    scroll.add(&transcript);
    layout.pack_start(&scroll, true, true, 0);
    let pending = text_label("");
    pending.set_lines(3);
    pending.set_ellipsize(gtk::pango::EllipsizeMode::End);
    pending.style_context().add_class("dim-label");
    layout.pack_start(&pending, false, false, 0);
    layout.pack_start(&delivery, false, false, 0);
    let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    let toggle = icon_button("media-record-symbolic", "开始");
    toggle.style_context().add_class("suggested-action");
    let cancel = icon_button("process-stop-symbolic", "取消");
    let copy = icon_button("edit-copy-symbolic", "复制");
    actions.pack_start(&toggle, false, false, 0);
    actions.pack_start(&cancel, false, false, 0);
    actions.pack_end(&copy, false, false, 0);
    layout.pack_end(&actions, false, false, 0);
    window.add(&layout);

    let settings = gtk::ApplicationWindow::new(app);
    settings.set_title("OneAxe Voice · 设置");
    settings.set_default_size(600, 560);
    settings.set_size_request(440, 360);
    settings.set_transient_for(Some(&window));
    let settings_layout = gtk::Box::new(gtk::Orientation::Vertical, 8);
    settings_layout.set_border_width(12);
    let form_scroll = gtk::ScrolledWindow::new(None::<&gtk::Adjustment>, None::<&gtk::Adjustment>);
    form_scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
    form_scroll.set_propagate_natural_height(false);
    form_scroll.set_min_content_height(180);
    let form = gtk::Box::new(gtk::Orientation::Vertical, 14);
    form.set_border_width(8);
    let grid = gtk::Grid::new();
    grid.set_row_spacing(12);
    grid.set_column_spacing(14);
    let server = gtk::Entry::new();
    server.set_hexpand(true);
    server.set_placeholder_text(Some("https://服务器.ts.net:8097"));
    let token = gtk::Entry::new();
    token.set_visibility(false);
    token.set_input_purpose(gtk::InputPurpose::Password);
    token.set_placeholder_text(Some("留空使用该服务器已保存的设备凭据"));
    let credential = text_label("凭据：未配置");
    credential.style_context().add_class("dim-label");
    let microphone = gtk::ComboBoxText::new();
    microphone.set_hexpand(true);
    let refresh_sources = icon_button("view-refresh-symbolic", "");
    refresh_sources.set_tooltip_text(Some("刷新麦克风"));
    let mic_row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    mic_row.pack_start(&microphone, true, true, 0);
    mic_row.pack_end(&refresh_sources, false, false, 0);
    let shortcut = gtk::Entry::new();
    shortcut.set_width_chars(12);
    shortcut.set_placeholder_text(Some("F9"));
    let hotkey = icon_button("input-keyboard-symbolic", "应用快捷键");
    let shortcut_row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    shortcut_row.pack_start(&shortcut, true, true, 0);
    shortcut_row.pack_end(&hotkey, false, false, 0);
    let pause = gtk::SpinButton::with_range(200.0, 10000.0, 100.0);
    pause.set_numeric(true);
    for (row, (title, widget)) in [
        ("服务器地址", server.clone().upcast::<gtk::Widget>()),
        ("设备凭据", token.clone().upcast()),
        ("", credential.clone().upcast()),
        ("麦克风", mic_row.upcast()),
        ("听写快捷键", shortcut_row.upcast()),
        ("停顿收尾（ms）", pause.clone().upcast()),
    ]
    .into_iter()
    .enumerate()
    {
        let label = text_label(title);
        grid.attach(&label, 0, row as i32, 1, 1);
        grid.attach(&widget, 1, row as i32, 1, 1);
    }
    let auto_paste = gtk::CheckButton::with_label("自动输入到开始时的目标窗口");
    let show_preview = gtk::CheckButton::with_label("显示候选字幕");
    let autostart = gtk::CheckButton::with_label("登录后启动");
    form.pack_start(&grid, false, false, 0);
    for check in [&auto_paste, &show_preview, &autostart] {
        form.pack_start(check, false, false, 0);
    }
    let settings_status = text_label("");
    settings_status.set_selectable(true);
    settings_status.set_max_width_chars(58);
    settings_status.set_lines(2);
    settings_status.set_ellipsize(gtk::pango::EllipsizeMode::End);
    settings_status.set_vexpand(false);
    settings_status.style_context().add_class("dim-label");
    let settings_footer = gtk::Box::new(gtk::Orientation::Vertical, 8);
    settings_footer.set_border_width(8);
    settings_footer.pack_start(&settings_status, false, false, 0);
    let setting_actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    let check = icon_button("network-transmit-receive-symbolic", "测试连接");
    let save = icon_button("document-save-symbolic", "保存");
    save.style_context().add_class("suggested-action");
    setting_actions.pack_start(&check, false, false, 0);
    setting_actions.pack_end(&save, false, false, 0);
    settings_footer.pack_start(&setting_actions, false, false, 0);
    form_scroll.add(&form);
    settings_layout.pack_start(&form_scroll, true, true, 0);
    settings_layout.pack_end(&settings_footer, false, false, 0);
    settings.add(&settings_layout);
    let preview = gtk::ApplicationWindow::new(app);
    preview.set_title("OneAxe Voice · 字幕");
    preview.set_default_size(620, 110);
    preview.set_size_request(320, 80);
    preview.set_decorated(false);
    preview.set_resizable(false);
    preview.set_keep_above(true);
    preview.set_accept_focus(false);
    preview.set_focus_on_map(false);
    preview.set_skip_taskbar_hint(true);
    preview.set_skip_pager_hint(true);
    preview.set_type_hint(gtk::gdk::WindowTypeHint::Utility);
    let preview_box = gtk::Box::new(gtk::Orientation::Vertical, 6);
    preview_box.set_border_width(14);
    let preview_fixed = text_label("");
    preview_fixed.set_max_width_chars(74);
    preview_fixed.set_lines(3);
    preview_fixed.set_ellipsize(gtk::pango::EllipsizeMode::Start);
    let preview_pending = text_label("");
    preview_pending.set_max_width_chars(74);
    preview_pending.set_lines(2);
    preview_pending.set_ellipsize(gtk::pango::EllipsizeMode::End);
    preview_pending.style_context().add_class("dim-label");
    preview_box.pack_start(&preview_fixed, false, false, 0);
    preview_box.pack_start(&preview_pending, false, false, 0);
    preview.add(&preview_box);
    let settings_for_button = settings.clone();
    settings_button.connect_clicked(move |_| {
        settings_for_button.show_all();
        settings_for_button.present();
    });
    preview.add_events(gtk::gdk::EventMask::BUTTON_PRESS_MASK);
    preview.connect_button_press_event(|window, event| {
        if event.button() == 1 {
            let (x, y) = event.root();
            window.begin_move_drag(1, x as i32, y as i32, event.time());
            glib::Propagation::Stop
        } else {
            glib::Propagation::Proceed
        }
    });
    let mic_for_refresh = refresh_sources.clone();
    mic_for_refresh.set_widget_name("refresh-sources");
    let tray_status = gtk::MenuItem::with_label("OneAxe Voice");
    tray_status.set_sensitive(false);
    let tray_toggle = gtk::MenuItem::with_label("开始");
    let tray_cancel = gtk::MenuItem::with_label("取消");
    let tray_copy = gtk::MenuItem::with_label("复制");
    Widgets {
        window,
        settings,
        preview,
        status,
        model,
        delivery,
        level,
        elapsed,
        fixed,
        pending,
        preview_fixed,
        preview_pending,
        toggle,
        cancel,
        copy,
        server,
        token,
        credential,
        microphone,
        shortcut,
        auto_paste,
        show_preview,
        autostart,
        pause,
        save,
        check,
        hotkey,
        settings_status,
        tray_status,
        tray_toggle,
        tray_cancel,
        tray_copy,
    }
}

fn connect_widgets(state: &Shared) {
    let ui = state.borrow().ui.clone();
    for window in [&ui.window, &ui.settings] {
        window.connect_delete_event(|window, _| {
            window.hide();
            glib::Propagation::Stop
        });
    }
    ui.preview.connect_delete_event(|window, _| {
        window.hide();
        glib::Propagation::Stop
    });
    let s = state.clone();
    ui.toggle.connect_clicked(move |_| toggle(&s, false));
    let s = state.clone();
    ui.cancel
        .connect_clicked(move |_| cancel(&s, "已取消，已确认文字保留"));
    let s = state.clone();
    ui.copy.connect_clicked(move |_| copy(&s));
    let s = state.clone();
    ui.save.connect_clicked(move |_| save_settings(&s));
    let s = state.clone();
    ui.check.connect_clicked(move |_| check_connection(&s));
    let s = state.clone();
    ui.hotkey.connect_clicked(move |_| install_hotkey(&s));
    let token = ui.token.clone();
    let credential = ui.credential.clone();
    ui.server.connect_changed(move |_| {
        token.set_text("");
        credential.set_text("凭据与服务器地址绑定");
    });
    if let Some(button) = find_named(&ui.settings.clone().upcast(), "refresh-sources")
        .and_then(|w| w.downcast::<gtk::Button>().ok())
    {
        let tx = state.borrow().tx.clone();
        button.connect_clicked(move |_| {
            let tx = tx.clone();
            thread::spawn(move || {
                let _ = tx.send(Event::Sources(audio::sources().map_err(|e| e.to_string())));
            });
        });
    }
}

fn find_named(widget: &gtk::Widget, name: &str) -> Option<gtk::Widget> {
    if widget.widget_name() == name {
        return Some(widget.clone());
    }
    if let Ok(container) = widget.clone().downcast::<gtk::Container>() {
        for child in container.children() {
            if let Some(found) = find_named(&child, name) {
                return Some(found);
            }
        }
    }
    None
}

fn build_tray_menu(state: &Shared) -> gtk::Menu {
    let ui = state.borrow().ui.clone();
    let menu = gtk::Menu::new();
    menu.append(&ui.tray_status);
    menu.append(&gtk::SeparatorMenuItem::new());
    menu.append(&ui.tray_toggle);
    menu.append(&ui.tray_cancel);
    let show = gtk::MenuItem::with_label("显示窗口");
    let settings = gtk::MenuItem::with_label("设置");
    let quit_item = gtk::MenuItem::with_label("退出");
    menu.append(&ui.tray_copy);
    menu.append(&gtk::SeparatorMenuItem::new());
    menu.append(&show);
    menu.append(&settings);
    menu.append(&quit_item);
    let s = state.clone();
    ui.tray_toggle.connect_activate(move |_| toggle(&s, false));
    let s = state.clone();
    ui.tray_cancel
        .connect_activate(move |_| cancel(&s, "已取消，已确认文字保留"));
    let s = state.clone();
    ui.tray_copy.connect_activate(move |_| copy(&s));
    let w = ui.window.clone();
    show.connect_activate(move |_| {
        w.show_all();
        w.present();
    });
    let w = ui.settings.clone();
    settings.connect_activate(move |_| {
        w.show_all();
        w.present();
    });
    let s = state.clone();
    quit_item.connect_activate(move |_| quit(&s));
    menu.show_all();
    menu
}

fn show_without_focus(window: &gtk::ApplicationWindow) {
    window.set_focus_on_map(false);
    window.show_all();
    window.set_focus_on_map(true);
}

fn refresh(s: &State) {
    s.ui.status.set_text(&s.status);
    s.ui.model.set_text(&format!("服务器模型：{}", s.model));
    let label = match s.phase {
        Phase::Recording => "停止",
        Phase::Connecting => "取消连接",
        Phase::Finishing => "正在收尾",
        _ => "开始",
    };
    s.ui.toggle.set_label(label);
    s.ui.toggle.set_tooltip_text(Some(label));
    s.ui.toggle
        .set_sensitive(!s.quitting && s.phase != Phase::Finishing);
    let icon = if s.phase == Phase::Recording {
        "media-playback-stop-symbolic"
    } else {
        "media-record-symbolic"
    };
    s.ui.toggle.set_image(Some(&gtk::Image::from_icon_name(
        Some(icon),
        gtk::IconSize::Button,
    )));
    s.ui.cancel.set_sensitive(s.phase.active() && !s.quitting);
    let copy_label = if s.phase.active() {
        "复制并暂停自动输入"
    } else {
        "复制"
    };
    s.ui.copy.set_label(copy_label);
    s.ui.copy.set_tooltip_text(Some(copy_label));
    s.ui.copy.set_sensitive(!s.fixed.is_empty() && !s.quitting);
    s.ui.tray_copy.set_label(copy_label);
    s.ui.tray_copy.set_sensitive(s.ui.copy.is_sensitive());
    s.ui.tray_status.set_label(&s.status);
    s.ui.tray_toggle.set_label(label);
    s.ui.tray_toggle.set_sensitive(s.ui.toggle.is_sensitive());
    s.ui.tray_cancel.set_sensitive(s.ui.cancel.is_sensitive());
    for button in [&s.ui.save, &s.ui.check, &s.ui.hotkey] {
        button.set_sensitive(s.loaded && !s.settings_busy && !s.phase.active() && !s.quitting);
    }
    let editable = s.loaded && !s.settings_busy && !s.phase.active() && !s.quitting;
    for field in [
        s.ui.server.clone().upcast::<gtk::Widget>(),
        s.ui.token.clone().upcast(),
        s.ui.microphone.clone().upcast(),
        s.ui.shortcut.clone().upcast(),
        s.ui.auto_paste.clone().upcast(),
        s.ui.show_preview.clone().upcast(),
        s.ui.autostart.clone().upcast(),
        s.ui.pause.clone().upcast(),
    ] {
        field.set_sensitive(editable);
    }
    s.ui.elapsed.set_text(&format!(
        "{:02}:{:02}",
        s.seconds as u64 / 60,
        s.seconds as u64 % 60
    ));
    if let Some(tray) = &s.tray {
        tray.set_recording(s.phase == Phase::Recording);
    }
    if s.config.show_preview
        && matches!(s.phase, Phase::Recording | Phase::Finishing)
        && (!s.fixed.is_empty() || !s.pending.is_empty())
    {
        s.ui.preview_fixed.set_text(&last_chars(&s.fixed, 240));
        s.ui.preview_pending.set_text(&s.pending);
        if !s.ui.preview.is_visible() {
            if let Some(monitor) = gtk::gdk::Display::default().and_then(|d| d.primary_monitor()) {
                let r = monitor.workarea();
                s.ui.preview
                    .move_(r.x() + (r.width() - 636).max(0), r.y() + 24);
            }
            s.ui.preview.show_all();
        }
    } else {
        s.ui.preview.hide();
    }
}

fn last_chars(text: &str, count: usize) -> String {
    text.chars()
        .skip(text.chars().count().saturating_sub(count))
        .collect()
}

fn fill_sources(ui: &Widgets, sources: &[audio::Source], selected: &Option<String>) {
    ui.microphone.remove_all();
    ui.microphone.append(Some("default"), "系统默认麦克风");
    for source in sources {
        let label = format!(
            "{}{}",
            source.description,
            if source.muted { "（静音）" } else { "" }
        );
        ui.microphone
            .append(Some(&format!("source:{}", source.name)), &label);
    }
    if let Some(selected) = selected {
        if !sources.iter().any(|s| &s.name == selected) {
            ui.microphone.append(
                Some(&format!("source:{selected}")),
                &format!("{selected}（不可用）"),
            );
        }
        ui.microphone
            .set_active_id(Some(&format!("source:{selected}")));
    } else {
        ui.microphone.set_active_id(Some("default"));
    }
}

fn populate(ui: &Widgets, config: &settings::Config) {
    ui.server.set_text(&config.server_url);
    ui.shortcut.set_text(&config.shortcut);
    ui.auto_paste.set_active(config.auto_paste);
    ui.show_preview.set_active(config.show_preview);
    ui.autostart.set_active(config.autostart);
    ui.pause.set_value(config.pause_ms as f64);
}

fn credential_label(
    configured: bool,
    persistence: Option<credentials::Persistence>,
) -> &'static str {
    if !configured {
        return "凭据：未配置";
    }
    match persistence {
        Some(credentials::Persistence::Keyring) => "凭据：已保存到系统密钥环",
        Some(credentials::Persistence::MemoryOnly) => "凭据：仅本次运行保存",
        None => "凭据：保存状态未知",
    }
}

fn load_settings(state: &Shared) {
    let s = state.borrow();
    let tx = s.tx.clone();
    let store = s.credentials.clone();
    thread::spawn(move || {
        let mut warning = None;
        let config = match settings::Config::load() {
            Ok(c) => c,
            Err(_) => {
                warning = Some("设置无法读取，请检查并重新保存".into());
                settings::Config::default()
            }
        };
        let (token, persistence) = match protocol::Endpoint::parse(&config.server_url) {
            Ok(endpoint) => {
                let mut store = store.lock().unwrap();
                match store.read(&endpoint) {
                    Ok(token) => {
                        let persistence = store.persistence(&endpoint);
                        (token, persistence)
                    }
                    Err(_) => {
                        warning = Some("系统密钥环不可用，请重新输入本机设备凭据".into());
                        (None, None)
                    }
                }
            }
            Err(_) => (None, None),
        };
        let sources = audio::sources().unwrap_or_default();
        let _ = tx.send(Event::Loaded {
            config,
            token,
            persistence,
            sources,
            warning,
        });
    });
}

fn toggle(state: &Shared, hotkey: bool) {
    let phase = state.borrow().phase;
    match phase {
        Phase::Recording => {
            let mut s = state.borrow_mut();
            s.phase = Phase::Finishing;
            s.status = "正在收尾".into();
            if let Some(capture) = &s.capture {
                capture.stop();
            } else if let Some(session) = &s.session {
                let _ = session.finish();
            }
            refresh(&s);
        }
        Phase::Connecting => cancel(state, "已取消连接"),
        Phase::Finishing => {}
        Phase::Idle | Phase::Error => begin(state, hotkey),
    }
}

fn begin(state: &Shared, hotkey: bool) {
    let mut s = state.borrow_mut();
    if s.quitting || s.settings_busy {
        return;
    }
    s.generation += 1;
    s.cancelled = Arc::new(AtomicBool::new(false));
    s.phase = Phase::Connecting;
    s.status = "正在检查服务器".into();
    s.terminal = false;
    s.capture_started = false;
    s.target_ready = !hotkey;
    s.target = None;
    s.fixed.clear();
    s.pending.clear();
    s.seconds = 0.0;
    s.buffered_seconds = 0.0;
    s.sent_samples = 0;
    s.delivery_state = if hotkey && s.config.auto_paste {
        "waiting_target"
    } else {
        "clipboard_only"
    }
    .into();
    s.ui.fixed.set_text("");
    s.ui.pending.set_text("");
    s.ui.level.set_value(0.0);
    s.ui.delivery.set_text(if hotkey && s.config.auto_paste {
        "等待核对目标窗口"
    } else {
        "仅复制"
    });
    let generation = s.generation;
    let tx = s.tx.clone();
    refresh(&s);
    drop(s);
    if hotkey {
        thread::spawn(move || {
            let (target, warning) = match desktop::current_target() {
                Ok(t) if t.pid != std::process::id().to_string() => (Some(t), None),
                Ok(_) => (None, Some("客户端窗口为当前目标，本轮仅复制".into())),
                Err(_) => (None, Some("无法核对输入目标，本轮仅复制".into())),
            };
            let _ = tx.send(Event::Target {
                generation,
                target,
                warning,
            });
        });
    }
    maybe_start(state);
}

fn maybe_start(state: &Shared) {
    let mut s = state.borrow_mut();
    if !s.loaded
        || !s.target_ready
        || s.phase != Phase::Connecting
        || s.session.is_some()
        || s.cancelled.load(Ordering::Acquire)
    {
        return;
    }
    let Some(token) = s.token.clone() else {
        s.phase = Phase::Error;
        s.status = "请在设置中填写本机设备凭据".into();
        refresh(&s);
        if s.tray.is_none() {
            show_without_focus(&s.ui.window);
        }
        return;
    };
    let endpoint = match protocol::Endpoint::parse(&s.config.server_url) {
        Ok(e) => e,
        Err(e) => {
            s.phase = Phase::Error;
            s.status = e.to_string();
            refresh(&s);
            return;
        }
    };
    // target_ready is also the one-shot latch while capability checking is in flight.
    s.target_ready = false;
    let generation = s.generation;
    let tx = s.tx.clone();
    let cancelled = s.cancelled.clone();
    let _ = s.delivery_tx.send(DeliveryJob::Start {
        generation,
        target: s.target.clone(),
        auto_paste: s.config.auto_paste,
    });
    drop(s);
    thread::spawn(move || {
        let mut network_started = false;
        let result: Result<(), protocol::SessionError> = (|| {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| protocol::SessionError::ConnectionFailed)?;
            rt.block_on(async {
                let caps = transport::capabilities(&endpoint, &token).await?;
                caps.ensure_can_start()?;
                if cancelled.load(Ordering::Acquire) {
                    return Ok(());
                }
                let (session, input) = transport::session_channel(&caps)?;
                if cancelled.load(Ordering::Acquire) {
                    session.cancel();
                    return Ok(());
                }
                let _ = tx.send(Event::Prepared {
                    generation,
                    session: session.clone(),
                    caps: caps.clone(),
                });
                network_started = true;
                let (session_tx, session_rx) = mpsc::channel();
                let forward = tx.clone();
                thread::spawn(move || {
                    for event in session_rx {
                        let _ = forward.send(Event::Network { generation, event });
                    }
                });
                transport::run_session(endpoint, token, caps, input, session_tx)
                    .await
                    .map(|_| ())
            })
        })();
        if let Err(error) = result {
            if !network_started && !cancelled.load(Ordering::Acquire) {
                let _ = tx.send(Event::StartFailed {
                    generation,
                    message: error.to_string(),
                });
            }
        }
    });
}

fn cancel(state: &Shared, message: &str) {
    let mut s = state.borrow_mut();
    if !s.phase.active() {
        return;
    }
    s.cancelled.store(true, Ordering::Release);
    if let Some(session) = s.session.take() {
        session.cancel();
    }
    if let Some(capture) = s.capture.take() {
        capture.cancel();
    }
    s.generation += 1;
    s.phase = Phase::Idle;
    s.terminal = true;
    s.pending.clear();
    s.ui.pending.set_text("");
    s.ui.level.set_value(0.0);
    s.status = message.into();
    refresh(&s);
}

fn fail(state: &Shared, message: String) {
    let mut s = state.borrow_mut();
    if s.terminal {
        return;
    }
    s.cancelled.store(true, Ordering::Release);
    if let Some(session) = s.session.take() {
        session.cancel();
    }
    if let Some(capture) = s.capture.take() {
        capture.cancel();
    }
    s.generation += 1;
    s.phase = Phase::Error;
    s.terminal = true;
    s.pending.clear();
    s.ui.pending.set_text("");
    s.ui.level.set_value(0.0);
    s.status = message;
    refresh(&s);
}

fn receive_text(s: &mut State, update: protocol::TextUpdate, final_text: bool) {
    let enqueue = should_deliver(&s.fixed, &update.fixed, final_text);
    s.fixed = update.fixed;
    s.pending = update.pending;
    s.ui.fixed.set_text(&s.fixed);
    s.ui.pending.set_text(&s.pending);
    if enqueue {
        if let Some(session) = &s.session {
            let _ = s.delivery_tx.send(DeliveryJob::Text {
                generation: s.generation,
                fixed: s.fixed.clone(),
                gate: session.delivery_gate(),
                final_text,
            });
        }
    }
}

fn process_event(state: &Shared, event: Event) {
    match event {
        Event::Loaded {
            config,
            token,
            persistence,
            sources,
            warning,
        } => {
            {
                let mut s = state.borrow_mut();
                s.config = config;
                s.token = token;
                s.persistence = persistence;
                s.loaded = true;
                populate(&s.ui, &s.config);
                fill_sources(&s.ui, &sources, &s.config.microphone);
                s.ui.credential
                    .set_text(credential_label(s.token.is_some(), s.persistence));
                if s.phase == Phase::Idle {
                    s.status = warning.clone().unwrap_or_else(|| {
                        if s.token.is_some() {
                            "可测试连接".into()
                        } else {
                            "请先配置设备凭据".into()
                        }
                    });
                }
                if let Some(w) = warning {
                    s.ui.settings_status.set_text(&w);
                }
                refresh(&s);
            }
            maybe_start(state);
        }
        Event::Target {
            generation,
            target,
            warning,
        } => {
            {
                let mut s = state.borrow_mut();
                if generation != s.generation || s.phase != Phase::Connecting {
                    return;
                }
                s.target = target;
                s.target_ready = true;
                if let Some(w) = warning {
                    s.delivery_state = "clipboard_only".into();
                    s.ui.delivery.set_text(&w);
                }
                if s.tray.is_none() {
                    show_without_focus(&s.ui.window);
                }
            }
            maybe_start(state);
        }
        Event::Prepared {
            generation,
            session,
            caps,
        } => {
            let mut s = state.borrow_mut();
            if generation != s.generation
                || s.phase != Phase::Connecting
                || s.cancelled.load(Ordering::Acquire)
            {
                session.cancel();
                return;
            }
            s.session = Some(session);
            s.model = caps
                .model_id
                .unwrap_or_else(|| caps.mode.unwrap_or_else(|| "未知模型".into()));
            s.status = "正在建立听写会话".into();
            refresh(&s);
        }
        Event::Network { generation, event } => {
            if generation != state.borrow().generation || state.borrow().terminal {
                return;
            }
            match event {
                protocol::SessionEvent::Ready(_) => {
                    let mut s = state.borrow_mut();
                    let Some(session) = s.session.clone() else {
                        return;
                    };
                    s.status = "正在打开麦克风".into();
                    let tx = s.tx.clone();
                    let target = s.target.clone();
                    let delivery = s.delivery_tx.clone();
                    let config = audio::CaptureConfig {
                        source: s.config.microphone.clone(),
                        pause_ms: s.config.pause_ms,
                        ..audio::CaptureConfig::default()
                    };
                    refresh(&s);
                    drop(s);
                    thread::spawn(move || {
                        if target.as_ref().is_some_and(|target| {
                            desktop::current_target()
                                .map(|current| &current != target)
                                .unwrap_or(true)
                        }) {
                            let _ = delivery.send(DeliveryJob::OnlyCopy { generation });
                            let _ = tx.send(Event::CopyOnly { generation });
                        }
                        if !session.delivery_gate().is_open() {
                            return;
                        }
                        let (capture_tx, capture_rx) = mpsc::channel();
                        let forward = tx.clone();
                        thread::spawn(move || {
                            for event in capture_rx {
                                let _ = forward.send(Event::Capture { generation, event });
                            }
                        });
                        let capture = audio::CaptureHandle::start(config, session, capture_tx)
                            .map_err(|e| e.to_string());
                        let _ = tx.send(Event::CaptureReady {
                            generation,
                            capture,
                        });
                    });
                }
                protocol::SessionEvent::Progress(p) => {
                    let mut s = state.borrow_mut();
                    s.sent_samples = p.sent_samples;
                    s.buffered_seconds = p.buffered_samples as f64 / 16000.0;
                }
                protocol::SessionEvent::Text(update) => {
                    let mut s = state.borrow_mut();
                    receive_text(&mut s, update, false);
                    refresh(&s);
                }
                protocol::SessionEvent::Finished {
                    update,
                    complete,
                    reason: _,
                } => {
                    if !complete {
                        {
                            let mut s = state.borrow_mut();
                            s.fixed = update.fixed;
                            s.ui.fixed.set_text(&s.fixed);
                        }
                        fail(state, "本轮未完整完成，已确认文字保留".into());
                        return;
                    }
                    let mut s = state.borrow_mut();
                    if let Some(capture) = &s.capture {
                        capture.stop();
                    }
                    s.phase = Phase::Finishing;
                    s.status = "正在交付最后文字".into();
                    receive_text(&mut s, update, true);
                    s.terminal = true;
                    refresh(&s);
                }
                protocol::SessionEvent::Cancelled => cancel(state, "已取消，已确认文字保留"),
                protocol::SessionEvent::Failed(e) => fail(state, e.to_string()),
            }
        }
        Event::CopyOnly { generation } => {
            let mut s = state.borrow_mut();
            if generation == s.generation {
                s.delivery_state = "clipboard_only".into();
                s.ui.delivery.set_text("目标已变化，本轮仅复制");
            }
        }
        Event::CaptureReady {
            generation,
            capture,
        } => {
            if generation != state.borrow().generation
                || state.borrow().terminal
                || state.borrow().cancelled.load(Ordering::Acquire)
            {
                if let Ok(capture) = capture {
                    capture.cancel();
                }
                return;
            }
            match capture {
                Ok(capture) => {
                    let mut s = state.borrow_mut();
                    s.capture = Some(capture);
                    if s.capture_started && s.capture.as_ref().is_some_and(|c| c.is_active()) {
                        s.phase = Phase::Recording;
                        s.status = "录音中".into();
                    }
                    refresh(&s);
                }
                Err(e) => fail(state, e),
            }
        }
        Event::Capture { generation, event } => {
            if generation != state.borrow().generation {
                return;
            }
            match event {
                audio::CaptureEvent::Started => {
                    let mut s = state.borrow_mut();
                    s.capture_started = true;
                    if s.capture.as_ref().is_some_and(|c| c.is_active()) && !s.terminal {
                        s.phase = Phase::Recording;
                        s.status = "录音中".into();
                        refresh(&s);
                    }
                }
                audio::CaptureEvent::Level {
                    rms_dbfs,
                    seconds,
                    buffered_seconds,
                    ..
                } => {
                    let mut s = state.borrow_mut();
                    s.seconds = seconds;
                    s.buffered_seconds = buffered_seconds;
                    s.ui.level
                        .set_value(((rms_dbfs + 60.0) / 60.0).clamp(0.0, 1.0));
                    s.ui.elapsed.set_text(&format!(
                        "{:02}:{:02}",
                        seconds as u64 / 60,
                        seconds as u64 % 60
                    ));
                }
                audio::CaptureEvent::Stopped => {
                    let mut s = state.borrow_mut();
                    s.capture_started = false;
                    s.capture.take();
                    s.ui.level.set_value(0.0);
                    if s.phase == Phase::Recording && !s.terminal {
                        s.phase = Phase::Finishing;
                        s.status = "正在收尾".into();
                        refresh(&s);
                    }
                }
                audio::CaptureEvent::Failed(e) => {
                    if !state.borrow().terminal {
                        fail(state, e.to_string());
                    }
                }
            }
        }
        Event::StartFailed {
            generation,
            message,
        } => {
            if generation == state.borrow().generation && !state.borrow().terminal {
                fail(state, message);
            }
        }
        Event::Delivered {
            generation,
            result,
            final_text,
        } => {
            let mut s = state.borrow_mut();
            if generation != s.generation {
                return;
            }
            let (label, delivery_state) = match result {
                Ok(desktop::DeliveryOutcome::Pasted) => ("已输入", "pasted"),
                Ok(desktop::DeliveryOutcome::FocusChanged) => {
                    ("目标已变化，本轮仅复制", "focus_changed")
                }
                Ok(desktop::DeliveryOutcome::Copied) => ("已复制", "copied"),
                Ok(desktop::DeliveryOutcome::Empty) => ("", "empty"),
                Ok(desktop::DeliveryOutcome::Cancelled) => ("已停止交付", "cancelled"),
                Err(_) => ("自动输入失败，文字保留，可手动复制", "failed"),
            };
            if !label.is_empty() {
                s.delivery_state = delivery_state.into();
            }
            if !label.is_empty() {
                s.ui.delivery.set_text(label);
            }
            if final_text {
                s.session.take();
                if let Some(capture) = s.capture.take() {
                    capture.cancel();
                }
                s.phase = Phase::Idle;
                s.pending.clear();
                s.ui.pending.set_text("");
                s.status = "已完成本轮听写".into();
                refresh(&s);
            }
        }
        Event::Saved {
            request,
            config,
            token,
            persistence,
            warning,
        } => {
            let mut s = state.borrow_mut();
            if request != s.request {
                return;
            }
            s.config = config;
            s.token = token;
            s.persistence = persistence;
            s.settings_busy = false;
            s.ui.token.set_text("");
            s.ui.credential
                .set_text(credential_label(s.token.is_some(), s.persistence));
            s.ui.settings_status
                .set_text(warning.as_deref().unwrap_or("设置已保存"));
            s.status = if s.token.is_some() {
                "可测试连接".into()
            } else {
                "请先配置设备凭据".into()
            };
            refresh(&s);
        }
        Event::SettingsFailed { request, message } => {
            let mut s = state.borrow_mut();
            if request == s.request {
                s.settings_busy = false;
                s.ui.settings_status.set_text(&message);
                refresh(&s);
            }
        }
        Event::Checked { request, result } => {
            let mut s = state.borrow_mut();
            if request != s.request {
                return;
            }
            s.settings_busy = false;
            match result {
                Ok(caps) => {
                    s.model = caps
                        .model_id
                        .clone()
                        .unwrap_or_else(|| caps.mode.clone().unwrap_or_else(|| "未知模型".into()));
                    let text = caps
                        .ensure_can_start()
                        .map(|_| "连接正常，可开始听写".to_string())
                        .unwrap_or_else(|e| e.to_string());
                    s.ui.settings_status.set_text(&text);
                    s.status = text;
                }
                Err(e) => {
                    s.ui.settings_status.set_text(&e);
                    s.status = e;
                }
            }
            refresh(&s);
        }
        Event::Hotkey { request, result } => {
            let mut s = state.borrow_mut();
            if request != s.request {
                return;
            }
            s.settings_busy = false;
            s.ui.settings_status.set_text(
                &result
                    .map(|_| "快捷键已应用，请保存设置".to_string())
                    .unwrap_or_else(|e| e),
            );
            refresh(&s);
        }
        Event::Sources(result) => {
            let s = state.borrow();
            let selected =
                s.ui.microphone
                    .active_id()
                    .and_then(|id| id.strip_prefix("source:").map(str::to_owned));
            match result {
                Ok(sources) => fill_sources(&s.ui, &sources, &selected),
                Err(e) => s.ui.settings_status.set_text(&e),
            }
        }
        Event::Copied { generation, result } => {
            let mut s = state.borrow_mut();
            if generation != s.generation {
                return;
            }
            s.delivery_state = if result.is_ok() { "copied" } else { "failed" }.into();
            s.ui.delivery
                .set_text(if result.is_ok() && s.phase.active() {
                    "已复制，本轮仅复制"
                } else if result.is_ok() {
                    "已复制"
                } else {
                    "复制失败，文字仍保留"
                });
        }
        Event::QuitReady => state.borrow().app.quit(),
    }
}

fn delivery_worker(events: mpsc::Sender<Event>) -> DeliverySender {
    delivery_worker_with(events, desktop::deliver, desktop::copy_text)
}

fn delivery_worker_with(
    events: mpsc::Sender<Event>,
    deliver: impl Fn(
            &str,
            &desktop::Target,
            bool,
            &str,
            &transport::DeliveryGate,
        ) -> Result<desktop::DeliveryOutcome, desktop::DesktopError>
        + Send
        + 'static,
    copy_text: impl Fn(&str) -> Result<(), desktop::DesktopError> + Send + 'static,
) -> DeliverySender {
    let (tx, rx) = mpsc::sync_channel(1);
    let queue = Arc::new(Mutex::new(VecDeque::new()));
    let sender = DeliverySender {
        queue: queue.clone(),
        wake: tx,
    };
    thread::spawn(move || {
        let mut generation = 0;
        let mut target = None;
        let mut plan = DeliveryPlan::default();
        while rx.recv().is_ok() {
            loop {
                let Some(job) = queue.lock().unwrap().pop_front() else {
                    break;
                };
                match job {
                    DeliveryJob::Start {
                        generation: next,
                        target: next_target,
                        auto_paste,
                    } => {
                        generation = next;
                        target = next_target;
                        plan = DeliveryPlan {
                            clipboard_only: !auto_paste || target.is_none(),
                            ..DeliveryPlan::default()
                        };
                    }
                    DeliveryJob::OnlyCopy { generation: next } => {
                        if next == generation {
                            plan.clipboard_only = true;
                        }
                    }
                    DeliveryJob::ManualCopy {
                        generation: next,
                        fixed,
                    } => {
                        // Explicit copying takes over this session's clipboard delivery.
                        if next == generation {
                            plan.manual_copy();
                        }
                        let _ = events.send(Event::Copied {
                            generation: next,
                            result: copy_text(&fixed).map_err(|e| e.to_string()),
                        });
                    }
                    DeliveryJob::Text {
                        generation: next,
                        fixed,
                        gate,
                        final_text,
                    } => {
                        if next != generation || !gate.is_open() {
                            if final_text {
                                gate.close();
                                let _ = events.send(Event::Delivered {
                                    generation: next,
                                    result: Ok(desktop::DeliveryOutcome::Cancelled),
                                    final_text,
                                });
                            }
                            continue;
                        }
                        let copy_target = desktop::Target {
                            window: String::new(),
                            focus: String::new(),
                            wm_class: String::new(),
                            pid: String::new(),
                        };
                        let mut pasted_next = None;
                        let mut result = match plan.plan(&fixed) {
                            Ok(TextPlan::Skip) => Ok(desktop::DeliveryOutcome::Empty),
                            Ok(TextPlan::Copy) => deliver(&fixed, &copy_target, true, "", &gate)
                                .map_err(|e| e.to_string()),
                            Ok(TextPlan::Paste { body, prefix, next }) => {
                                pasted_next = Some(next);
                                deliver(
                                    &body,
                                    target.as_ref().unwrap_or(&copy_target),
                                    false,
                                    prefix,
                                    &gate,
                                )
                                .map_err(|e| e.to_string())
                            }
                            Err(()) => {
                                gate.close();
                                Err("固定文字前缀不一致，已停止自动输入".into())
                            }
                        };
                        match &result {
                            Ok(outcome) => {
                                plan.complete(*outcome, pasted_next);
                                if *outcome == desktop::DeliveryOutcome::FocusChanged {
                                    if let Err(e) = deliver(&fixed, &copy_target, true, "", &gate) {
                                        result = Err(e.to_string());
                                    }
                                }
                            }
                            Err(_) => {
                                plan.clipboard_only = true;
                                let _ = deliver(&fixed, &copy_target, true, "", &gate);
                            }
                        }
                        if final_text {
                            gate.close();
                        }
                        let _ = events.send(Event::Delivered {
                            generation,
                            result,
                            final_text,
                        });
                    }
                    DeliveryJob::Quit => return,
                }
            }
        }
    });
    sender
}

fn copy(state: &Shared) {
    let mut s = state.borrow_mut();
    if s.fixed.is_empty() || s.quitting {
        return;
    }
    if s.delivery_tx
        .send(DeliveryJob::ManualCopy {
            generation: s.generation,
            fixed: s.fixed.clone(),
        })
        .is_err()
    {
        s.ui.delivery.set_text("复制失败，文字仍保留");
        return;
    }
    if s.phase.active() {
        s.delivery_state = "clipboard_only".into();
        s.ui.delivery.set_text("正在复制，本轮仅复制");
    } else {
        s.ui.delivery.set_text("正在复制");
    }
}

fn form_config(s: &State) -> Result<settings::Config, String> {
    let config = settings::Config {
        server_url: s.ui.server.text().trim().to_owned(),
        microphone: s
            .ui
            .microphone
            .active_id()
            .and_then(|id| id.strip_prefix("source:").map(str::to_owned)),
        shortcut: s.ui.shortcut.text().trim().to_owned(),
        auto_paste: s.ui.auto_paste.is_active(),
        show_preview: s.ui.show_preview.is_active(),
        autostart: s.ui.autostart.is_active(),
        pause_ms: s.ui.pause.value_as_int() as u64,
    };
    config
        .validate()
        .map_err(|_| "请检查服务器地址、快捷键与录音设置".to_string())?;
    Ok(config)
}

fn start_settings_request(s: &mut State) -> u64 {
    s.request += 1;
    s.settings_busy = true;
    refresh(s);
    s.request
}

fn save_settings(state: &Shared) {
    let mut s = state.borrow_mut();
    if s.settings_busy || s.phase.active() {
        return;
    }
    let config = match form_config(&s) {
        Ok(c) => c,
        Err(e) => {
            s.ui.settings_status.set_text(&e);
            return;
        }
    };
    let endpoint = protocol::Endpoint::parse(&config.server_url).unwrap();
    let secret = zeroize::Zeroizing::new(s.ui.token.text().trim().to_owned());
    let token = if secret.is_empty() {
        None
    } else {
        match protocol::DeviceToken::new(secret.to_string()) {
            Ok(t) => Some(t),
            Err(_) => {
                s.ui.settings_status.set_text("设备凭据格式无效");
                return;
            }
        }
    };
    s.ui.token.set_text("");
    let request = start_settings_request(&mut s);
    let tx = s.tx.clone();
    let store = s.credentials.clone();
    drop(s);
    thread::spawn(move || {
        let result: Result<_, String> = (|| {
            config
                .save()
                .map_err(|_| "设置保存失败，请检查配置目录权限".to_string())?;
            let mut warning = None;
            let (token, persistence) = {
                let mut store = store.lock().unwrap();
                if let Some(token) = token {
                    if store
                        .store(&endpoint, token.clone())
                        .map_err(|_| "设备凭据保存失败".to_string())?
                        == credentials::Persistence::MemoryOnly
                    {
                        warning = Some("系统密钥环不可用，凭据仅保存在本次运行内存".into());
                    }
                    (Some(token), store.persistence(&endpoint))
                } else {
                    match store.read(&endpoint) {
                        Ok(t) => (t, store.persistence(&endpoint)),
                        Err(_) => {
                            warning = Some("系统密钥环不可用，请重新输入设备凭据".into());
                            (None, None)
                        }
                    }
                }
            };
            if configure_autostart(config.autostart).is_err() {
                warning = Some(match warning {
                    Some(w) => format!("{w}；登录启动设置失败"),
                    None => "设置已保存，登录启动设置失败".into(),
                });
            }
            Ok((token, persistence, warning))
        })();
        let event = match result {
            Ok((token, persistence, warning)) => Event::Saved {
                request,
                config,
                token,
                persistence,
                warning,
            },
            Err(message) => Event::SettingsFailed { request, message },
        };
        let _ = tx.send(event);
    });
}

fn check_connection(state: &Shared) {
    let mut s = state.borrow_mut();
    if s.settings_busy || s.phase.active() {
        return;
    }
    let endpoint = match protocol::Endpoint::parse(s.ui.server.text().trim()) {
        Ok(e) => e,
        Err(e) => {
            s.ui.settings_status.set_text(&e.to_string());
            return;
        }
    };
    let secret = zeroize::Zeroizing::new(s.ui.token.text().trim().to_owned());
    let candidate = if secret.is_empty() {
        None
    } else {
        match protocol::DeviceToken::new(secret.to_string()) {
            Ok(t) => Some(t),
            Err(e) => {
                s.ui.settings_status.set_text(&e.to_string());
                return;
            }
        }
    };
    let request = start_settings_request(&mut s);
    s.ui.settings_status.set_text("正在检查连接");
    let tx = s.tx.clone();
    let store = s.credentials.clone();
    drop(s);
    thread::spawn(move || {
        let result: Result<_, String> = (|| {
            let token = match candidate {
                Some(t) => t,
                None => store
                    .lock()
                    .unwrap()
                    .read(&endpoint)
                    .map_err(|_| "系统密钥环不可用，请输入设备凭据".to_string())?
                    .ok_or_else(|| "该服务器尚未配置本机设备凭据".to_string())?,
            };
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .map_err(|_| "无法启动网络任务".to_string())?;
            rt.block_on(transport::capabilities(&endpoint, &token))
                .map_err(|e| e.to_string())
        })();
        let _ = tx.send(Event::Checked { request, result });
    });
}

fn install_hotkey(state: &Shared) {
    let mut s = state.borrow_mut();
    if s.settings_busy || s.phase.active() {
        return;
    }
    let shortcut = s.ui.shortcut.text().trim().to_string();
    if shortcut.is_empty() {
        s.ui.settings_status.set_text("请填写快捷键");
        return;
    }
    let request = start_settings_request(&mut s);
    let tx = s.tx.clone();
    drop(s);
    thread::spawn(move || {
        let result = std::env::current_exe()
            .map_err(|_| "无法找到客户端程序".into())
            .and_then(|exe| {
                desktop::install_hotkey(&shortcut, &exe)
                    .map(|_| ())
                    .map_err(|e| e.to_string())
            });
        let _ = tx.send(Event::Hotkey { request, result });
    });
}

fn configure_autostart(enabled: bool) -> Result<(), std::io::Error> {
    let root = settings::Config::path().map_err(std::io::Error::other)?;
    let root = root
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| std::io::Error::other("Configuration directory unavailable"))?;
    let dir = root.join("autostart");
    let file = dir.join("oneaxe-voice-linux.desktop");
    if let Ok(metadata) = std::fs::symlink_metadata(&file) {
        if !metadata.is_file()
            || metadata.file_type().is_symlink()
            || !std::fs::read_to_string(&file)?.contains("X-OneAxe-Voice-Linux=true")
        {
            return Err(std::io::Error::other(
                "Autostart entry is not owned by this application",
            ));
        }
    }
    if !enabled {
        if file.exists() {
            std::fs::remove_file(file)?;
        }
        return Ok(());
    }
    std::fs::create_dir_all(&dir)?;
    let executable: PathBuf = std::env::current_exe()?;
    let exe = executable.to_string_lossy();
    if exe.chars().any(char::is_control) {
        return Err(std::io::Error::other("Invalid executable path"));
    }
    let exe = exe
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('`', "\\`")
        .replace('$', "\\$")
        .replace('%', "%%");
    let text = format!("[Desktop Entry]\nType=Application\nName=OneAxe Voice Linux\nExec=\"{exe}\" --background\nIcon=oneaxe-voice-linux\nTerminal=false\nX-GNOME-Autostart-enabled=true\nX-OneAxe-Voice-Linux=true\n");
    let mut temporary = tempfile::NamedTempFile::new_in(&dir)?;
    temporary.write_all(text.as_bytes())?;
    temporary.as_file().sync_all()?;
    temporary.persist(file).map_err(|e| e.error)?;
    Ok(())
}

fn quit(state: &Shared) {
    {
        let mut s = state.borrow_mut();
        if s.quitting {
            return;
        }
        s.quitting = true;
        s.cancelled.store(true, Ordering::Release);
        if let Some(session) = s.session.take() {
            session.cancel();
        }
        s.generation += 1;
        s.status = "正在退出".into();
        refresh(&s);
    }
    let mut s = state.borrow_mut();
    let capture = s.capture.take();
    let tx = s.tx.clone();
    let delivery = s.delivery_tx.clone();
    thread::spawn(move || {
        if let Some(capture) = capture {
            capture.cancel();
            let start = Instant::now();
            while capture.is_active() && start.elapsed() < Duration::from_secs(4) {
                thread::sleep(Duration::from_millis(20));
            }
        }
        let _ = delivery.send(DeliveryJob::Quit);
        let _ = tx.send(Event::QuitReady);
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn paste(plan: &mut DeliveryPlan, text: &str) -> (String, &'static str, String) {
        match plan.plan(text).unwrap() {
            TextPlan::Paste { body, prefix, next } => (body, prefix, next),
            _ => panic!("Expected a new fixed-text suffix"),
        }
    }

    #[test]
    fn cumulative_snapshots_deliver_only_the_new_suffix() {
        let mut plan = DeliveryPlan::default();
        let (body, prefix, next) = paste(&mut plan, "hello");
        assert_eq!((body.as_str(), prefix), ("hello", ""));
        plan.complete(desktop::DeliveryOutcome::Pasted, Some(next));
        let (body, prefix, next) = paste(&mut plan, "hello world");
        assert_eq!((body.as_str(), prefix), ("world", " "));
        plan.complete(desktop::DeliveryOutcome::Pasted, Some(next));
        assert!(matches!(plan.plan("hello world"), Ok(TextPlan::Skip)));
    }

    #[test]
    fn a_word_continuation_does_not_insert_a_space() {
        let mut plan = DeliveryPlan::default();
        let (_, _, next) = paste(&mut plan, "hello w");
        plan.complete(desktop::DeliveryOutcome::Pasted, Some(next));
        let (body, prefix, _) = paste(&mut plan, "hello world");
        assert_eq!((body.as_str(), prefix), ("orld", ""));
    }

    #[test]
    fn candidates_do_not_enqueue_and_an_unchanged_final_still_completes() {
        assert!(!should_deliver("固定文字", "固定文字", false));
        assert!(should_deliver("固定文字", "固定文字", true));
        let mut plan = DeliveryPlan::default();
        let (_, _, next) = paste(&mut plan, "固定文字");
        plan.complete(desktop::DeliveryOutcome::Pasted, Some(next));
        assert!(matches!(plan.plan("固定文字"), Ok(TextPlan::Skip)));
    }

    #[test]
    fn target_change_permanently_switches_the_session_to_copying() {
        let mut plan = DeliveryPlan::default();
        let (_, _, next) = paste(&mut plan, "hello");
        plan.complete(desktop::DeliveryOutcome::FocusChanged, Some(next));
        assert!(plan.pasted.is_empty());
        assert!(matches!(plan.plan("hello world"), Ok(TextPlan::Copy)));
        assert!(matches!(plan.plan("hello world again"), Ok(TextPlan::Copy)));
    }

    #[test]
    fn cancelled_delivery_never_advances_the_pasted_prefix() {
        let mut plan = DeliveryPlan::default();
        let (_, _, next) = paste(&mut plan, "hello");
        plan.complete(desktop::DeliveryOutcome::Cancelled, Some(next));
        assert!(plan.pasted.is_empty());
    }

    #[test]
    fn pending_full_snapshots_coalesce_without_losing_the_final_marker() {
        let (wake, _receiver) = mpsc::sync_channel(1);
        let sender = DeliverySender {
            queue: Arc::new(Mutex::new(VecDeque::new())),
            wake,
        };
        let gate = transport::DeliveryGate::new();
        sender
            .send(DeliveryJob::Start {
                generation: 1,
                target: None,
                auto_paste: false,
            })
            .unwrap();
        for n in 1..100 {
            sender
                .send(DeliveryJob::Text {
                    generation: 1,
                    fixed: format!("fixed-{n}"),
                    gate: gate.clone(),
                    final_text: false,
                })
                .unwrap();
        }
        sender
            .send(DeliveryJob::Text {
                generation: 1,
                fixed: "final".into(),
                gate,
                final_text: true,
            })
            .unwrap();
        let queue = sender.queue.lock().unwrap();
        assert_eq!(queue.len(), 2);
        assert!(
            matches!(queue.back(), Some(DeliveryJob::Text { final_text: true, fixed, .. }) if fixed == "final")
        );
    }

    #[test]
    fn a_closed_gate_discards_queued_text_and_acknowledges_final_without_desktop_io() {
        let (tx, rx) = mpsc::channel();
        let sender = delivery_worker(tx);
        let gate = transport::DeliveryGate::new();
        gate.close();
        sender
            .send(DeliveryJob::Start {
                generation: 1,
                target: None,
                auto_paste: false,
            })
            .unwrap();
        sender
            .send(DeliveryJob::Text {
                generation: 1,
                fixed: "not delivered".into(),
                gate,
                final_text: true,
            })
            .unwrap();
        assert!(matches!(
            rx.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Delivered {
                result: Ok(desktop::DeliveryOutcome::Cancelled),
                final_text: true,
                ..
            }
        ));
        sender.send(DeliveryJob::Quit).unwrap();
    }

    #[test]
    fn manual_copy_waits_for_the_inflight_paste_and_remaining_text_copies_the_full_snapshot() {
        let (events, receiver) = mpsc::channel();
        let (entered, started) = mpsc::channel();
        let (release, resume) = mpsc::channel();
        let calls = Arc::new(Mutex::new(Vec::new()));
        let deliveries = calls.clone();
        let copies = calls.clone();
        let sender = delivery_worker_with(
            events,
            move |text, _, clipboard_only, _, gate| {
                assert!(gate.is_open());
                if clipboard_only {
                    deliveries.lock().unwrap().push(format!("full:{text}"));
                    return Ok(desktop::DeliveryOutcome::Copied);
                }
                deliveries.lock().unwrap().push(format!("delta:{text}"));
                entered.send(()).unwrap();
                resume.recv_timeout(Duration::from_secs(2)).unwrap();
                deliveries.lock().unwrap().push(format!("paste:{text}"));
                Ok(desktop::DeliveryOutcome::Pasted)
            },
            move |text| {
                copies.lock().unwrap().push(format!("manual:{text}"));
                Ok(())
            },
        );
        let gate = transport::DeliveryGate::new();
        sender
            .send(DeliveryJob::Start {
                generation: 1,
                target: Some(desktop::Target {
                    window: "100".into(),
                    focus: "101".into(),
                    wm_class: "test".into(),
                    pid: "42".into(),
                }),
                auto_paste: true,
            })
            .unwrap();
        sender
            .send(DeliveryJob::Text {
                generation: 1,
                fixed: "hello".into(),
                gate: gate.clone(),
                final_text: false,
            })
            .unwrap();
        started.recv_timeout(Duration::from_secs(1)).unwrap();
        sender
            .send(DeliveryJob::ManualCopy {
                generation: 1,
                fixed: "hello world".into(),
            })
            .unwrap();
        sender
            .send(DeliveryJob::Text {
                generation: 1,
                fixed: "hello world again".into(),
                gate: gate.clone(),
                final_text: true,
            })
            .unwrap();
        assert_eq!(*calls.lock().unwrap(), ["delta:hello"]);
        release.send(()).unwrap();
        assert!(matches!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Delivered {
                result: Ok(desktop::DeliveryOutcome::Pasted),
                final_text: false,
                ..
            }
        ));
        assert!(matches!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Copied {
                generation: 1,
                result: Ok(()),
            }
        ));
        assert!(matches!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Delivered {
                result: Ok(desktop::DeliveryOutcome::Copied),
                final_text: true,
                ..
            }
        ));
        assert_eq!(
            *calls.lock().unwrap(),
            [
                "delta:hello",
                "paste:hello",
                "manual:hello world",
                "full:hello world again",
            ]
        );
        assert!(!gate.is_open());
        sender.send(DeliveryJob::Quit).unwrap();
    }

    #[test]
    fn cancelling_still_blocks_queued_delivery_but_explicitly_copying_retained_text_is_allowed() {
        let (events, receiver) = mpsc::channel();
        let copies = Arc::new(Mutex::new(Vec::new()));
        let copied = copies.clone();
        let sender = delivery_worker_with(
            events,
            |_, _, _, _, _| panic!("Cancelled automatic delivery must not access the desktop"),
            move |text| {
                copied.lock().unwrap().push(text.to_owned());
                Ok(())
            },
        );
        let gate = transport::DeliveryGate::new();
        gate.close();
        sender
            .send(DeliveryJob::Start {
                generation: 1,
                target: None,
                auto_paste: true,
            })
            .unwrap();
        sender
            .send(DeliveryJob::Text {
                generation: 1,
                fixed: "confirmed".into(),
                gate,
                final_text: true,
            })
            .unwrap();
        sender
            .send(DeliveryJob::ManualCopy {
                generation: 2,
                fixed: "confirmed".into(),
            })
            .unwrap();
        assert!(matches!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Delivered {
                result: Ok(desktop::DeliveryOutcome::Cancelled),
                final_text: true,
                ..
            }
        ));
        assert!(matches!(
            receiver.recv_timeout(Duration::from_secs(1)).unwrap(),
            Event::Copied {
                generation: 2,
                result: Ok(()),
            }
        ));
        assert_eq!(*copies.lock().unwrap(), ["confirmed"]);
        sender.send(DeliveryJob::Quit).unwrap();
    }

    #[test]
    fn credential_status_distinguishes_no_token_from_memory_and_keyring_storage() {
        assert_eq!(
            credential_label(false, Some(credentials::Persistence::MemoryOnly)),
            "凭据：未配置"
        );
        assert_eq!(
            credential_label(true, Some(credentials::Persistence::MemoryOnly)),
            "凭据：仅本次运行保存"
        );
        assert_eq!(
            credential_label(true, Some(credentials::Persistence::Keyring)),
            "凭据：已保存到系统密钥环"
        );
    }
}
