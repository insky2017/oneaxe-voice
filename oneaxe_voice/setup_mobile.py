"""Provision the user's own Tailnet certificate and private listener settings."""

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import tempfile

from .config import Settings


def resolve_docker_image(image: str) -> str:
    if (not isinstance(image, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image)):
        raise ValueError("Docker 镜像名称无效")
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                            capture_output=True, timeout=10)
    image_id = result.stdout.decode("ascii", errors="replace").strip()
    if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("本地 Docker 镜像不可用；不会自动拉取镜像")
    return image_id


def issue_certificate(hostname: str, cert_file: Path, key_file: Path, *, sudo=False, docker_image=None):
    if sudo and docker_image is not None:
        raise ValueError("sudo 与 Docker 证书签发不能同时使用")
    if docker_image is not None and (not isinstance(docker_image, str)
                                   or not re.fullmatch(r"sha256:[0-9a-f]{64}", docker_image)):
        raise ValueError("Docker 证书签发须使用固定 sha256 镜像 ID")
    cert_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".tls-", dir=cert_file.parent) as temporary:
        cert, key = Path(temporary) / "server.crt", Path(temporary) / "server.key"
        if sudo or docker_image is not None:
            # Both PEM outputs can use stdout. Tailscale's file writer uses
            # atomic replacement, so /dev/null is not an output destination.
            if docker_image is not None:
                command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                           "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=0:0",
                           "--log-driver=none",
                           "--mount", "type=bind,src=/usr/bin/tailscale,dst=/usr/bin/tailscale,readonly",
                           "--mount", "type=bind,src=/var/run/tailscale/tailscaled.sock,"
                           "dst=/var/run/tailscale/tailscaled.sock,readonly",
                           "--entrypoint", "/usr/bin/tailscale", docker_image]
            else:
                command = ["sudo", "tailscale"]
            command += ["cert", "--min-validity=336h", "--cert-file=-", "--key-file=-", hostname]
            result = subprocess.run(command, capture_output=True, timeout=120)
            if result.returncode:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                detail = re.sub(r"-----BEGIN .*", "[PEM omitted]", detail, flags=re.S)
                raise RuntimeError("Tailscale 证书签发失败：" + (detail[:1200] or f"exit={result.returncode}"))
            certificates = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", result.stdout, re.S)
            keys = re.findall(rb"-----BEGIN (?:EC |RSA )?PRIVATE KEY-----.*?-----END (?:EC |RSA )?PRIVATE KEY-----", result.stdout, re.S)
            if not certificates or len(keys) != 1:
                raise RuntimeError("Tailscale 未返回完整的证书和私钥；输出未显示")
            cert.write_bytes(b"\n".join(certificates) + b"\n")
            key.write_bytes(keys[0] + b"\n")
        else:
            result = subprocess.run(
                ["tailscale", "cert", "--min-validity=336h", f"--cert-file={cert}",
                 f"--key-file={key}", hostname], capture_output=True, timeout=90)
            if result.returncode:
                raise RuntimeError("Tailscale 证书签发失败；可在本机终端使用 --sudo-cert，仅提权签发证书")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        checked = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-checkhost", hostname],
                                 capture_output=True, timeout=10)
        if checked.returncode:
            raise ValueError("TLS 证书主机名不匹配")
        cert.chmod(0o600)
        key.chmod(0o600)
        os.replace(cert, cert_file)
        os.replace(key, key_file)


def configure(settings: Settings, port=8097, *, sudo_cert=False, docker_cert=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口须为 1–65535")
    if sudo_cert and docker_cert is not None:
        raise ValueError("sudo 与 Docker 证书签发不能同时使用")
    docker_image = resolve_docker_image(docker_cert) if docker_cert is not None else None
    result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, check=True, timeout=10)
    tailnet = json.loads(result.stdout)
    hostname = tailnet.get("Self", {}).get("DNSName", "").rstrip(".")
    if tailnet.get("BackendState") != "Running" or hostname not in tailnet.get("CertDomains", []):
        raise ValueError("请先连接 Tailscale 并启用本机 Tailnet HTTPS 证书")
    addresses = tailnet.get("Self", {}).get("TailscaleIPs", [])
    bind = next((value for value in addresses if ":" not in value), None)
    if not bind:
        raise ValueError("本机尚无 Tailnet IPv4 地址")
    runtime = settings.runtime_dir
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = runtime / "tls/server.crt", runtime / "tls/server.key"
    issue_certificate(hostname, cert, key, sudo=sudo_cert, docker_image=docker_image)
    value = dict(hostname=hostname, bind=bind, port=port,
                 cert_file=str(cert.resolve()), key_file=str(key.resolve()), renew=not sudo_cert)
    if docker_image is not None:
        value["docker_image"] = docker_image
    descriptor, temporary = tempfile.mkstemp(prefix=".mobile-", dir=runtime)
    try:
        with os.fdopen(descriptor, "w") as target:
            os.fchmod(target.fileno(), 0o600)
            json.dump(value, target, indent=2)
            target.write("\n")
        os.replace(temporary, runtime / "mobile-listener.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return value


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="配置 OneAxe Voice Tailnet HTTPS/WSS 入口")
    parser.add_argument("--port", type=int, default=8097)
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument("--sudo-cert", action="store_true",
                        help="仅以 sudo 签发证书；不修改 Tailscale 权限，续期需再次运行本命令")
    signing.add_argument("--docker-cert", metavar="IMAGE",
                         help="使用已有本地 Docker 镜像签发并自动续期，不拉取镜像")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        parser.exit(2, "请以普通用户运行本脚本；提权仅用于证书签发。\n")
    os.umask(0o077)
    try:
        value = configure(Settings.from_env(), args.port, sudo_cert=args.sudo_cert,
                          docker_cert=args.docker_cert)
        print(f"已配置 https://{value['hostname']}:{value['port']}；下次服务启动生效。")
        print("配置不加载模型，不重启当前服务。")
        if args.sudo_cert:
            print("证书由当前用户安全保存；每月重跑此命令续期。已运行服务会自动重读证书。")
        elif args.docker_cert:
            print("证书由当前用户安全保存；自动续期使用已固定的本地 Docker 镜像。")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"移动入口配置失败：{exc}\n")


if __name__ == "__main__":
    main()
