"""Private device capabilities, with atomic persistence and no stored bearer."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import uuid


MOBILE_READ = "voice.mobile.read"
MOBILE_STREAM = "voice.mobile.stream"
MOBILE_SCOPES = (MOBILE_READ, MOBILE_STREAM)


@dataclass(frozen=True)
class DevicePrincipal:
    credential_id: str
    device_id: str
    scopes: tuple[str, ...]


class DeviceCredentialStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records = {}
        self._stamp = None
        self.refresh()

    def _read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("devices"), dict)):
            raise ValueError("移动设备凭据文件无效")
        records = value["devices"]
        for key, record in records.items():
            if (not isinstance(record, dict) or record.get("credential_id") != key
                    or not isinstance(record.get("device_id"), str)
                    or not isinstance(record.get("token_sha256"), str)
                    or len(record["token_sha256"]) != 64
                    or not isinstance(record.get("scopes"), list)
                    or any(scope not in MOBILE_SCOPES for scope in record["scopes"])):
                raise ValueError("移动设备凭据文件无效")
        return records

    def refresh(self):
        with self._lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                stamp = None
            if stamp != self._stamp or stamp is None:
                self._records = self._read()
                self._stamp = stamp

    def authenticate(self, token: str, required_scope: str | None = None):
        self.refresh()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            for record in self._records.values():
                if hmac.compare_digest(digest, record["token_sha256"]):
                    if record.get("revoked_at") is not None:
                        return None
                    if required_scope and required_scope not in record["scopes"]:
                        return None
                    return DevicePrincipal(record["credential_id"], record["device_id"],
                                           tuple(record["scopes"]))
        return None

    def is_active(self, credential_id: str):
        """Fast validation for an engine lifecycle critical section."""
        with self._lock:
            record = self._records.get(credential_id)
            return bool(record and record.get("revoked_at") is None
                        and MOBILE_STREAM in record["scopes"])

    def recognizes(self, token: str):
        self.refresh()
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            return any(hmac.compare_digest(digest, record["token_sha256"])
                       for record in self._records.values())

    def _write(self):
        descriptor, name = tempfile.mkstemp(prefix=".mobile-devices-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump({"version": 1, "devices": self._records}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            self._stamp = None
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            descriptor = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._records = self._read()
                try:
                    yield
                    self._write()
                except BaseException:
                    self._records = self._read()
                    self._stamp = None
                    raise
            finally:
                os.close(descriptor)

    @staticmethod
    def _public(record):
        return {key: record.get(key) for key in (
            "credential_id", "device_id", "name", "scopes", "created_at", "revoked_at")}

    def list_devices(self):
        self.refresh()
        with self._lock:
            return [self._public(record) for record in self._records.values()]

    def _issue(self, name, device_id=None, scopes=MOBILE_SCOPES):
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("设备名称须为 1 至 80 个字符")
        if not scopes or any(scope not in MOBILE_SCOPES for scope in scopes):
            raise ValueError("移动设备权限无效")
        token = secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        record = {"credential_id": credential_id, "device_id": device_id or str(uuid.uuid4()),
                  "name": name.strip(), "scopes": list(scopes), "created_at": time.time(),
                  "revoked_at": None,
                  "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest()}
        self._records[credential_id] = record
        return {**self._public(record), "token": token}

    def issue(self, name, scopes=MOBILE_SCOPES):
        with self._transaction():
            result = self._issue(name, scopes=scopes)
        return result

    def revoke(self, credential_id):
        with self._transaction():
            record = self._records.get(credential_id)
            if record is None:
                raise KeyError(credential_id)
            if record.get("revoked_at") is None:
                record["revoked_at"] = time.time()
            result = self._public(record)
        return result

    def rotate(self, credential_id):
        with self._transaction():
            record = self._records.get(credential_id)
            if record is None:
                raise KeyError(credential_id)
            record["revoked_at"] = time.time()
            result = self._issue(record["name"], device_id=record["device_id"],
                                 scopes=record["scopes"])
        return result
