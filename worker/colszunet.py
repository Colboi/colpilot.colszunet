from __future__ import annotations

import argparse
import base64
import ctypes
import configparser
import hashlib
import hmac
import json
import logging
import os
import platform
import ssl
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from ipaddress import IPv4Address
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener, getproxies


MASK = 0xFFFFFFFF
BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
LOGGER_NAME = "colszunet"
SINGLE_INSTANCE_EXIT_CODE = 3
SINGLE_INSTANCE_MUTEX_NAME = "Local\\Colpilot.Colszunet.AutoLogin"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5


class ConfigurationError(ValueError):
    pass


class PortalError(RuntimeError):
    pass


class CaptchaRequiredError(PortalError):
    pass


class PortalRequestError(PortalError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_kind = error_kind


class SingleInstanceGuard:
    def __init__(self, name: str) -> None:
        self.name = name
        self._handle: wintypes.HANDLE | None = None
        self._kernel32: object | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, f"CreateMutexW failed: {self.name}")

        if ctypes.get_last_error() == 183:
            if not kernel32.CloseHandle(handle):
                error_code = ctypes.get_last_error()
                raise OSError(error_code, f"CloseHandle failed: {self.name}")
            return False

        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = self._kernel32
        if kernel32 is None:
            raise RuntimeError("单实例锁的 Windows API 句柄没有初始化")
        if not kernel32.CloseHandle(self._handle):
            error_code = ctypes.get_last_error()
            raise OSError(error_code, f"CloseHandle failed: {self.name}")
        self._handle = None
        self._kernel32 = None


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    domain: str
    portal_origin: str
    ac_id: str
    device: str
    platform: str
    connectivity_test_url: str
    failure_threshold: int
    connectivity_timeout_seconds: float
    probe_interval_seconds: float
    login_verify_attempts: int
    login_verify_interval_seconds: float
    login_retry_cooldown_seconds: float
    request_timeout_seconds: float
    captcha_id: str
    captcha_value: str

    @property
    def full_username(self) -> str:
        return self.username + self.domain


@dataclass(frozen=True)
class ConnectivityResult:
    success: bool
    detail: str
    status_code: int | None = None
    final_url: str | None = None


@dataclass(frozen=True)
class ApiSummary:
    error: str = ""
    error_msg: str = ""
    ecode: str = ""
    code: str = ""
    message: str = ""
    suc_msg: str = ""


def _required_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"配置项 {key} 需要填写非空文本")
    return value.strip()


def _optional_text(
    data: dict[str, object],
    key: str,
    default: str = "",
) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigurationError(f"配置项 {key} 需要使用文本")
    return value.strip()


def _positive_integer(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise ConfigurationError(f"配置项 {key} 需要使用大于零的整数") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"配置项 {key} 需要使用大于零的整数")
    return value


def _positive_number(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError as exc:
            raise ConfigurationError(f"配置项 {key} 需要使用大于零的数字") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigurationError(f"配置项 {key} 需要使用大于零的数字")
    return float(value)


def _normalise_url(value: str, key: str) -> str:
    normalised = value if "://" in value else f"https://{value}"
    parsed = urlparse(normalised)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"配置项 {key} 需要使用完整的 HTTP 或 HTTPS 地址")
    return normalised.rstrip("/")


def load_settings(path: Path, section: str) -> Settings:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置文件不存在: {path}") from exc
    except (OSError, configparser.Error) as exc:
        raise ConfigurationError(f"配置文件格式错误: {path}") from exc
    if not parser.has_section(section):
        raise ConfigurationError(f"配置文件缺少配置节: {section}")
    raw: dict[str, object] = dict(parser.items(section, raw=True))

    domain = _optional_text(raw, "domain")
    if domain and not domain.startswith("@"):
        domain = "@" + domain

    captcha_id = _optional_text(raw, "captcha_id")
    captcha_value = _optional_text(raw, "captcha_value")
    if bool(captcha_id) != bool(captcha_value):
        raise ConfigurationError("配置项 captcha_id 与 captcha_value 需要同时填写")

    return Settings(
        username=_required_text(raw, "username"),
        password=_required_text(raw, "password"),
        domain=domain,
        portal_origin=_normalise_url(
            _required_text(raw, "portal_origin"),
            "portal_origin",
        ),
        ac_id=_required_text(raw, "ac_id"),
        device=_optional_text(raw, "device", "Windows 10"),
        platform=_optional_text(raw, "platform", "Windows"),
        connectivity_test_url=_normalise_url(
            _required_text(raw, "connectivity_test_url"),
            "connectivity_test_url",
        ),
        failure_threshold=_positive_integer(raw, "failure_threshold"),
        connectivity_timeout_seconds=_positive_number(
            raw,
            "connectivity_timeout_seconds",
        ),
        probe_interval_seconds=_positive_number(
            raw,
            "probe_interval_seconds",
        ),
        login_verify_attempts=_positive_integer(raw, "login_verify_attempts"),
        login_verify_interval_seconds=_positive_number(
            raw,
            "login_verify_interval_seconds",
        ),
        login_retry_cooldown_seconds=_positive_number(
            raw,
            "login_retry_cooldown_seconds",
        ),
        request_timeout_seconds=_positive_number(
            raw,
            "request_timeout_seconds",
        ),
        captcha_id=captcha_id,
        captcha_value=captcha_value,
    )


def _utf16_code_units(value: str) -> list[int]:
    raw = value.encode("utf-16-le", "surrogatepass")
    return [
        raw[index] | (raw[index + 1] << 8)
        for index in range(0, len(raw), 2)
    ]


def _to_words(value: str, include_length: bool) -> list[int]:
    code_units = _utf16_code_units(value)
    words: list[int] = []
    for start in range(0, len(code_units), 4):
        word = 0
        for offset in range(4):
            index = start + offset
            if index < len(code_units):
                word |= code_units[index] << (offset * 8)
        words.append(word & MASK)
    if include_length:
        words.append(len(code_units))
    return words


def _from_words(words: list[int]) -> bytes:
    output = bytearray()
    for word in words:
        output.extend(
            (
                word & 0xFF,
                (word >> 8) & 0xFF,
                (word >> 16) & 0xFF,
                (word >> 24) & 0xFF,
            )
        )
    return bytes(output)


def xencode(message: str, key: str) -> str:
    if message == "":
        return ""

    values = _to_words(message, True)
    keys = _to_words(key, False)
    keys.extend([0] * (4 - len(keys)))
    n = len(values) - 1
    z = values[n]
    delta = (0x86014019 | 0x183639A0) & MASK
    rounds = 6 + 52 // (n + 1)
    total = 0

    while rounds > 0:
        total = (total + delta) & MASK
        selector = (total >> 2) & 3
        for position in range(n):
            y = values[position + 1]
            mixed = ((z >> 5) ^ ((y << 2) & MASK)) & MASK
            mixed = (
                mixed
                + ((y >> 3) ^ ((z << 4) & MASK) ^ (total ^ y))
            ) & MASK
            mixed = (mixed + ((keys[(position & 3) ^ selector] ^ z) & MASK)) & MASK
            z = values[position] = (values[position] + mixed) & MASK

        y = values[0]
        mixed = ((z >> 5) ^ ((y << 2) & MASK)) & MASK
        mixed = (
            mixed
            + ((y >> 3) ^ ((z << 4) & MASK) ^ (total ^ y))
        ) & MASK
        mixed = (mixed + ((keys[(n & 3) ^ selector] ^ z) & MASK)) & MASK
        z = values[n] = (values[n] + mixed) & MASK
        rounds -= 1

    return _from_words(values).decode("latin1")


def custom_base64_encode(value: bytes) -> str:
    standard = base64.b64encode(value).decode("ascii")
    return "".join(
        "=" if char == "=" else CUSTOM_ALPHABET[BASE64_ALPHABET.index(char)]
        for char in standard
    )


def hmac_md5(message: str, key: str) -> str:
    return hmac.new(
        key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.md5,
    ).hexdigest()


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def build_info(
    username: str,
    password: str,
    ip: str,
    ac_id: str,
    token: str,
) -> str:
    payload = json.dumps(
        {
            "username": username,
            "password": password,
            "ip": ip,
            "acid": ac_id,
            "enc_ver": "srun_bx1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = xencode(payload, token).encode("latin1")
    return "{SRBX1}" + custom_base64_encode(encoded)


def summarise_response(response: dict[str, object]) -> ApiSummary:
    return ApiSummary(
        error=str(response.get("error", "")),
        error_msg=str(response.get("error_msg", "")),
        ecode=str(response.get("ecode", "")),
        code=str(response.get("code", "")),
        message=str(response.get("message", "")),
        suc_msg=str(response.get("suc_msg", "")),
    )


def _build_direct_opener():
    return build_opener(ProxyHandler({}))


def _target_description(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or "unknown-host"
    try:
        port = parsed.port
    except ValueError:
        port = None
    host_port = hostname if port is None else f"{hostname}:{port}"
    return f"{parsed.scheme}://{host_port}{parsed.path or '/'}"


def _network_error_kind(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    candidates = (error, reason)
    if any(isinstance(item, ssl.SSLError) for item in candidates):
        return "ssl_error"
    if any(isinstance(item, TimeoutError) for item in candidates):
        return "timeout"
    if any(isinstance(item, ConnectionResetError) for item in candidates):
        return "connection_reset"
    if any(isinstance(item, ConnectionAbortedError) for item in candidates):
        return "connection_aborted"
    if any(isinstance(item, ConnectionRefusedError) for item in candidates):
        return "connection_refused"
    if any(isinstance(item, OSError) for item in candidates):
        return "os_error"
    return type(error).__name__


def _network_error_text(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    if reason is not None and reason is not error:
        return f"{type(error).__name__}(reason={type(reason).__name__}: {reason})"
    return f"{type(error).__name__}: {error}"


def _redact_proxy_value(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname:
        return "<configured>"
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{hostname}{port}"


def _proxy_environment_summary() -> str:
    proxies = getproxies()
    if not proxies:
        return "none"
    return ",".join(
        f"{key}={_redact_proxy_value(str(value))}"
        for key, value in sorted(proxies.items())
    )


def _configure_process_no_proxy(portal_origin: str) -> tuple[str, ...]:
    portal_hostname = (urlparse(portal_origin).hostname or "").lower()
    additions = ["localhost", "127.0.0.1", "::1"]
    if portal_hostname:
        additions.append(portal_hostname)
    if portal_hostname == "szu.edu.cn" or portal_hostname.endswith(".szu.edu.cn"):
        additions.append(".szu.edu.cn")

    for variable_name in ("NO_PROXY", "no_proxy"):
        existing_values = [
            item.strip()
            for item in os.environ.get(variable_name, "").split(",")
            if item.strip()
        ]
        existing_lower = {item.lower() for item in existing_values}
        for value in additions:
            if value.lower() not in existing_lower:
                existing_values.append(value)
                existing_lower.add(value.lower())
        os.environ[variable_name] = ",".join(existing_values)
    return tuple(additions)


def _elapsed_milliseconds(started_at: float) -> float:
    return round((time.monotonic() - started_at) * 1000, 1)


def _log_runtime_context(
    logger: logging.Logger,
    settings: Settings,
    no_proxy_values: tuple[str, ...],
) -> None:
    logger.info(
        "监控进程启动: pid=%d python=%s openssl=%s platform=%s cwd=%s",
        os.getpid(),
        platform.python_version(),
        ssl.OPENSSL_VERSION,
        platform.platform(),
        Path.cwd(),
    )
    logger.info(
        "网络请求策略: route=direct proxy_handler=disabled portal=%s connectivity=%s proxies=%s no_proxy_added=%s",
        _target_description(settings.portal_origin),
        _target_description(settings.connectivity_test_url),
        _proxy_environment_summary(),
        ",".join(no_proxy_values),
    )


def configure_logger(verbose: bool = False) -> logging.Logger:
    log_path = Path(__file__).resolve().with_name("colszunet.log")
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


class ConnectivityChecker:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.opener = _build_direct_opener()
        self.request_index = 0
        self.expected_hostname = (
            urlparse(settings.connectivity_test_url).hostname or ""
        ).lower()

    def check(self) -> ConnectivityResult:
        self.request_index += 1
        request_id = self.request_index
        started_at = time.monotonic()
        target = _target_description(self.settings.connectivity_test_url)
        self.logger.info(
            "连通性请求开始: request_id=%d route=direct target=%s timeout=%.1fs",
            request_id,
            target,
            self.settings.connectivity_timeout_seconds,
        )
        request = Request(
            self.settings.connectivity_test_url,
            headers={
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "User-Agent": "colszunet-auto-login/1.0",
            },
        )
        try:
            with self.opener.open(
                request,
                timeout=self.settings.connectivity_timeout_seconds,
            ) as response:
                response.read(256)
                status_code = response.getcode()
                final_url = response.geturl()
        except HTTPError as exc:
            elapsed_ms = _elapsed_milliseconds(started_at)
            final_url = exc.geturl()
            self.logger.warning(
                "连通性请求结束: request_id=%d result=failure kind=http_error status=%d final_host=%s elapsed_ms=%.1f",
                request_id,
                exc.code,
                (urlparse(final_url).hostname or "").lower(),
                elapsed_ms,
            )
            return ConnectivityResult(
                success=False,
                detail=f"测试网站返回 HTTP {exc.code}",
                status_code=exc.code,
                final_url=final_url,
            )
        except (URLError, TimeoutError, OSError) as exc:
            elapsed_ms = _elapsed_milliseconds(started_at)
            error_kind = _network_error_kind(exc)
            error_text = _network_error_text(exc)
            self.logger.warning(
                "连通性请求结束: request_id=%d result=failure kind=%s error=%s elapsed_ms=%.1f",
                request_id,
                error_kind,
                error_text,
                elapsed_ms,
            )
            return ConnectivityResult(
                success=False,
                detail=f"测试网站请求失败[{error_kind}]: {error_text}",
            )

        final_hostname = (urlparse(final_url).hostname or "").lower()
        if status_code < 200 or status_code >= 400:
            elapsed_ms = _elapsed_milliseconds(started_at)
            self.logger.warning(
                "连通性请求结束: request_id=%d result=failure kind=http_status status=%d final_host=%s elapsed_ms=%.1f",
                request_id,
                status_code,
                final_hostname,
                elapsed_ms,
            )
            return ConnectivityResult(
                success=False,
                detail=f"测试网站返回 HTTP {status_code}",
                status_code=status_code,
                final_url=final_url,
            )
        if final_hostname != self.expected_hostname:
            elapsed_ms = _elapsed_milliseconds(started_at)
            self.logger.warning(
                "连通性请求结束: request_id=%d result=failure kind=unexpected_redirect status=%d final_host=%s expected_host=%s elapsed_ms=%.1f",
                request_id,
                status_code,
                final_hostname or "unknown-host",
                self.expected_hostname or "unknown-host",
                elapsed_ms,
            )
            return ConnectivityResult(
                success=False,
                detail=f"请求被重定向到 {final_hostname or '未知地址'}",
                status_code=status_code,
                final_url=final_url,
            )
        self.logger.info(
            "连通性请求结束: request_id=%d result=success status=%d final_host=%s elapsed_ms=%.1f",
            request_id,
            status_code,
            final_hostname,
            _elapsed_milliseconds(started_at),
        )
        return ConnectivityResult(
            success=True,
            detail="测试网站访问成功",
            status_code=status_code,
            final_url=final_url,
        )


class PortalClient:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger
        self.opener = _build_direct_opener()
        self.request_index = 0

    def login(self) -> bool:
        try:
            self.logger.info("开始执行校园网登录，等待门户分配认证地址")
            challenge_response = self._request_jsonp(
                "/cgi-bin/get_challenge",
                {"username": self.settings.full_username},
            )
            ip = self._extract_portal_ip(challenge_response)
            self.logger.info("门户已分配认证地址 %s", ip)
            token = challenge_response.get("challenge")
            if not isinstance(token, str) or not token:
                raise PortalError("认证接口没有返回 challenge")

            captcha_params = self._captcha_params(ip)
            password_hash = hmac_md5(self.settings.password, token)
            info = build_info(
                username=self.settings.full_username,
                password=self.settings.password,
                ip=ip,
                ac_id=self.settings.ac_id,
                token=token,
            )
            checksum_source = "".join(
                (
                    token,
                    self.settings.full_username,
                    token,
                    password_hash,
                    token,
                    self.settings.ac_id,
                    token,
                    ip,
                    token,
                    "200",
                    token,
                    "1",
                    token,
                    info,
                )
            )
            login_params: dict[str, object] = {
                "action": "login",
                "username": self.settings.full_username,
                "password": "{MD5}" + password_hash,
                "os": self.settings.device,
                "name": self.settings.platform,
                "nas_ip": "",
                "double_stack": 0,
                "chksum": sha1_hex(checksum_source),
                "info": info,
                "ac_id": self.settings.ac_id,
                "ip": ip,
                "n": 200,
                "type": 1,
            }
            login_params.update(captcha_params)
            login_response = self._request_jsonp(
                "/cgi-bin/srun_portal",
                login_params,
            )
            summary = summarise_response(login_response)
            if summary.error != "ok":
                self.logger.error(
                    "校园网登录接口返回失败: error=%s error_msg=%s ecode=%s code=%s message=%s",
                    summary.error,
                    summary.error_msg,
                    summary.ecode,
                    summary.code,
                    summary.message,
                )
                return False

            self.logger.info("校园网登录接口已接受认证请求，开始查询在线状态")
            return self._wait_until_online(ip)
        except CaptchaRequiredError as exc:
            self.logger.error("%s", exc)
            return False
        except PortalError as exc:
            self.logger.error("校园网登录流程失败: %s", exc)
            return False

    @staticmethod
    def _extract_portal_ip(response: dict[str, object]) -> str:
        value = response.get("client_ip")
        if not isinstance(value, str) or not value.strip():
            raise PortalError("认证接口没有返回门户分配的 IPv4 地址")
        portal_ip = value.strip()
        try:
            IPv4Address(portal_ip)
        except ValueError as exc:
            raise PortalError(
                f"认证接口返回了无效的门户 IPv4 地址: {portal_ip}"
            ) from exc
        return portal_ip

    def _captcha_params(self, ip: str) -> dict[str, str]:
        try:
            response = self._request_json(
                "/v2/srun_portal_captcha_image_info",
                {
                    "user_name": self.settings.username,
                    "ip": ip,
                },
            )
        except PortalRequestError as exc:
            if exc.status_code == 404:
                self.logger.info("验证码检查接口返回 404，继续执行登录")
                return {}
            raise

        captcha_required = response.get("code") == 0 and str(response.get("data")) == "1"
        if not captcha_required:
            return {}
        if not self.settings.captcha_id or not self.settings.captcha_value:
            raise CaptchaRequiredError(
                "校园网当前要求输入验证码，请在 config.ini 的 colszunet 配置节填写 captcha_id 与 captcha_value"
            )
        self.logger.info("检测到已填写验证码，继续执行登录")
        return {
            "captchaId": self.settings.captcha_id,
            "captchaVal": self.settings.captcha_value,
        }

    def _wait_until_online(self, ip: str) -> bool:
        for attempt in range(1, self.settings.login_verify_attempts + 1):
            try:
                response = self._request_jsonp(
                    "/cgi-bin/rad_user_info",
                    {"ip": ip},
                )
            except PortalError as exc:
                self.logger.warning(
                    "第 %d 次在线状态查询失败: %s",
                    attempt,
                    exc,
                )
            else:
                summary = summarise_response(response)
                online_ip = response.get("online_ip")
                if isinstance(online_ip, str) and online_ip.strip() == ip:
                    self.logger.info(
                        "校园网在线状态确认成功，认证地址为 %s",
                        ip,
                    )
                    return True
                self.logger.info(
                    "第 %d 次在线状态查询地址不匹配: requested_ip=%s online_ip=%s error=%s error_msg=%s",
                    attempt,
                    ip,
                    online_ip,
                    summary.error,
                    summary.error_msg,
                )

            if attempt < self.settings.login_verify_attempts:
                time.sleep(self.settings.login_verify_interval_seconds)
        self.logger.error("校园网登录接口已返回成功，但在线状态没有确认成功")
        return False

    def _request_jsonp(
        self,
        path: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        return self._request_payload(path, params, jsonp=True)

    def _request_json(
        self,
        path: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        return self._request_payload(path, params, jsonp=False)

    def _request_payload(
        self,
        path: str,
        params: dict[str, object],
        jsonp: bool,
    ) -> dict[str, object]:
        self.request_index += 1
        request_id = self.request_index
        started_at = time.monotonic()
        request_params = dict(params)
        if jsonp:
            request_params["callback"] = (
                "campusAutoLoginCallback_" + str(int(time.time() * 1000))
            )
        request_params["_"] = str(int(time.time() * 1000))
        url = urljoin(self.settings.portal_origin + "/", path.lstrip("/"))
        url = url + "?" + urlencode(request_params)
        request = Request(
            url,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": self.settings.portal_origin + "/srun_portal_pc",
                "User-Agent": "colszunet-auto-login/1.0",
            },
        )
        self.logger.info(
            "门户请求开始: request_id=%d route=direct path=%s timeout=%.1fs",
            request_id,
            path,
            self.settings.request_timeout_seconds,
        )
        try:
            with self.opener.open(
                request,
                timeout=self.settings.request_timeout_seconds,
            ) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as exc:
            self.logger.warning(
                "门户请求结束: request_id=%d result=failure kind=http_error path=%s status=%d elapsed_ms=%.1f",
                request_id,
                path,
                exc.code,
                _elapsed_milliseconds(started_at),
            )
            raise PortalRequestError(
                f"校园网接口返回 HTTP {exc.code}: {path}",
                status_code=exc.code,
                error_kind="http_error",
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            error_kind = _network_error_kind(exc)
            error_text = _network_error_text(exc)
            self.logger.warning(
                "门户请求结束: request_id=%d result=failure kind=%s path=%s error=%s elapsed_ms=%.1f",
                request_id,
                error_kind,
                path,
                error_text,
                _elapsed_milliseconds(started_at),
            )
            raise PortalRequestError(
                f"校园网接口请求失败[{error_kind}] {path}: {error_text}",
                error_kind=error_kind,
            ) from exc

        self.logger.info(
            "门户请求结束: request_id=%d result=success path=%s status=%d bytes=%d charset=%s elapsed_ms=%.1f",
            request_id,
            path,
            response.getcode(),
            len(body),
            charset,
            _elapsed_milliseconds(started_at),
        )
        try:
            text = body.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise PortalError(f"校园网接口响应编码无法识别: {path}") from exc
        return self._parse_payload(text, path)

    @staticmethod
    def _parse_payload(text: str, path: str) -> dict[str, object]:
        payload = text.lstrip("\ufeff").strip()
        try:
            result = json.loads(payload)
        except json.JSONDecodeError:
            left = payload.find("(")
            right = payload.rfind(")")
            if left < 0 or right <= left:
                raise PortalError(f"校园网接口响应不是有效 JSON 或 JSONP: {path}")
            try:
                result = json.loads(payload[left + 1:right])
            except json.JSONDecodeError as exc:
                raise PortalError(f"校园网接口 JSONP 内容无法解析: {path}") from exc

        if not isinstance(result, dict):
            raise PortalError(f"校园网接口响应顶层内容不是 JSON 对象: {path}")
        return result


def run_monitor(settings: Settings, logger: logging.Logger) -> None:
    checker = ConnectivityChecker(settings, logger)
    client = PortalClient(settings, logger)
    consecutive_failures = 0
    last_login_time = 0.0

    logger.info(
        "校园网自动登录监控已启动，连续失败阈值为 %d 次，测试地址为 %s，门户地址为 %s",
        settings.failure_threshold,
        _target_description(settings.connectivity_test_url),
        _target_description(settings.portal_origin),
    )

    while True:
        result = checker.check()
        if result.success:
            if consecutive_failures:
                logger.info("连通性已经恢复，连续失败计数清零")
            else:
                logger.info("连通性测试成功")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            logger.warning(
                "连通性测试失败：%s；连续失败次数为 %d/%d",
                result.detail,
                consecutive_failures,
                settings.failure_threshold,
            )

            if consecutive_failures >= settings.failure_threshold:
                elapsed = time.monotonic() - last_login_time
                if elapsed < settings.login_retry_cooldown_seconds:
                    logger.warning(
                        "登录重试冷却时间尚未结束，剩余约 %.1f 秒",
                        settings.login_retry_cooldown_seconds - elapsed,
                    )
                else:
                    last_login_time = time.monotonic()
                    logger.warning("达到连续失败阈值，开始执行校园网登录")
                    if client.login():
                        verification = checker.check()
                        if verification.success:
                            logger.info("登录后外部连通性确认成功")
                        else:
                            logger.error(
                                "登录流程完成，但外部连通性仍未恢复：%s",
                                verification.detail,
                            )
                    else:
                        logger.error("校园网登录失败，将在后续检查中重新尝试")
                    consecutive_failures = 0

        time.sleep(settings.probe_interval_seconds)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校园网自动登录监控")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "config.ini",
        help="配置文件路径",
    )
    parser.add_argument(
        "--section",
        default="colszunet",
        help="配置节名称",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="保留参数以兼容旧运行方式",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config.expanduser().resolve(), args.section)
    except ConfigurationError as exc:
        print(f"配置错误：{exc}")
        return 2

    no_proxy_values = _configure_process_no_proxy(settings.portal_origin)
    logger = configure_logger(args.verbose)
    _log_runtime_context(logger, settings, no_proxy_values)
    guard = SingleInstanceGuard(SINGLE_INSTANCE_MUTEX_NAME)
    if not guard.acquire():
        logger.warning(
            "检测到已有校园网监控进程，当前进程退出: mutex=%s pid=%d",
            SINGLE_INSTANCE_MUTEX_NAME,
            os.getpid(),
        )
        logging.shutdown()
        return SINGLE_INSTANCE_EXIT_CODE

    logger.info(
        "单实例锁获取成功: mutex=%s pid=%d",
        SINGLE_INSTANCE_MUTEX_NAME,
        os.getpid(),
    )
    try:
        run_monitor(settings, logger)
    except KeyboardInterrupt:
        logger.info("校园网自动登录监控已停止")
    except Exception:
        logger.exception("校园网自动登录监控出现未处理异常")
        raise
    finally:
        guard.release()
        logging.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
