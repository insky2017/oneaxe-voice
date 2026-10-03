use glib::translate::ToGlibPtr;
use libloading::Library;
use std::ffi::CString;
use std::path::PathBuf;

type IndicatorNew = unsafe extern "C" fn(
    *const libc::c_char,
    *const libc::c_char,
    libc::c_int,
) -> *mut libc::c_void;
type SetStatus = unsafe extern "C" fn(*mut libc::c_void, libc::c_int);
type SetMenu = unsafe extern "C" fn(*mut libc::c_void, *mut gtk::ffi::GtkMenu);
type SetIcon = unsafe extern "C" fn(*mut libc::c_void, *const libc::c_char, *const libc::c_char);

pub struct Tray {
    indicator: *mut libc::c_void,
    set_icon: SetIcon,
    _menu: gtk::Menu,
    _library: Library,
}

pub fn icon_path(recording: bool) -> Option<PathBuf> {
    let file = if recording {
        "oneaxe-voice-linux-recording.svg"
    } else {
        "oneaxe-voice-linux.svg"
    };
    let mut roots = Vec::new();
    if let Some(data) = dirs::data_dir() {
        roots.push(data.join("oneaxe-voice-linux/icons"));
        roots.push(data.join("icons/hicolor/scalable/apps"));
    }
    roots.push(PathBuf::from("/usr/share/oneaxe-voice-linux/icons"));
    roots.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("assets"));
    roots
        .into_iter()
        .map(|root| root.join(file))
        .find(|path| path.is_file())
}

impl Tray {
    pub fn new(menu: &gtk::Menu) -> Option<Self> {
        for name in ["libayatana-appindicator3.so.1", "libappindicator3.so.1"] {
            // Keep the library and menu alive until the indicator's final unref.
            let result = unsafe { Self::load(name, menu) };
            if result.is_some() {
                return result;
            }
        }
        None
    }

    unsafe fn load(name: &str, menu: &gtk::Menu) -> Option<Self> {
        let library = Library::new(name).ok()?;
        let new = *library.get::<IndicatorNew>(b"app_indicator_new\0").ok()?;
        let set_status = *library
            .get::<SetStatus>(b"app_indicator_set_status\0")
            .ok()?;
        let set_menu = *library.get::<SetMenu>(b"app_indicator_set_menu\0").ok()?;
        let set_icon = *library
            .get::<SetIcon>(b"app_indicator_set_icon_full\0")
            .ok()?;
        let id = CString::new("oneaxe-voice-linux").ok()?;
        let icon = CString::new("audio-input-microphone").ok()?;
        let indicator = new(id.as_ptr(), icon.as_ptr(), 0);
        if indicator.is_null() {
            return None;
        }
        set_menu(indicator, menu.to_glib_none().0);
        set_status(indicator, 1);
        let tray = Self {
            indicator,
            set_icon,
            _menu: menu.clone(),
            _library: library,
        };
        tray.set_recording(false);
        Some(tray)
    }

    pub fn set_recording(&self, recording: bool) {
        let path = icon_path(recording)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_else(|| "audio-input-microphone".into());
        let Ok(icon) = CString::new(path) else {
            return;
        };
        let description = CString::new(if recording {
            "OneAxe Voice - recording"
        } else {
            "OneAxe Voice"
        })
        .unwrap();
        unsafe {
            (self.set_icon)(self.indicator, icon.as_ptr(), description.as_ptr());
        }
    }
}

impl Drop for Tray {
    fn drop(&mut self) {
        unsafe {
            glib::gobject_ffi::g_object_unref(self.indicator.cast());
        }
    }
}
