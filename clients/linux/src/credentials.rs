use std::collections::{HashMap, HashSet};
use std::fmt;

use thiserror::Error;
use zeroize::{Zeroize, Zeroizing};

use crate::protocol::{DeviceToken, Endpoint};

const KEYRING_SERVICE: &str = "cn.oneaxe.voice.linux";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Persistence {
    Keyring,
    MemoryOnly,
}

#[derive(Debug, Error)]
pub enum CredentialError {
    #[error("系统密钥环不可用或已锁定，凭据未写入普通文件。")]
    Unavailable,
    #[error("密钥环中的设备凭据格式无效，请重新导入。")]
    InvalidStoredToken,
}

#[derive(Clone, Copy)]
enum BackendError {
    Missing,
    Unavailable,
}

trait KeyringBackend: Send {
    fn read(&self, origin: &str) -> Result<Zeroizing<Vec<u8>>, BackendError>;
    fn store(&self, origin: &str, token: &str) -> Result<(), BackendError>;
    fn delete(&self, origin: &str) -> Result<(), BackendError>;
}

struct SystemKeyring;

fn keyring_error(error: keyring::Error) -> BackendError {
    match error {
        keyring::Error::NoEntry => BackendError::Missing,
        // Never format a keyring error: BadEncoding carries the original secret.
        keyring::Error::BadEncoding(mut secret) => {
            secret.zeroize();
            BackendError::Unavailable
        }
        _ => BackendError::Unavailable,
    }
}

impl SystemKeyring {
    fn entry(origin: &str) -> Result<keyring::Entry, BackendError> {
        keyring::Entry::new(KEYRING_SERVICE, origin).map_err(keyring_error)
    }
}

impl KeyringBackend for SystemKeyring {
    fn read(&self, origin: &str) -> Result<Zeroizing<Vec<u8>>, BackendError> {
        Self::entry(origin)?
            .get_secret()
            .map(Zeroizing::new)
            .map_err(keyring_error)
    }

    fn store(&self, origin: &str, token: &str) -> Result<(), BackendError> {
        Self::entry(origin)?
            .set_password(token)
            .map_err(keyring_error)
    }

    fn delete(&self, origin: &str) -> Result<(), BackendError> {
        Self::entry(origin)?
            .delete_credential()
            .map_err(keyring_error)
    }
}

pub struct CredentialStore {
    backend: Box<dyn KeyringBackend>,
    memory: HashMap<String, CachedCredential>,
    deleted: HashSet<String>,
}

struct CachedCredential {
    token: DeviceToken,
    persistence: Persistence,
}

impl fmt::Debug for CredentialStore {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CredentialStore")
            .finish_non_exhaustive()
    }
}

impl Default for CredentialStore {
    fn default() -> Self {
        Self::new()
    }
}

impl CredentialStore {
    pub fn new() -> Self {
        Self {
            backend: Box::new(SystemKeyring),
            memory: HashMap::new(),
            deleted: HashSet::new(),
        }
    }

    pub fn read(&mut self, endpoint: &Endpoint) -> Result<Option<DeviceToken>, CredentialError> {
        let origin = endpoint.origin();
        if let Some(credential) = self.memory.get(origin) {
            return Ok(Some(credential.token.clone()));
        }
        if self.deleted.contains(origin) {
            return Ok(None);
        }
        let secret = match self.backend.read(origin) {
            Ok(secret) => secret,
            Err(BackendError::Missing) => return Ok(None),
            Err(BackendError::Unavailable) => return Err(CredentialError::Unavailable),
        };
        let text = std::str::from_utf8(&secret).map_err(|_| CredentialError::InvalidStoredToken)?;
        let token = DeviceToken::new(text).map_err(|_| CredentialError::InvalidStoredToken)?;
        self.memory.insert(
            origin.to_owned(),
            CachedCredential {
                token: token.clone(),
                persistence: Persistence::Keyring,
            },
        );
        Ok(Some(token))
    }

    /// Returns the cached credential's last known storage without accessing the keyring.
    pub fn persistence(&self, endpoint: &Endpoint) -> Option<Persistence> {
        self.memory
            .get(endpoint.origin())
            .map(|credential| credential.persistence)
    }

    pub fn store(
        &mut self,
        endpoint: &Endpoint,
        token: DeviceToken,
    ) -> Result<Persistence, CredentialError> {
        let origin = endpoint.origin();
        let persistence = match self.backend.store(origin, token.expose_secret()) {
            Ok(()) => Persistence::Keyring,
            Err(_) => Persistence::MemoryOnly,
        };
        self.deleted.remove(origin);
        self.memory
            .insert(origin.to_owned(), CachedCredential { token, persistence });
        Ok(persistence)
    }

    pub fn delete(&mut self, endpoint: &Endpoint) -> Result<(), CredentialError> {
        let origin = endpoint.origin();
        self.memory.remove(origin);
        // A failed keyring deletion must not silently restore that token this session.
        self.deleted.insert(origin.to_owned());
        match self.backend.delete(origin) {
            Ok(()) | Err(BackendError::Missing) => Ok(()),
            Err(BackendError::Unavailable) => Err(CredentialError::Unavailable),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct FakeState {
        values: HashMap<String, Vec<u8>>,
        fail_reads: bool,
        fail_writes: bool,
        fail_deletes: bool,
    }

    struct FakeKeyring(Arc<Mutex<FakeState>>);

    impl KeyringBackend for FakeKeyring {
        fn read(&self, origin: &str) -> Result<Zeroizing<Vec<u8>>, BackendError> {
            let state = self.0.lock().unwrap();
            if state.fail_reads {
                return Err(BackendError::Unavailable);
            }
            state
                .values
                .get(origin)
                .cloned()
                .map(Zeroizing::new)
                .ok_or(BackendError::Missing)
        }

        fn store(&self, origin: &str, token: &str) -> Result<(), BackendError> {
            let mut state = self.0.lock().unwrap();
            if state.fail_writes {
                return Err(BackendError::Unavailable);
            }
            state
                .values
                .insert(origin.to_owned(), token.as_bytes().to_vec());
            Ok(())
        }

        fn delete(&self, origin: &str) -> Result<(), BackendError> {
            let mut state = self.0.lock().unwrap();
            if state.fail_deletes {
                return Err(BackendError::Unavailable);
            }
            state.values.remove(origin).ok_or(BackendError::Missing)?;
            Ok(())
        }
    }

    fn with_backend(state: Arc<Mutex<FakeState>>) -> CredentialStore {
        CredentialStore {
            backend: Box::new(FakeKeyring(state)),
            memory: HashMap::new(),
            deleted: HashSet::new(),
        }
    }

    fn endpoint(name: &str) -> Endpoint {
        Endpoint::parse(&format!("https://{name}.example.ts.net:8097")).unwrap()
    }

    #[test]
    fn persists_and_isolates_credentials_by_server_origin() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let first = endpoint("first");
        let second = endpoint("second");
        let mut credentials = with_backend(state.clone());
        assert_eq!(
            credentials
                .store(&first, DeviceToken::new("fixture-first").unwrap())
                .unwrap(),
            Persistence::Keyring
        );
        assert_eq!(
            credentials
                .store(&second, DeviceToken::new("fixture-second").unwrap())
                .unwrap(),
            Persistence::Keyring
        );
        let mut fresh = with_backend(state);
        assert_eq!(
            fresh.read(&first).unwrap().unwrap().expose_secret(),
            "fixture-first"
        );
        assert_eq!(
            fresh.read(&second).unwrap().unwrap().expose_secret(),
            "fixture-second"
        );
        assert!(fresh.read(&endpoint("third")).unwrap().is_none());
    }

    #[test]
    fn failed_keyring_write_lasts_only_in_this_store() {
        let state = Arc::new(Mutex::new(FakeState {
            fail_writes: true,
            ..FakeState::default()
        }));
        let server = endpoint("first");
        let mut credentials = with_backend(state.clone());
        assert_eq!(
            credentials
                .store(&server, DeviceToken::new("fixture-memory").unwrap())
                .unwrap(),
            Persistence::MemoryOnly
        );
        assert_eq!(
            credentials.read(&server).unwrap().unwrap().expose_secret(),
            "fixture-memory"
        );
        assert!(state.lock().unwrap().values.is_empty());
        let mut fresh = with_backend(state);
        assert!(fresh.read(&server).unwrap().is_none());
        assert!(credentials.read(&endpoint("second")).unwrap().is_none());
    }

    #[test]
    fn keyring_unavailable_does_not_erase_the_session_token() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let server = endpoint("first");
        let mut credentials = with_backend(state.clone());
        credentials
            .store(&server, DeviceToken::new("fixture-cached").unwrap())
            .unwrap();
        state.lock().unwrap().fail_reads = true;
        assert_eq!(
            credentials.read(&server).unwrap().unwrap().expose_secret(),
            "fixture-cached"
        );
        assert!(matches!(
            credentials.read(&endpoint("second")),
            Err(CredentialError::Unavailable)
        ));
    }

    #[test]
    fn deletion_is_idempotent_and_preserves_other_origins() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let first = endpoint("first");
        let second = endpoint("second");
        let mut credentials = with_backend(state.clone());
        credentials
            .store(&first, DeviceToken::new("fixture-first").unwrap())
            .unwrap();
        credentials
            .store(&second, DeviceToken::new("fixture-second").unwrap())
            .unwrap();
        credentials.delete(&first).unwrap();
        credentials.delete(&first).unwrap();
        assert!(credentials.read(&first).unwrap().is_none());
        assert_eq!(
            credentials.read(&second).unwrap().unwrap().expose_secret(),
            "fixture-second"
        );
        assert!(!state.lock().unwrap().values.contains_key(first.origin()));
    }

    #[test]
    fn failed_delete_is_reported_and_does_not_restore_a_cached_token() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let server = endpoint("first");
        let mut credentials = with_backend(state.clone());
        credentials
            .store(&server, DeviceToken::new("fixture-first").unwrap())
            .unwrap();
        state.lock().unwrap().fail_deletes = true;
        assert!(matches!(
            credentials.delete(&server),
            Err(CredentialError::Unavailable)
        ));
        assert!(credentials.read(&server).unwrap().is_none());
        assert!(state.lock().unwrap().values.contains_key(server.origin()));
        assert_eq!(
            credentials
                .store(&server, DeviceToken::new("fixture-replaced").unwrap())
                .unwrap(),
            Persistence::Keyring
        );
        assert_eq!(
            credentials.read(&server).unwrap().unwrap().expose_secret(),
            "fixture-replaced"
        );
    }

    #[test]
    fn invalid_stored_values_and_debug_output_do_not_disclose_secrets() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let server = endpoint("first");
        state
            .lock()
            .unwrap()
            .values
            .insert(server.origin().to_owned(), b"fixture-invalid\n".to_vec());
        let mut credentials = with_backend(state.clone());
        let error = credentials.read(&server).unwrap_err();
        assert!(matches!(error, CredentialError::InvalidStoredToken));
        assert!(!format!("{error:?}: {error}").contains("fixture-invalid"));
        state
            .lock()
            .unwrap()
            .values
            .insert(server.origin().to_owned(), vec![0xff]);
        assert!(matches!(
            credentials.read(&server),
            Err(CredentialError::InvalidStoredToken)
        ));
        credentials
            .store(&server, DeviceToken::new("fixture-hidden").unwrap())
            .unwrap();
        assert!(!format!("{credentials:?}").contains("fixture-hidden"));
    }

    #[test]
    fn canonical_origins_share_credentials_but_different_ports_do_not() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let mut credentials = with_backend(state);
        let first = Endpoint::parse("https://first.example.ts.net:443/").unwrap();
        let canonical = Endpoint::parse("https://first.example.ts.net").unwrap();
        let other_port = Endpoint::parse("https://first.example.ts.net:8097").unwrap();
        credentials
            .store(&first, DeviceToken::new("fixture-canonical").unwrap())
            .unwrap();
        assert_eq!(
            credentials
                .read(&canonical)
                .unwrap()
                .unwrap()
                .expose_secret(),
            "fixture-canonical"
        );
        assert!(credentials.read(&other_port).unwrap().is_none());
    }

    #[test]
    fn storage_status_tracks_reads_writes_failures_and_deletion() {
        let state = Arc::new(Mutex::new(FakeState::default()));
        let server = endpoint("first");
        let mut credentials = with_backend(state.clone());
        assert_eq!(credentials.persistence(&server), None);
        credentials
            .store(&server, DeviceToken::new("fixture-durable").unwrap())
            .unwrap();
        assert_eq!(credentials.persistence(&server), Some(Persistence::Keyring));

        let mut fresh = with_backend(state.clone());
        assert_eq!(fresh.persistence(&server), None);
        fresh.read(&server).unwrap().unwrap();
        assert_eq!(fresh.persistence(&server), Some(Persistence::Keyring));
        state.lock().unwrap().fail_reads = true;
        fresh.read(&server).unwrap().unwrap();
        assert_eq!(fresh.persistence(&server), Some(Persistence::Keyring));

        state.lock().unwrap().fail_writes = true;
        fresh
            .store(&server, DeviceToken::new("fixture-new-memory").unwrap())
            .unwrap();
        assert_eq!(fresh.persistence(&server), Some(Persistence::MemoryOnly));
        assert_eq!(
            fresh.read(&server).unwrap().unwrap().expose_secret(),
            "fixture-new-memory"
        );
        assert_eq!(fresh.persistence(&server), Some(Persistence::MemoryOnly));
        assert_eq!(fresh.persistence(&endpoint("second")), None);

        state.lock().unwrap().fail_deletes = true;
        assert!(fresh.delete(&server).is_err());
        assert_eq!(fresh.persistence(&server), None);
        assert!(fresh.read(&server).unwrap().is_none());
    }

    #[test]
    fn failed_or_invalid_reads_never_claim_persistent_storage() {
        let state = Arc::new(Mutex::new(FakeState {
            fail_reads: true,
            ..FakeState::default()
        }));
        let server = endpoint("first");
        let mut credentials = with_backend(state.clone());
        assert!(credentials.read(&server).is_err());
        assert_eq!(credentials.persistence(&server), None);
        state.lock().unwrap().fail_reads = false;
        assert!(credentials.read(&server).unwrap().is_none());
        assert_eq!(credentials.persistence(&server), None);
        state
            .lock()
            .unwrap()
            .values
            .insert(server.origin().to_owned(), vec![0xff]);
        assert!(credentials.read(&server).is_err());
        assert_eq!(credentials.persistence(&server), None);
    }
}
