use crate::transport::DeliveryGate;
use gio::prelude::*;
use std::collections::BTreeSet;
use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const HOTKEY_SCHEMA: &str = "org.gnome.settings-daemon.plugins.media-keys";
const CUSTOM_SCHEMA: &str = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding";
const HOTKEY_PATH: &str =
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/oneaxe-voice-linux/";
const HOTKEY_NAME: &str = "OneAxe Voice Linux";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub window: String,
    pub focus: String,
    pub wm_class: String,
    pub pid: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryOutcome {
    Empty,
    Copied,
    FocusChanged,
    Pasted,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DesktopError {
    #[error("缺少桌面命令 {0}")]
    CommandUnavailable(&'static str),
    #[error("桌面命令 {0} 执行失败")]
    CommandFailed(&'static str),
    #[error("桌面命令 {0} 超时")]
    CommandTimeout(&'static str),
    #[error("当前输入窗口不可用或焦点正在变化")]
    TargetUnavailable,
    #[error("粘贴前缀仅允许空字符串或一个空格")]
    InvalidPrefix,
    #[error("快捷键格式无效")]
    InvalidHotkey,
    #[error("快捷键已被其他 GNOME 绑定占用，未修改任何快捷键")]
    HotkeyConflict,
    #[error("快捷键安装位置已被其他配置占用")]
    HotkeyPathInUse,
    #[error("当前系统没有可用的 GNOME 快捷键设置")]
    HotkeySettingsUnavailable,
    #[error("GNOME 快捷键设置不可写")]
    HotkeySettingsReadOnly,
    #[error("无法保存 GNOME 快捷键设置")]
    HotkeyWriteFailed,
    #[error("客户端程序路径无效")]
    InvalidExecutable,
}

trait DesktopCommands {
    fn run(
        &self,
        program: &'static str,
        args: &[&str],
        input: Option<&[u8]>,
    ) -> Result<String, DesktopError>;
}

struct SystemCommands;

impl DesktopCommands for SystemCommands {
    fn run(
        &self,
        program: &'static str,
        args: &[&str],
        input: Option<&[u8]>,
    ) -> Result<String, DesktopError> {
        let mut child = Command::new(program)
            .args(args)
            .stdin(if input.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(
                if input.is_some() || program == "xdotool" && args.first() == Some(&"key") {
                    Stdio::null()
                } else {
                    Stdio::piped()
                },
            )
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| DesktopError::CommandUnavailable(program))?;
        let reader = child.stdout.take().map(|stdout| {
            thread::spawn(move || {
                let mut bytes = Vec::new();
                stdout
                    .take(64 * 1024)
                    .read_to_end(&mut bytes)
                    .map(|_| bytes)
            })
        });
        let writer = if let Some(input) = input {
            let bytes = input.to_vec();
            let mut stdin = child
                .stdin
                .take()
                .ok_or(DesktopError::CommandFailed(program))?;
            Some(thread::spawn(move || stdin.write_all(&bytes)))
        } else {
            None
        };
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            match child.try_wait() {
                Ok(Some(status)) if status.success() => break,
                Ok(Some(_)) => return Err(DesktopError::CommandFailed(program)),
                Err(_) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(DesktopError::CommandFailed(program));
                }
                Ok(None) if Instant::now() >= deadline => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(DesktopError::CommandTimeout(program));
                }
                Ok(None) => thread::sleep(Duration::from_millis(5)),
            }
        }
        if let Some(writer) = writer {
            writer
                .join()
                .map_err(|_| DesktopError::CommandFailed(program))?
                .map_err(|_| DesktopError::CommandFailed(program))?;
        }
        let bytes = match reader {
            Some(reader) => reader
                .join()
                .map_err(|_| DesktopError::CommandFailed(program))?
                .map_err(|_| DesktopError::CommandFailed(program))?,
            None => Vec::new(),
        };
        String::from_utf8(bytes)
            .map(|s| s.trim().to_string())
            .map_err(|_| DesktopError::CommandFailed(program))
    }
}

pub fn current_target() -> Result<Target, DesktopError> {
    query_target(&SystemCommands)
}

fn query_target(commands: &impl DesktopCommands) -> Result<Target, DesktopError> {
    let window = commands.run("xdotool", &["getactivewindow"], None)?;
    let properties = commands.run("xprop", &["-id", &window, "WM_CLASS", "_NET_WM_PID"], None)?;
    let focus = commands.run("xdotool", &["getwindowfocus"], None)?;
    let current_window = commands.run("xdotool", &["getactivewindow"], None)?;
    if current_window != window || !valid_window_id(&window) || !valid_window_id(&focus) {
        return Err(DesktopError::TargetUnavailable);
    }
    let property = |name: &str| {
        properties
            .lines()
            .find(|line| line.starts_with(name))
            .and_then(|line| line.split_once(" = ").map(|(_, value)| value.to_string()))
            .unwrap_or_default()
    };
    let target = Target {
        window,
        focus,
        wm_class: property("WM_CLASS("),
        pid: property("_NET_WM_PID("),
    };
    if target.wm_class.trim().is_empty() || !target.pid.parse::<u64>().is_ok_and(|pid| pid > 0) {
        return Err(DesktopError::TargetUnavailable);
    }
    Ok(target)
}

fn valid_window_id(value: &str) -> bool {
    value.parse::<u64>().is_ok_and(|value| value > 1)
}

pub fn plain_text(value: &str) -> String {
    let collapsed = value.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed
        .chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .trim()
        .to_string()
}

pub fn append_delta(previous: &str, text: &str) -> String {
    let add_space = previous
        .chars()
        .last()
        .zip(text.chars().next())
        .is_some_and(|(last, first)| {
            last.is_ascii() && first.is_ascii_alphanumeric() && !last.is_whitespace()
        });
    if add_space {
        format!(" {text}")
    } else {
        text.to_string()
    }
}

pub fn paste_key(wm_class: &str) -> &'static str {
    let class = wm_class.to_ascii_lowercase();
    if [
        "gnome-terminal",
        "kgx",
        "konsole",
        "kitty",
        "alacritty",
        "terminator",
        "tilix",
        "xfce4-terminal",
        "wezterm",
    ]
    .iter()
    .any(|terminal| class.contains(terminal))
    {
        "ctrl+shift+v"
    } else if [
        "xterm",
        "urxvt",
        "rxvt",
        "\"code\"",
        "\"code-oss\"",
        "\"vscodium\"",
    ]
    .iter()
    .any(|terminal| class.contains(terminal))
    {
        "shift+Insert"
    } else {
        "ctrl+v"
    }
}

pub fn copy_text(value: &str) -> Result<(), DesktopError> {
    SystemCommands
        .run(
            "xclip",
            &["-selection", "clipboard", "-in"],
            Some(value.as_bytes()),
        )
        .map(|_| ())
}

/// The owner must keep FocusChanged sticky for the remainder of the session.
pub fn deliver(
    value: &str,
    target: &Target,
    clipboard_only: bool,
    prefix: &str,
    gate: &DeliveryGate,
) -> Result<DeliveryOutcome, DesktopError> {
    deliver_with(value, target, clipboard_only, prefix, gate, &SystemCommands)
}

fn deliver_with(
    value: &str,
    target: &Target,
    clipboard_only: bool,
    prefix: &str,
    gate: &DeliveryGate,
    commands: &impl DesktopCommands,
) -> Result<DeliveryOutcome, DesktopError> {
    if prefix != "" && prefix != " " {
        return Err(DesktopError::InvalidPrefix);
    }
    if !gate.is_open() {
        return Ok(DeliveryOutcome::Cancelled);
    }
    let text = plain_text(value);
    if text.is_empty() {
        return Ok(DeliveryOutcome::Empty);
    }
    commands.run(
        "xclip",
        &["-selection", "clipboard", "-in"],
        Some(format!("{prefix}{text}").as_bytes()),
    )?;
    if !gate.is_open() {
        return Ok(DeliveryOutcome::Cancelled);
    }
    if clipboard_only {
        return Ok(DeliveryOutcome::Copied);
    }
    if query_target(commands).ok().as_ref() != Some(target) {
        return Ok(if gate.is_open() {
            DeliveryOutcome::FocusChanged
        } else {
            DeliveryOutcome::Cancelled
        });
    }
    // Querying the destination may block; cancellation is checked again immediately before spawning the key command.
    if !gate.is_open() {
        return Ok(DeliveryOutcome::Cancelled);
    }
    commands.run(
        "xdotool",
        &["key", "--clearmodifiers", paste_key(&target.wm_class)],
        None,
    )?;
    Ok(DeliveryOutcome::Pasted)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShortcutConflict {
    pub owner: String,
    pub binding: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotkeyInstalled {
    pub path: String,
    pub binding: String,
    pub command: String,
}

#[derive(Debug, Clone)]
struct ShortcutBinding {
    owner: String,
    binding: String,
}

#[derive(Debug, Clone)]
struct CustomEntry {
    name: String,
    command: String,
    binding: String,
}

fn normalized_accelerator(binding: &str) -> Option<String> {
    if binding.chars().any(char::is_control) {
        return None;
    }
    let mut binding = binding.trim();
    if binding.is_empty() || binding.eq_ignore_ascii_case("disabled") {
        return None;
    }
    let mut modifiers = BTreeSet::new();
    while let Some(rest) = binding.strip_prefix('<') {
        let (modifier, rest) = rest.split_once('>')?;
        let canonical = match modifier.to_ascii_lowercase().as_str() {
            "control" | "ctrl" | "primary" => "Control",
            "alt" | "mod1" => "Alt",
            "super" | "mod4" => "Super",
            "shift" => "Shift",
            "meta" => "Meta",
            "hyper" => "Hyper",
            _ => return None,
        };
        modifiers.insert(canonical);
        binding = rest;
    }
    if binding.is_empty()
        || binding
            .chars()
            .any(|c| c.is_whitespace() || c == '<' || c == '>')
    {
        return None;
    }
    let key = match binding.to_ascii_lowercase().as_str() {
        "enter" | "return" => "Return".to_string(),
        "esc" | "escape" => "Escape".to_string(),
        key if key.starts_with('f') && key[1..].parse::<u8>().is_ok() => {
            format!("F{}", &key[1..])
        }
        _ => binding.to_string(),
    };
    let key = gtk::gdk::keys::Key::from_name(&key);
    if *key == 0 || key == gtk::gdk::keys::constants::VoidSymbol {
        return None;
    }
    let key = key.to_lower().name()?;
    Some(format!(
        "{}{key}",
        modifiers
            .into_iter()
            .map(|m| format!("<{m}>"))
            .collect::<String>()
    ))
}

fn conflicts(
    binding: &str,
    entries: &[ShortcutBinding],
    own_path: Option<&str>,
) -> Result<Vec<ShortcutConflict>, DesktopError> {
    let desired = normalized_accelerator(binding).ok_or(DesktopError::InvalidHotkey)?;
    Ok(entries
        .iter()
        .filter(|entry| {
            Some(entry.owner.as_str()) != own_path
                && normalized_accelerator(&entry.binding).as_ref() == Some(&desired)
        })
        .map(|entry| ShortcutConflict {
            owner: entry.owner.clone(),
            binding: entry.binding.clone(),
        })
        .collect())
}

fn settings_for(schema_id: &str, path: Option<&str>) -> Option<gio::Settings> {
    let source = gio::SettingsSchemaSource::default()?;
    let schema = source.lookup(schema_id, true)?;
    Some(gio::Settings::new_full(
        &schema,
        None::<&gio::SettingsBackend>,
        path,
    ))
}

fn shortcut_entries() -> Result<Vec<ShortcutBinding>, DesktopError> {
    let mut entries = Vec::new();
    for schema_id in [
        HOTKEY_SCHEMA,
        "org.gnome.desktop.wm.keybindings",
        "org.gnome.shell.keybindings",
        "org.gnome.mutter.keybindings",
        "org.gnome.mutter.wayland.keybindings",
    ] {
        if let Some(settings) = settings_for(schema_id, None) {
            let schema = settings
                .settings_schema()
                .ok_or(DesktopError::HotkeySettingsUnavailable)?;
            for key in schema.list_keys() {
                if key == "custom-keybindings" {
                    continue;
                }
                let value = settings.value(&key);
                if let Some(values) = value.get::<Vec<String>>() {
                    for binding in values {
                        entries.push(ShortcutBinding {
                            owner: format!("{schema_id}/{key}"),
                            binding,
                        });
                    }
                } else if let Some(binding) = value.get::<String>() {
                    entries.push(ShortcutBinding {
                        owner: format!("{schema_id}/{key}"),
                        binding,
                    });
                }
            }
        }
    }
    let media = settings_for(HOTKEY_SCHEMA, None).ok_or(DesktopError::HotkeySettingsUnavailable)?;
    for path in media.strv("custom-keybindings") {
        let settings = settings_for(CUSTOM_SCHEMA, Some(&path))
            .ok_or(DesktopError::HotkeySettingsUnavailable)?;
        entries.push(ShortcutBinding {
            owner: path.to_string(),
            binding: settings.string("binding").to_string(),
        });
    }
    Ok(entries)
}

pub fn hotkey_conflicts(binding: &str) -> Result<Vec<ShortcutConflict>, DesktopError> {
    conflicts(binding, &shortcut_entries()?, None)
}

fn validate_install(
    binding: &str,
    command: &str,
    entries: &[ShortcutBinding],
    existing: &CustomEntry,
) -> Result<(), DesktopError> {
    let unused =
        existing.name.is_empty() && existing.command.is_empty() && existing.binding.is_empty();
    let ours = existing.name == HOTKEY_NAME && existing.command == command;
    if !unused && !ours {
        return Err(DesktopError::HotkeyPathInUse);
    }
    if !conflicts(
        binding,
        entries,
        if ours { Some(HOTKEY_PATH) } else { None },
    )?
    .is_empty()
    {
        return Err(DesktopError::HotkeyConflict);
    }
    Ok(())
}

/// Explicit setup command only. Existing custom shortcuts and system bindings are checked before any write.
pub fn install_hotkey(binding: &str, executable: &Path) -> Result<HotkeyInstalled, DesktopError> {
    let binding = normalized_accelerator(binding).ok_or(DesktopError::InvalidHotkey)?;
    if !executable.is_absolute() || !executable.is_file() {
        return Err(DesktopError::InvalidExecutable);
    }
    let executable = executable.to_str().ok_or(DesktopError::InvalidExecutable)?;
    let quoted = glib::shell_quote(executable);
    let command = format!(
        "{} --toggle",
        quoted.to_str().ok_or(DesktopError::InvalidExecutable)?
    );
    let media = settings_for(HOTKEY_SCHEMA, None).ok_or(DesktopError::HotkeySettingsUnavailable)?;
    let custom = settings_for(CUSTOM_SCHEMA, Some(HOTKEY_PATH))
        .ok_or(DesktopError::HotkeySettingsUnavailable)?;
    let existing = CustomEntry {
        name: custom.string("name").to_string(),
        command: custom.string("command").to_string(),
        binding: custom.string("binding").to_string(),
    };
    validate_install(&binding, &command, &shortcut_entries()?, &existing)?;
    if !media.is_writable("custom-keybindings")
        || ["name", "command", "binding"]
            .iter()
            .any(|k| !custom.is_writable(k))
    {
        return Err(DesktopError::HotkeySettingsReadOnly);
    }
    let mut paths = media
        .strv("custom-keybindings")
        .iter()
        .map(|p| p.to_string())
        .collect::<Vec<_>>();
    if !paths.iter().any(|path| path == HOTKEY_PATH) {
        paths.push(HOTKEY_PATH.to_string());
    }
    custom.delay();
    if custom.set_string("name", HOTKEY_NAME).is_err()
        || custom.set_string("command", &command).is_err()
        || custom.set_string("binding", &binding).is_err()
    {
        custom.revert();
        return Err(DesktopError::HotkeyWriteFailed);
    }
    custom.apply();
    let path_refs: Vec<&str> = paths.iter().map(String::as_str).collect();
    media
        .set_strv("custom-keybindings", path_refs.as_slice())
        .map_err(|_| DesktopError::HotkeyWriteFailed)?;
    gio::Settings::sync();
    Ok(HotkeyInstalled {
        path: HOTKEY_PATH.into(),
        binding,
        command,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::VecDeque;

    struct FakeCommands {
        replies: RefCell<VecDeque<Result<String, DesktopError>>>,
        calls: RefCell<Vec<(String, Vec<String>, Option<Vec<u8>>)>>,
        close_gate_on: Option<usize>,
        gate: DeliveryGate,
    }
    impl DesktopCommands for FakeCommands {
        fn run(
            &self,
            program: &'static str,
            args: &[&str],
            input: Option<&[u8]>,
        ) -> Result<String, DesktopError> {
            self.calls.borrow_mut().push((
                program.to_string(),
                args.iter().map(|s| s.to_string()).collect(),
                input.map(|s| s.to_vec()),
            ));
            if self.close_gate_on == Some(self.calls.borrow().len()) {
                self.gate.close();
            }
            self.replies
                .borrow_mut()
                .pop_front()
                .expect("unexpected external command")
        }
    }
    fn target() -> Target {
        Target {
            window: "100".into(),
            focus: "101".into(),
            wm_class: "\"gnome-terminal-server\", \"Gnome-terminal\"".into(),
            pid: "123".into(),
        }
    }
    fn matching_replies() -> VecDeque<Result<String, DesktopError>> {
        ["", "100", "WM_CLASS(STRING) = \"gnome-terminal-server\", \"Gnome-terminal\"\n_NET_WM_PID(CARDINAL) = 123", "101", "100", ""]
            .into_iter().map(|s| Ok(s.into())).collect()
    }
    fn fake(gate: &DeliveryGate) -> FakeCommands {
        FakeCommands {
            replies: RefCell::new(matching_replies()),
            calls: RefCell::new(Vec::new()),
            close_gate_on: None,
            gate: gate.clone(),
        }
    }

    #[test]
    fn dictation_is_one_line_without_control_characters() {
        assert_eq!(
            plain_text(" 你好\n世界\t hello\u{0000}\u{001b} "),
            "你好 世界 hello"
        );
        assert_eq!(plain_text("\r\n\t"), "");
        assert_eq!(append_delta("hello", "world"), " world");
        assert_eq!(append_delta("你好", "世界"), "世界");
        assert_eq!(append_delta("hello ", "world"), "world");
    }

    #[test]
    fn terminal_and_vscode_paste_bindings_never_include_enter() {
        assert_eq!(paste_key("\"Gnome-terminal\""), "ctrl+shift+v");
        assert_eq!(paste_key("\"Code\""), "shift+Insert");
        assert_eq!(paste_key("\"URxvt\""), "shift+Insert");
        assert_eq!(paste_key("Firefox"), "ctrl+v");
        assert!(!paste_key("kitty").to_ascii_lowercase().contains("enter"));
    }

    #[test]
    fn matched_target_copies_then_pastes_without_focus_or_enter_commands() {
        let gate = DeliveryGate::new();
        let commands = fake(&gate);
        assert_eq!(
            deliver_with("hello\nworld", &target(), false, " ", &gate, &commands).unwrap(),
            DeliveryOutcome::Pasted
        );
        let calls = commands.calls.borrow();
        assert_eq!(calls[0].0, "xclip");
        assert_eq!(calls[0].2, Some(b" hello world".to_vec()));
        assert_eq!(
            calls.last().unwrap().1,
            ["key", "--clearmodifiers", "ctrl+shift+v"]
        );
        assert!(calls
            .iter()
            .all(|(_, args, _)| !args.iter().any(|s| s == "windowactivate" || s == "Return")));
    }

    #[test]
    fn focus_changes_and_query_failures_only_copy() {
        let gate = DeliveryGate::new();
        let commands = fake(&gate);
        commands.replies.borrow_mut()[3] = Ok("999".into());
        assert_eq!(
            deliver_with("text", &target(), false, "", &gate, &commands).unwrap(),
            DeliveryOutcome::FocusChanged
        );
        assert_eq!(commands.calls.borrow().len(), 5);
        let commands = fake(&gate);
        commands.replies.borrow_mut()[1] = Err(DesktopError::TargetUnavailable);
        assert_eq!(
            deliver_with("text", &target(), false, "", &gate, &commands).unwrap(),
            DeliveryOutcome::FocusChanged
        );
        assert_eq!(commands.calls.borrow().len(), 2);
    }

    #[test]
    fn cancellation_during_copy_or_target_query_never_pastes() {
        for close_on in [1, 5] {
            let gate = DeliveryGate::new();
            let mut commands = fake(&gate);
            commands.close_gate_on = Some(close_on);
            assert_eq!(
                deliver_with("text", &target(), false, "", &gate, &commands).unwrap(),
                DeliveryOutcome::Cancelled
            );
            assert!(commands
                .calls
                .borrow()
                .iter()
                .all(|(_, args, _)| args.first().map(String::as_str) != Some("key")));
        }
    }

    #[test]
    fn closed_gate_empty_text_and_copy_only_do_not_query_or_paste() {
        let gate = DeliveryGate::new();
        let commands = fake(&gate);
        assert_eq!(
            deliver_with("\n", &target(), false, "", &gate, &commands).unwrap(),
            DeliveryOutcome::Empty
        );
        assert!(commands.calls.borrow().is_empty());
        gate.close();
        assert_eq!(
            deliver_with("text", &target(), false, "", &gate, &commands).unwrap(),
            DeliveryOutcome::Cancelled
        );
        assert!(commands.calls.borrow().is_empty());
        let gate = DeliveryGate::new();
        let commands = fake(&gate);
        assert_eq!(
            deliver_with("text", &target(), true, "", &gate, &commands).unwrap(),
            DeliveryOutcome::Copied
        );
        assert_eq!(commands.calls.borrow().len(), 1);
    }

    #[test]
    fn target_snapshot_rejects_top_level_change() {
        let gate = DeliveryGate::new();
        let commands = fake(&gate);
        commands.replies.borrow_mut().pop_front();
        commands.replies.borrow_mut()[3] = Ok("200".into());
        assert_eq!(
            query_target(&commands),
            Err(DesktopError::TargetUnavailable)
        );
    }

    #[test]
    fn incomplete_target_identity_only_copies() {
        for properties in [
            "WM_CLASS(STRING) = \"Editor\"",
            "_NET_WM_PID(CARDINAL) = 123",
            "WM_CLASS(STRING) = \"Editor\"\n_NET_WM_PID(CARDINAL) = 0",
        ] {
            let gate = DeliveryGate::new();
            let commands = fake(&gate);
            commands.replies.borrow_mut()[2] = Ok(properties.into());
            assert_eq!(
                deliver_with("text", &target(), false, "", &gate, &commands).unwrap(),
                DeliveryOutcome::FocusChanged
            );
            assert!(commands
                .calls
                .borrow()
                .iter()
                .all(|(_, args, _)| args.first().map(String::as_str) != Some("key")));
        }
    }

    #[test]
    fn accelerator_aliases_and_modifier_order_compare_semantically() {
        assert_eq!(
            normalized_accelerator("<Primary><Alt>F9"),
            normalized_accelerator("<Alt><Control>f9")
        );
        assert_eq!(
            normalized_accelerator("<Mod4>F9"),
            normalized_accelerator("<Super>F9")
        );
        assert_ne!(normalized_accelerator("F8"), normalized_accelerator("F9"));
        assert_eq!(normalized_accelerator("<Wrong>F9"), None);
        assert_eq!(normalized_accelerator("NoSuchKey"), None);
    }

    #[test]
    fn canonical_bindings_are_valid_gtk_accelerators() {
        for (input, expected) in [
            ("f9", "F9"),
            ("Esc", "Escape"),
            ("Enter", "Return"),
            ("<Ctrl>f9", "<Control>F9"),
            ("<Primary><Mod1>esc", "<Alt><Control>Escape"),
        ] {
            let canonical = normalized_accelerator(input).unwrap();
            assert_eq!(canonical, expected);
            let canonical = std::ffi::CString::new(canonical).unwrap();
            let mut key = 0;
            let mut modifiers = 0;
            // This parser only reads key names; no GTK initialization or window is needed.
            unsafe {
                gtk::ffi::gtk_accelerator_parse(canonical.as_ptr(), &mut key, &mut modifiers);
            }
            assert_ne!(key, 0, "{input}");
        }
        assert_eq!(
            normalized_accelerator("<Ctrl>f9"),
            normalized_accelerator("<Control>F9")
        );
    }

    #[test]
    fn invalid_shortcuts_are_rejected_before_any_installation() {
        for input in [
            "",
            "disabled",
            "<Wrong>F9",
            "NoSuchKey",
            "<Control>",
            "<Control>F999",
            "F9 space",
            "F9\n",
            "\tF9",
        ] {
            assert_eq!(normalized_accelerator(input), None, "{input:?}");
            assert_eq!(
                install_hotkey(input, Path::new("/definitely/not/a/client")),
                Err(DesktopError::InvalidHotkey)
            );
        }
    }

    #[test]
    fn install_plan_refuses_system_and_custom_conflicts_without_writes() {
        let existing = CustomEntry {
            name: String::new(),
            command: String::new(),
            binding: String::new(),
        };
        for owner in ["org.gnome.shell.keybindings/test", "/other/custom/path/"] {
            let entries = [ShortcutBinding {
                owner: owner.into(),
                binding: "F9".into(),
            }];
            assert_eq!(
                validate_install("F9", "'/client' --toggle", &entries, &existing),
                Err(DesktopError::HotkeyConflict)
            );
        }
        let entries = [ShortcutBinding {
            owner: "/existing/f8/".into(),
            binding: "F8".into(),
        }];
        assert!(validate_install("F9", "'/client' --toggle", &entries, &existing).is_ok());
    }

    #[test]
    fn install_plan_only_reuses_own_matching_command() {
        let command = "'/client' --toggle";
        let existing = CustomEntry {
            name: HOTKEY_NAME.into(),
            command: command.into(),
            binding: "F9".into(),
        };
        let entries = [ShortcutBinding {
            owner: HOTKEY_PATH.into(),
            binding: "F9".into(),
        }];
        assert!(validate_install("F9", command, &entries, &existing).is_ok());
        assert_eq!(
            validate_install("F9", "'/other' --toggle", &entries, &existing),
            Err(DesktopError::HotkeyPathInUse)
        );
    }
}
