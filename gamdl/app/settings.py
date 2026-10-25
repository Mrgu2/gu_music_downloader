from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
import uuid

from ..network import (
    ALLOWED_NETWORK_MODES,
    NETWORK_MODE_ERROR,
    PROXY_URL_ERROR,
    PROXY_URL_REQUIRED_ERROR,
    SOCKS_PROXY_SUPPORT_ERROR,
    normalize_network_config,
)
from .paths import AppPaths, restrict_permissions
from .wrapper_manager import (
    default_wrapper_decrypt_ip,
    normalize_wrapper_decrypt_ip,
    parse_wrapper_decrypt_ip,
)

CONCURRENCY_KEYS = {"download_concurrency", "conversion_concurrency"}


def validate_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise ValueError("并发数必须是 1 到 32 之间的整数。")
    return value


ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
ALLOWED_SONG_CODECS = {"aac-legacy", "aac", "alac", "atmos"}
BOOLEAN_KEYS = {
    "overwrite",
    "keep_original_files",
    "save_cover",
    "preserve_folder_structure",
    "browser_import_enabled",
    "setup_completed",
    "use_wrapper",
}
STRING_KEYS = {
    "network_mode",
    "output_path",
    "log_level",
    "language",
    "last_login_method",
    "open_file_application",
    "proxy_url",
    "song_codec",
    "download_output_format",
    "theme",
    "wrapper_decrypt_ip",
}
CLEARABLE_STRING_KEYS = {"open_file_application", "proxy_url"}
ALLOWED_THEMES = {"warm", "cool"}
DEFAULT_WRAPPER_DECRYPT_IP = default_wrapper_decrypt_ip()
WRAPPER_DECRYPT_IP_ERROR = (
    "Wrapper 解密地址格式无效，请使用类似 127.0.0.1:10022 的地址，或填写 1 到 55535 之间的端口号。"
)
WRAPPER_LOCALHOST_ONLY_ERROR = "桌面版 wrapper 地址必须是本机回环地址，例如 127.0.0.1:10022。"
METADATA_LANGUAGE_ERROR = (
    "元数据语言格式无效，请使用 zh-CN、ja-JP、zh-TW 这类语言代码，也支持 ja、en 这类简写。"
)
METADATA_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def normalize_metadata_language(language: str) -> str:
    normalized = str(language or "").strip().replace("_", "-")
    if not normalized or not METADATA_LANGUAGE_RE.fullmatch(normalized):
        raise ValueError(METADATA_LANGUAGE_ERROR)

    parts = normalized.split("-")
    formatted = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            formatted.append(part.title())
        elif len(part) in {2, 3} and part.isalpha():
            formatted.append(part.upper())
        else:
            formatted.append(part)
    return "-".join(formatted)


@dataclass
class AppSettings:
    output_path: str
    overwrite: bool = False
    save_cover: bool = True
    preserve_folder_structure: bool = True
    log_level: str = "INFO"
    language: str = "zh-CN"
    last_login_method: str | None = None
    open_file_application: str = ""
    browser_import_enabled: bool = True
    setup_completed: bool = False
    song_codec: str = "aac-legacy"
    download_output_format: str = "original"
    keep_original_files: bool = False
    download_concurrency: int = 8
    conversion_concurrency: int = 8
    theme: str = "warm"
    use_wrapper: bool = False
    wrapper_decrypt_ip: str = DEFAULT_WRAPPER_DECRYPT_IP
    network_mode: str = "auto"
    proxy_url: str = ""
    artist_auto_select: str = ""


class AppSettingsStore:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths()
        self.path = self.paths.settings_path

    def defaults(self) -> AppSettings:
        return AppSettings(output_path=str(self.paths.default_output_path))

    def validate_output_path(self, output_path: str) -> str:
        if not output_path.strip():
            raise ValueError("请先选择下载目录。")

        candidate = Path(output_path).expanduser()

        if candidate.exists() and not candidate.is_dir():
            raise ValueError("下载目录不是文件夹，请重新选择。")

        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"无法创建下载目录：{candidate}") from exc

        if not candidate.is_dir():
            raise ValueError("下载目录不是文件夹，请重新选择。")

        probe_path = candidate / f".apple-music-downloader-write-test-{uuid.uuid4().hex}"
        try:
            probe_path.write_text("", encoding="utf-8")
            probe_path.unlink()
        except OSError as exc:
            raise ValueError("这个目录没有写权限，请换一个可写位置。") from exc

        normalized = candidate if candidate.is_absolute() else candidate.absolute()
        return str(normalized)

    def _sanitize(self, payload: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            if key in CONCURRENCY_KEYS:
                try:
                    sanitized[key] = validate_concurrency(value)
                except ValueError:
                    pass
            elif key in BOOLEAN_KEYS and isinstance(value, bool):
                sanitized[key] = value
            elif key in STRING_KEYS and isinstance(value, str):
                stripped = value.strip()
                if stripped or key in CLEARABLE_STRING_KEYS:
                    sanitized[key] = stripped

        if "log_level" in sanitized and sanitized["log_level"] not in ALLOWED_LOG_LEVELS:
            sanitized.pop("log_level")
        if "song_codec" in sanitized and sanitized["song_codec"] not in ALLOWED_SONG_CODECS:
            sanitized.pop("song_codec")
        if "download_output_format" in sanitized and sanitized["download_output_format"] not in {"original", "flac", "mp3"}:
            sanitized.pop("download_output_format")
        if "theme" in sanitized and sanitized["theme"] not in ALLOWED_THEMES:
            sanitized.pop("theme")
        if "language" in sanitized:
            try:
                sanitized["language"] = normalize_metadata_language(sanitized["language"])
            except ValueError:
                sanitized.pop("language")
        if "network_mode" in sanitized and sanitized["network_mode"] not in ALLOWED_NETWORK_MODES:
            sanitized.pop("network_mode")
        if "wrapper_decrypt_ip" in sanitized:
            try:
                sanitized["wrapper_decrypt_ip"] = self._validate_wrapper_decrypt_ip(
                    sanitized["wrapper_decrypt_ip"]
                )
            except ValueError:
                sanitized.pop("wrapper_decrypt_ip")
        try:
            defaults = self.defaults()
            normalized_network = normalize_network_config(
                sanitized.get("network_mode", defaults.network_mode),
                sanitized.get("proxy_url", defaults.proxy_url),
            )
            sanitized["network_mode"] = normalized_network.mode
            sanitized["proxy_url"] = normalized_network.proxy_url
        except ValueError:
            sanitized.pop("network_mode", None)
            sanitized.pop("proxy_url", None)
        return sanitized

    @staticmethod
    def _validate_wrapper_decrypt_ip(wrapper_decrypt_ip: str) -> str:
        normalized = normalize_wrapper_decrypt_ip(wrapper_decrypt_ip)
        host, _port = parse_wrapper_decrypt_ip(normalized)
        if host.lower() == "localhost":
            return normalized
        try:
            parsed_host = ip_address(host)
        except ValueError as exc:
            raise ValueError(WRAPPER_LOCALHOST_ONLY_ERROR) from exc
        if not parsed_host.is_loopback:
            raise ValueError(WRAPPER_LOCALHOST_ONLY_ERROR)
        return normalized

    def validate_wrapper_decrypt_ip(self, wrapper_decrypt_ip: str) -> str:
        return self._validate_wrapper_decrypt_ip(wrapper_decrypt_ip)

    def load(self) -> AppSettings:
        defaults = self.defaults()
        if not self.path.exists():
            return defaults
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(raw, dict):
            return defaults

        payload = asdict(defaults)
        payload.update(self._sanitize(raw))
        return AppSettings(**payload)

    def save(self, payload: dict[str, Any]) -> AppSettings:
        for key in CONCURRENCY_KEYS & payload.keys():
            validate_concurrency(payload[key])
        if "download_output_format" in payload and payload["download_output_format"] not in {"original", "flac", "mp3"}:
            raise ValueError("下载后格式请选择保留原格式、FLAC 或 MP3。")
        if "network_mode" in payload or "proxy_url" in payload:
            current = self.load()
            try:
                normalized_network = normalize_network_config(
                    payload.get("network_mode", current.network_mode),
                    payload.get("proxy_url", current.proxy_url),
                )
            except ValueError as exc:
                message = str(exc)
                if message in {
                    NETWORK_MODE_ERROR,
                    PROXY_URL_REQUIRED_ERROR,
                    SOCKS_PROXY_SUPPORT_ERROR,
                }:
                    raise ValueError(message) from exc
                raise ValueError(PROXY_URL_ERROR) from exc
            payload = {
                **payload,
                "network_mode": normalized_network.mode,
                "proxy_url": normalized_network.proxy_url,
            }

        if isinstance(payload.get("wrapper_decrypt_ip"), str):
            wrapper_decrypt_ip = payload["wrapper_decrypt_ip"].strip()
            if wrapper_decrypt_ip:
                try:
                    self._validate_wrapper_decrypt_ip(wrapper_decrypt_ip)
                except ValueError as exc:
                    message = str(exc)
                    if message == WRAPPER_LOCALHOST_ONLY_ERROR:
                        raise ValueError(message) from exc
                    raise ValueError(WRAPPER_DECRYPT_IP_ERROR) from exc

        if isinstance(payload.get("language"), str):
            payload = {
                **payload,
                "language": normalize_metadata_language(payload["language"]),
            }

        settings = asdict(self.load())
        settings.update(self._sanitize(payload))
        settings["output_path"] = self.validate_output_path(settings["output_path"])
        self.paths.ensure()
        self.path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        restrict_permissions(self.path, 0o600)
        return AppSettings(**settings)
