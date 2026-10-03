use std::env;
use std::fs::{self, DirBuilder, File};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tempfile::NamedTempFile;
use thiserror::Error;

use crate::protocol::Endpoint;

pub const DEFAULT_SERVER_URL: &str = "https://rtx4090.nase-stairs.ts.net:8097";
const CONFIG_DIRECTORY: &str = "oneaxe-voice-linux";
const MAX_CONFIG_BYTES: usize = 64 * 1024;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub server_url: String,
    pub microphone: Option<String>,
    pub shortcut: String,
    pub auto_paste: bool,
    pub show_preview: bool,
    pub autostart: bool,
    pub pause_ms: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            server_url: DEFAULT_SERVER_URL.to_owned(),
            microphone: None,
            shortcut: "F9".to_owned(),
            auto_paste: true,
            show_preview: true,
            autostart: false,
            pause_ms: 1000,
        }
    }
}

#[derive(Debug, Error)]
pub enum SettingsError {
    #[error("无法访问配置文件：{0}")]
    Io(#[from] io::Error),
    #[error("无法确定用户配置目录。")]
    MissingDirectory,
    #[error("配置文件必须为有效 JSON，且仅包含支持的设置项。")]
    InvalidJson,
    #[error("配置必须是当前用户目录中的普通文件，不支持符号链接。")]
    UnsafePath,
    #[error("{0}")]
    Invalid(&'static str),
}

impl Config {
    pub fn path() -> Result<PathBuf, SettingsError> {
        let xdg = env::var_os("XDG_CONFIG_HOME").map(PathBuf::from);
        let root = xdg
            .filter(|path| path.is_absolute())
            .or_else(|| dirs::home_dir().map(|home| home.join(".config")))
            .ok_or(SettingsError::MissingDirectory)?;
        Ok(root.join(CONFIG_DIRECTORY).join("config.json"))
    }

    pub fn load() -> Result<Self, SettingsError> {
        Self::load_from(&Self::path()?)
    }

    pub fn save(&self) -> Result<(), SettingsError> {
        self.save_to(&Self::path()?)
    }

    pub fn validate(&self) -> Result<(), SettingsError> {
        Endpoint::parse(&self.server_url).map_err(|_| {
            SettingsError::Invalid(
                "服务地址必须为 HTTPS 的 *.ts.net 根地址，不得包含用户信息、路径、查询参数或片段。",
            )
        })?;
        if !(200..=10_000).contains(&self.pause_ms) {
            return Err(SettingsError::Invalid("静音时长必须为 200 至 10000 毫秒。"));
        }
        if self.shortcut.trim().is_empty()
            || self.shortcut.len() > 128
            || self.shortcut.chars().any(char::is_control)
        {
            return Err(SettingsError::Invalid("快捷键格式无效。"));
        }
        if let Some(source) = &self.microphone {
            if source.trim().is_empty()
                || source.len() > 512
                || source.chars().any(char::is_control)
            {
                return Err(SettingsError::Invalid("麦克风来源名称无效。"));
            }
        }
        Ok(())
    }

    fn load_from(path: &Path) -> Result<Self, SettingsError> {
        let parent = path.parent().ok_or(SettingsError::UnsafePath)?;
        match fs::symlink_metadata(parent) {
            Ok(metadata) => secure_directory(parent, &metadata)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(error) => return Err(error.into()),
        }
        match fs::symlink_metadata(path) {
            Ok(metadata) => check_regular_file(&metadata)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Self::default()),
            Err(error) => return Err(error.into()),
        }
        let file = File::open(path)?;
        check_regular_file(&file.metadata()?)?;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        let mut data = Vec::new();
        file.take((MAX_CONFIG_BYTES + 1) as u64)
            .read_to_end(&mut data)?;
        if data.len() > MAX_CONFIG_BYTES {
            return Err(SettingsError::Invalid("配置文件过大。"));
        }
        let config: Self = serde_json::from_slice(&data).map_err(|_| SettingsError::InvalidJson)?;
        config.validate()?;
        Ok(config)
    }

    fn save_to(&self, path: &Path) -> Result<(), SettingsError> {
        self.validate()?;
        let parent = path.parent().ok_or(SettingsError::UnsafePath)?;
        DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(parent)?;
        secure_directory(parent, &fs::symlink_metadata(parent)?)?;
        match fs::symlink_metadata(path) {
            Ok(metadata) => check_regular_file(&metadata)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }

        let data = serde_json::to_vec_pretty(self).map_err(|_| SettingsError::InvalidJson)?;
        let mut temporary = NamedTempFile::new_in(parent)?;
        temporary
            .as_file()
            .set_permissions(fs::Permissions::from_mode(0o600))?;
        temporary.write_all(&data)?;
        temporary.write_all(b"\n")?;
        temporary.as_file().sync_all()?;
        temporary.persist(path).map_err(|error| error.error)?;
        File::open(parent)?.sync_all()?;
        Ok(())
    }
}

fn owned_by_current_user(metadata: &fs::Metadata) -> bool {
    // geteuid has no arguments or memory safety preconditions.
    metadata.uid() == unsafe { libc::geteuid() }
}

fn secure_directory(path: &Path, metadata: &fs::Metadata) -> Result<(), SettingsError> {
    if !metadata.is_dir() || metadata.file_type().is_symlink() || !owned_by_current_user(metadata) {
        return Err(SettingsError::UnsafePath);
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

fn check_regular_file(metadata: &fs::Metadata) -> Result<(), SettingsError> {
    if !metadata.is_file() || metadata.file_type().is_symlink() || !owned_by_current_user(metadata)
    {
        return Err(SettingsError::UnsafePath);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn config_path(directory: &tempfile::TempDir) -> PathBuf {
        directory.path().join(CONFIG_DIRECTORY).join("config.json")
    }

    #[test]
    fn missing_configuration_uses_requested_defaults() {
        let directory = tempfile::tempdir().unwrap();
        let config = Config::load_from(&config_path(&directory)).unwrap();
        assert_eq!(config.server_url, DEFAULT_SERVER_URL);
        assert_eq!(config.microphone, None);
        assert_eq!(config.shortcut, "F9");
        assert!(config.auto_paste && config.show_preview);
        assert!(!config.autostart);
        assert_eq!(config.pause_ms, 1000);
        config.validate().unwrap();
    }

    #[test]
    fn round_trip_uses_private_permissions_and_contains_no_credentials() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        let config = Config {
            microphone: Some("alsa_input.example".to_owned()),
            shortcut: "F10".to_owned(),
            ..Config::default()
        };
        config.save_to(&path).unwrap();
        assert_eq!(Config::load_from(&path).unwrap(), config);
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        let json: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
        assert_eq!(json.as_object().unwrap().len(), 7);
        assert!(json.get("token").is_none());
        assert!(json.get("device_token").is_none());
    }

    #[test]
    fn replacing_configuration_leaves_only_the_final_file() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        Config::default().save_to(&path).unwrap();
        let changed = Config {
            auto_paste: false,
            pause_ms: 2000,
            ..Config::default()
        };
        changed.save_to(&path).unwrap();
        assert_eq!(Config::load_from(&path).unwrap(), changed);
        assert_eq!(fs::read_dir(path.parent().unwrap()).unwrap().count(), 1);
        let previous = fs::read(&path).unwrap();
        let invalid = Config {
            pause_ms: 0,
            ..changed
        };
        assert!(invalid.save_to(&path).is_err());
        assert_eq!(fs::read(&path).unwrap(), previous);
    }

    #[test]
    fn existing_configuration_permissions_are_restricted_on_load() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        Config::default().save_to(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o666)).unwrap();
        fs::set_permissions(path.parent().unwrap(), fs::Permissions::from_mode(0o755)).unwrap();
        Config::load_from(&path).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(path.parent().unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }

    #[test]
    fn rejects_unknown_credential_fields_without_echoing_values() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        Config::default().save_to(&path).unwrap();
        fs::write(&path, br#"{"token":"fixture-secret-do-not-echo"}"#).unwrap();
        let error = Config::load_from(&path).unwrap_err();
        assert!(matches!(error, SettingsError::InvalidJson));
        assert!(!format!("{error:?}: {error}").contains("fixture-secret-do-not-echo"));
    }

    #[test]
    fn partial_configuration_gets_safe_defaults() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        Config::default().save_to(&path).unwrap();
        fs::write(&path, br#"{"show_preview":false}"#).unwrap();
        let config = Config::load_from(&path).unwrap();
        assert!(!config.show_preview);
        assert_eq!(config.server_url, DEFAULT_SERVER_URL);
    }

    #[test]
    fn rejects_public_and_non_origin_addresses() {
        for server_url in [
            "http://example.ts.net",
            "https://example.com",
            "https://ts.net",
            "https://user@example.ts.net",
            "https://example.ts.net/api",
            "https://example.ts.net/?token=fixture",
            "https://example.ts.net/#fragment",
        ] {
            let config = Config {
                server_url: server_url.to_owned(),
                ..Config::default()
            };
            assert!(config.validate().is_err(), "{server_url}");
        }
    }

    #[test]
    fn rejects_invalid_local_preferences() {
        assert!(Config {
            pause_ms: 199,
            ..Config::default()
        }
        .validate()
        .is_err());
        assert!(Config {
            pause_ms: 10_001,
            ..Config::default()
        }
        .validate()
        .is_err());
        assert!(Config {
            shortcut: "\nF9".to_owned(),
            ..Config::default()
        }
        .validate()
        .is_err());
        assert!(Config {
            microphone: Some(String::new()),
            ..Config::default()
        }
        .validate()
        .is_err());
    }

    #[test]
    fn rejects_symbolic_links_and_preserves_their_targets() {
        let directory = tempfile::tempdir().unwrap();
        let path = config_path(&directory);
        Config::default().save_to(&path).unwrap();
        let original = fs::read(&path).unwrap();
        let link = path.parent().unwrap().join("link.json");
        symlink(&path, &link).unwrap();
        assert!(matches!(
            Config::load_from(&link),
            Err(SettingsError::UnsafePath)
        ));
        assert!(matches!(
            Config::default().save_to(&link),
            Err(SettingsError::UnsafePath)
        ));
        let directory_link = directory.path().join("linked-directory");
        symlink(path.parent().unwrap(), &directory_link).unwrap();
        assert!(matches!(
            Config::default().save_to(&directory_link.join("config.json")),
            Err(SettingsError::UnsafePath)
        ));
        assert_eq!(fs::read(path).unwrap(), original);
    }
}
