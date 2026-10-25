from __future__ import annotations

import argparse
import json
import logging
import queue
import re
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import __version__
from .api.exceptions import ApiError
from .network import (
    NETWORK_MODE_ERROR,
    NetworkConfig,
    PROXY_URL_ERROR,
    PROXY_URL_REQUIRED_ERROR,
    SOCKS_PROXY_SUPPORT_ERROR,
    add_network_guidance,
    is_network_error,
    normalize_network_config,
)
from .app import (
    AppLogStore,
    AppPaths,
    AppSettingsStore,
    AuthManager,
    BrowserType,
    ConversionJobSpec,
    ConversionService,
    DesktopFileActions,
    DiagnosticsService,
    DownloadJob,
    DownloadService,
    JobCancelledError,
    WrapperManager,
    configure_app_logging,
    resolve_executable,
)
from .app.auth import TokenDeletionError
from .app.settings import AppSettings, normalize_metadata_language
from .desktop_runtime import detect_desktop_runtime
from .downloader import ArtistAutoSelect
from .downloader.constants import VALID_URL_PATTERN

logger = logging.getLogger("gamdl.app.web")

MAX_JSON_BODY_BYTES = 1024 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
API_REQUEST_TOKEN_HEADER = "X-Gamdl-Request-Token"
API_REQUEST_TOKEN_PLACEHOLDER = "__GAMDL_API_REQUEST_TOKEN__"
SESSION_BASE_PATH_PLACEHOLDER = "__GAMDL_SESSION_BASE_PATH__"
GUI_SETTINGS_DEFAULTS = AppSettingsStore(AppPaths()).defaults().__dict__
SUPPORTED_BROWSER_IMPORTS = [browser.value for browser in BrowserType]
INVALID_APPLE_MUSIC_AUTH_MESSAGE = "Apple Music 登录已失效，请重新登录。"
SUPPORTED_DOWNLOAD_KINDS = {
    "song",
    "album",
    "artist",
    "playlist",
    "library-playlist",
    "library-albums",
}
SUPPORTED_CONVERSION_FORMATS = {"flac", "mp3"}
DEFAULT_WRAPPER_DECRYPT_IP = "127.0.0.1:10022"
LOCAL_SESSION_TOKEN_ERROR = "需要有效本地会话令牌，请刷新页面重试。"
REQUEST_BODY_TOO_LARGE_ERROR = "请求体过大。"
INVALID_OPEN_APPLICATION_ERROR = "打开方式无效，请重新从菜单选择。"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)
SUPPORTED_ARTIST_AUTO_SELECT = {
    option.value: str(option)
    for option in (
        ArtistAutoSelect.TOP_SONGS,
        ArtistAutoSelect.MAIN_ALBUMS,
        ArtistAutoSelect.SINGLES_EPS,
        ArtistAutoSelect.ALL_ALBUMS,
    )
}


def validate_bind_host(host: str) -> str:
    normalized = str(host or "").strip()
    if not normalized:
        raise ValueError("Host 不能为空。")
    if normalized.lower() == "localhost":
        return normalized
    try:
        parsed = ip_address(normalized)
    except ValueError as exc:
        raise ValueError("Web GUI 仅允许绑定到本机回环地址。") from exc
    if not parsed.is_loopback:
        raise ValueError("Web GUI 仅允许绑定到本机回环地址。")
    return normalized


def _is_loopback_hostname(host: str) -> bool:
    normalized = str(host or "").strip()
    if not normalized:
        return False
    if normalized.lower() == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_ipv6_host(host: str) -> bool:
    try:
        return ip_address(str(host)).version == 6
    except ValueError:
        return False


def _format_host_for_url(host: str) -> str:
    normalized = str(host).strip()
    if _is_ipv6_host(normalized):
        return f"[{normalized}]"
    return normalized


def parse_url_input(text: str) -> list[str]:
    return [token.strip() for token in re.split(r"\s+", text) if token.strip()]


def classify_url(url: str) -> dict[str, Any]:
    match = VALID_URL_PATTERN.match(url)
    if not match:
        return {
            "url": url,
            "valid": False,
            "kind": "invalid",
            "label": "Invalid",
            "supported": False,
        }

    groups = match.groupdict()
    if groups.get("library_type") == "playlist":
        kind = "library-playlist"
    elif groups.get("library_type") == "albums":
        kind = "library-albums"
    elif groups.get("type") == "album" and groups.get("sub_id"):
        kind = "song"
    else:
        kind = groups.get("type") or groups.get("library_type") or "unknown"

    label_map = {
        "album": "Album",
        "artist": "Artist",
        "library-albums": "Library Albums",
        "library-playlist": "Library Playlist",
        "music-video": "Music Video",
        "playlist": "Playlist",
        "post": "Post",
        "song": "Song",
    }
    return {
        "url": url,
        "valid": True,
        "kind": kind,
        "label": label_map.get(kind, kind.title()),
        "storefront": groups.get("storefront") or groups.get("library_storefront"),
        "id": (
            groups.get("sub_id")
            if kind == "song" and groups.get("sub_id")
            else groups.get("id") or groups.get("library_id")
        ),
        "supported": kind in SUPPORTED_DOWNLOAD_KINDS,
    }


def build_download_command(payload: dict[str, Any]) -> list[str]:
    cmd = [payload.get("runner", "internal-download")]
    for key in (
        "output_path",
        "overwrite",
        "save_cover",
        "preserve_folder_structure",
        "save_playlist",
        "log_level",
        "language",
        "network_mode",
        "proxy_url",
        "song_codec",
        "download_output_format",
        "keep_original_files",
        "download_concurrency",
        "conversion_concurrency",
        "artist_auto_select",
        "use_wrapper",
        "wrapper_decrypt_ip",
    ):
        if key in payload and payload[key] not in {"", None}:
            cmd.append(f"{key}={payload[key]}")
    cmd.extend(payload.get("urls", []))
    return cmd


def build_conversion_command(payload: dict[str, Any]) -> list[str]:
    cmd = [payload.get("runner", "internal-convert")]
    for key in ("input_mode", "input_path", "output_path", "target_format", "overwrite"):
        if key in payload:
            cmd.append(f"{key}={payload[key]}")
    return cmd


def _network_config_from_settings(settings: AppSettings) -> NetworkConfig:
    return normalize_network_config(settings.network_mode, settings.proxy_url)


@dataclass
class Job:
    id: str
    urls: list[str]
    url_preview: list[dict[str, Any]]
    payload: dict[str, Any]
    command: list[str]
    kind: str = "download"
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error_category: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    _cancel_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
        compare=False,
    )

    def append_log(self, line: str) -> None:
        if line:
            self.logs.append(line)

    def request_cancel(self) -> bool:
        if self._cancel_event.is_set():
            return False
        self.cancel_requested = True
        self._cancel_event.set()
        return True

    def is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "urls": list(self.urls),
            "url_preview": [dict(item) for item in self.url_preview],
            "payload": dict(self.payload),
            "command": list(self.command),
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "logs": list(self.logs),
            "result": dict(self.result) if self.result else None,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "cancel_requested": self.cancel_requested,
        }


class JobManager:
    def __init__(
        self,
        auth_manager: AuthManager,
        settings_store: AppSettingsStore,
        paths: AppPaths,
    ) -> None:
        self.auth_manager = auth_manager
        self.settings_store = settings_store
        self.paths = paths
        self.jobs: dict[str, Job] = {}
        self.jobs_lock = threading.Lock()
        self.job_queue: queue.Queue[str] = queue.Queue()
        self.conversion_queue: queue.Queue[str] = queue.Queue()
        self._shutdown = False
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.conversion_worker = threading.Thread(target=self._worker_loop, args=(True,), daemon=True)
        self.worker.start()
        self.conversion_worker.start()

    def shutdown(self, timeout: float = 1.0) -> None:
        self._shutdown = True
        self.job_queue.put("")
        self.conversion_queue.put("")
        self.worker.join(timeout=timeout)
        self.conversion_worker.join(timeout=timeout)

    def create_job(self, payload: dict[str, Any]) -> Job:
        kind = str(payload.get("kind") or "download").strip().lower()
        if kind == "download":
            return self._create_download_job(payload)
        if kind == "convert":
            return self._create_conversion_job(payload)
        raise ValueError("不支持的任务类型。")

    def _create_download_job(self, payload: dict[str, Any]) -> Job:
        urls = parse_url_input(payload.get("url_text", ""))
        if not urls:
            raise ValueError("请至少输入一个 Apple Music 链接。")
        if not self.auth_manager.get_session_status(verify=False).connected:
            raise ValueError("请先完成 Apple Music 登录。")
        requested_codec = str(payload.get("song_codec") or "").strip().lower()
        use_wrapper = bool(payload.get("use_wrapper"))
        if requested_codec in {"alac", "atmos"} and not use_wrapper:
            raise ValueError("当前所选音质需要启用外部 wrapper。请先打开“启用 wrapper”，或切回 AAC。")
        url_preview = [classify_url(url) for url in urls]
        if any(not item["valid"] for item in url_preview):
            raise ValueError("包含无法识别的链接，请先修正。")
        if any(not item["supported"] for item in url_preview):
            unsupported = ", ".join(sorted({item["kind"] for item in url_preview if not item["supported"]}))
            raise ValueError(f"首版仅支持歌曲、专辑、歌单和艺术家，当前包含不支持的类型：{unsupported}")
        if any(item["kind"] == "artist" for item in url_preview):
            selected_artist_mode = str(payload.get("artist_auto_select") or "").strip()
            if selected_artist_mode not in SUPPORTED_ARTIST_AUTO_SELECT:
                raise ValueError("检测到艺术家链接，请先选择要下载的艺术家内容。")

        job_artist_auto_select = selected_artist_mode if any(
            item["kind"] == "artist" for item in url_preview
        ) else ""
        job_save_playlist = bool(payload.get("save_playlist"))
        job_retry_items = [dict(item) for item in payload.get("retry_items") or [] if isinstance(item, dict)]
        settings_payload = dict(payload)
        settings_payload.pop("artist_auto_select", None)
        settings_payload.pop("save_playlist", None)
        settings_payload.pop("retry_items", None)
        settings = self.settings_store.save(settings_payload)
        job_payload = {
            **settings.__dict__,
            "artist_auto_select": job_artist_auto_select,
            "save_playlist": job_save_playlist,
            "retry_items": job_retry_items,
        }
        job = Job(
            id=secrets.token_hex(8),
            kind="download",
            urls=urls,
            url_preview=url_preview,
            payload=job_payload,
            command=build_download_command({**job_payload, "urls": urls}),
        )
        with self.jobs_lock:
            self.jobs[job.id] = job
        self.job_queue.put(job.id)
        return job

    def _create_conversion_job(self, payload: dict[str, Any]) -> Job:
        input_mode = str(payload.get("input_mode") or "").strip()
        if input_mode not in {"file", "directory"}:
            raise ValueError("请选择输入模式：文件或目录。")

        input_path_raw = str(payload.get("input_path") or "").strip()
        if not input_path_raw:
            raise ValueError("请先选择输入文件或目录。")
        input_path = Path(input_path_raw).expanduser()
        if not input_path.exists():
            raise ValueError("输入路径不存在，请重新选择。")
        if input_mode == "file" and not input_path.is_file():
            raise ValueError("文件模式下请选择有效文件。")
        if input_mode == "directory" and not input_path.is_dir():
            raise ValueError("目录模式下请选择有效文件夹。")

        target_format = str(payload.get("target_format") or "").strip().lower()
        if target_format not in SUPPORTED_CONVERSION_FORMATS:
            raise ValueError("请选择有效的转换格式。")

        settings = self.settings_store.save(
            {
                "output_path": payload.get("output_path", ""),
                "overwrite": payload.get("overwrite", False),
            }
        )
        normalized_input = str(input_path.resolve())
        job_payload = {
            **settings.__dict__,
            "input_mode": input_mode,
            "input_path": normalized_input,
            "target_format": target_format,
        }
        job = Job(
            id=secrets.token_hex(8),
            kind="convert",
            urls=[],
            url_preview=[],
            payload=job_payload,
            command=build_conversion_command(job_payload),
        )
        with self.jobs_lock:
            self.jobs[job.id] = job
        self.conversion_queue.put(job.id)
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self.jobs_lock:
            return self.jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.jobs_lock:
            jobs = sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)
            return [job.to_dict() for job in jobs]

    def cancel_job(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if not job or job.status in {"completed", "failed", "cancelled"}:
            return False
        requested = job.request_cancel()
        if not requested:
            return False
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            job.append_log("任务已取消（尚未开始执行）。")
        else:
            job.append_log("已收到取消请求，取消中，当前文件完成后停止。")
        return True

    def _worker_loop(self, conversion: bool = False) -> None:
        while True:
            job_queue = self.conversion_queue if conversion else self.job_queue
            job_id = job_queue.get()
            try:
                if self._shutdown:
                    return
                job = self.get_job(job_id)
                if not job or job.status == "cancelled":
                    continue
                self._run_job(job)
            finally:
                job_queue.task_done()

    def _run_job(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        try:
            if job.kind == "download":
                self._run_download_job(job)
            elif job.kind == "convert":
                self._run_conversion_job(job)
            else:
                raise RuntimeError(f"未知任务类型：{job.kind}")
        except JobCancelledError as exc:
            job.status = "cancelled"
            job.return_code = 130
            if exc.result is not None:
                job.result = exc.result
            job.append_log("任务已停止。")
        except Exception as exc:
            error_category = _classify_error(exc)
            error_message = add_network_guidance(str(exc), error_category)
            job.append_log(error_message)
            job.status = "failed"
            job.return_code = 1
            job.error_category = error_category
            job.error_message = error_message
            if job.kind == "convert":
                job.result = {
                    "total_files": 0,
                    "converted_files": 0,
                    "skipped_files": 0,
                    "errors": 1,
                    "latest_media_path": None,
                    "latest_media_dir": None,
                }
            else:
                job.result = {
                    "downloaded_items": 0,
                    "skipped_items": 0,
                    "errors": 1,
                    "output_path": job.payload.get("output_path"),
                    "latest_media_path": None,
                    "latest_media_dir": None,
                }
        finally:
            job.finished_at = time.time()

    def _run_download_job(self, job: Job) -> None:
        token = self.auth_manager.get_media_user_token()
        service = DownloadService(
            media_user_token=token,
            log_callback=job.append_log,
            cancel_callback=job.is_cancel_requested,
            paths=self.paths,
            progress_callback=lambda result: setattr(job, "result", result),
        )
        try:
            result = service.run_sync(
                DownloadJob(
                    urls=job.urls,
                    output_path=job.payload["output_path"],
                    overwrite=job.payload["overwrite"],
                    save_cover=job.payload["save_cover"],
                    download_output_format=job.payload.get("download_output_format", "original"),
                    keep_original_files=job.payload.get("keep_original_files", False),
                    download_concurrency=job.payload.get("download_concurrency", 8),
                    conversion_concurrency=job.payload.get("conversion_concurrency", 8),
                    preserve_folder_structure=job.payload.get("preserve_folder_structure", True),
                    save_playlist=bool(job.payload.get("save_playlist")),
                    log_level=job.payload["log_level"],
                    language=job.payload.get("language", "zh-CN"),
                    song_codec=job.payload["song_codec"],
                    use_wrapper=job.payload["use_wrapper"],
                    wrapper_decrypt_ip=job.payload["wrapper_decrypt_ip"],
                    network_mode=job.payload.get("network_mode", "auto"),
                    proxy_url=job.payload.get("proxy_url", ""),
                    artist_auto_select=job.payload.get("artist_auto_select") or None,
                    retry_items=[dict(item) for item in job.payload.get("retry_items") or [] if isinstance(item, dict)],
                )
            )
        except Exception as exc:
            if _is_invalid_apple_music_auth_error(exc):
                try:
                    self.auth_manager.invalidate_session(INVALID_APPLE_MUSIC_AUTH_MESSAGE)
                except TokenDeletionError as delete_exc:
                    raise RuntimeError(str(delete_exc)) from delete_exc
                raise RuntimeError(INVALID_APPLE_MUSIC_AUTH_MESSAGE) from exc
            raise
        job.result = result.to_dict()
        job.status = "completed" if result.errors == 0 else "failed"
        job.return_code = 0 if result.errors == 0 else 1
        if result.errors:
            job.error_category = "download"
            job.error_message = "任务包含失败项，请查看日志。"

    def _run_conversion_job(self, job: Job) -> None:
        service = ConversionService(
            concurrency=job.payload.get("conversion_concurrency", 8),
            log_callback=job.append_log,
            cancel_callback=job.is_cancel_requested,
            progress_callback=lambda result: setattr(job, "result", result),
        )
        result = service.run(
            ConversionJobSpec(
                input_mode=job.payload["input_mode"],
                input_path=job.payload["input_path"],
                output_path=job.payload["output_path"],
                target_format=job.payload["target_format"],
                overwrite=job.payload["overwrite"],
            )
        )
        job.result = result.to_dict()
        job.status = "completed" if result.errors == 0 else "failed"
        job.return_code = 0 if result.errors == 0 else 1
        if result.errors:
            job.error_category = "conversion"
            job.error_message = "转换任务包含失败项，请查看日志。"


class RequestBodyTooLargeError(ValueError):
    pass


class WebGuiHandler(BaseHTTPRequestHandler):
    server_version = "gamdl-web/1.0"

    @staticmethod
    def _resolve_request_language(payload: dict[str, Any], settings: AppSettings) -> str:
        requested_language = payload.get("language")
        if isinstance(requested_language, str) and requested_language.strip():
            return normalize_metadata_language(requested_language)
        return settings.language

    def do_GET(self) -> None:
        if not self._verify_local_request():
            return
        parsed = urlparse(self.path)
        route_path = self._route_path(parsed.path)
        if route_path is None:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if route_path == "/":
            self._send_html()
            return
        if not self._verify_request_token():
            return
        if route_path == "/api/settings":
            settings = self.server.settings_store.load()
            wrapper_status = self.server.wrapper_manager.probe_status(
                settings.wrapper_decrypt_ip,
                _network_config_from_settings(settings),
            )
            self._send_json(
                {
                    "settings": settings.__dict__,
                    "wrapper_status": wrapper_status.__dict__,
                    "runtime": self.server.runtime().to_dict(),
                }
            )
            return
        if route_path == "/api/jobs":
            self._send_json({"jobs": self.server.job_manager.list_jobs()})
            return
        if route_path.startswith("/api/jobs/") and route_path.endswith("/open-with-options"):
            self._handle_job_open_with_options(route_path)
            return
        if route_path.startswith("/api/jobs/"):
            job_id = route_path.rsplit("/", 1)[-1]
            job = self.server.job_manager.get_job(job_id)
            if not job:
                self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(job.to_dict())
            return
        if route_path == "/api/auth/status":
            self._send_json({"session": self.server.auth_manager.get_session_status(verify=False).__dict__})
            return
        if route_path == "/api/logs":
            self._send_json(
                {
                    "channels": self.server.log_store.export(),
                    "folder_path": str(self.server.paths.logs_dir),
                }
            )
            return
        if route_path == "/api/about":
            settings = self.server.settings_store.load()
            self._send_json(
                {
                    "version": __version__,
                    "original_project_name": "Gamdl (Glomatico's Apple Music Downloader)",
                    "original_project_url": "https://github.com/glomatico/gamdl",
                    "modified_by": "@Mrgu2",
                    "modified_project_url": "https://github.com/Mrgu2/gu_music_downloader",
                    "download_safety_note": "请确保从 GitHub @Mrgu2 下载该软件，以保证软件安全、没有后门且来源可核验。",
                    "alac_max_spec": "24-bit / 192 kHz",
                    "alac_spec_note": "具体取决于歌曲本身是否提供对应规格。",
                    "supported_browser_imports": SUPPORTED_BROWSER_IMPORTS,
                    "supported_download_kinds": sorted(SUPPORTED_DOWNLOAD_KINDS),
                    "distribution_audio_default": "aac-legacy",
                    "alac_mode": "external-wrapper-only",
                    "runtime": self.server.runtime().to_dict(),
                    "wrapper_status": self.server.wrapper_manager.probe_status(
                        settings.wrapper_decrypt_ip,
                        _network_config_from_settings(settings),
                    ).__dict__,
                }
            )
            return
        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._verify_local_request():
            return
        parsed = urlparse(self.path)
        route_path = self._route_path(parsed.path)
        if route_path is None:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self._verify_post_request():
            return
        try:
            payload = self._read_json()
        except RequestBodyTooLargeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if route_path == "/api/preview":
            urls = parse_url_input(payload.get("url_text", ""))
            preview = [classify_url(url) for url in urls]
            self._send_json(
                {
                    "count": len(preview),
                    "preview": preview,
                    "summary": self._build_preview_summary(preview),
                }
            )
            return

        if route_path == "/api/settings":
            try:
                settings = self.server.settings_store.save(payload)
            except ValueError as exc:
                message = str(exc)
                if message in {
                    NETWORK_MODE_ERROR,
                    PROXY_URL_REQUIRED_ERROR,
                    PROXY_URL_ERROR,
                    SOCKS_PROXY_SUPPORT_ERROR,
                }:
                    category = "network"
                elif "元数据语言格式无效" in message:
                    category = "settings"
                else:
                    category = "filesystem"
                self._send_json({"error": str(exc), "category": category}, HTTPStatus.BAD_REQUEST)
                return
            self.server.set_log_level(settings.log_level)
            self._send_json(
                {
                    "settings": settings.__dict__,
                    "wrapper_status": self.server.wrapper_manager.probe_status(
                        settings.wrapper_decrypt_ip,
                        _network_config_from_settings(settings),
                    ).__dict__,
                    "runtime": self.server.runtime().to_dict(),
                }
            )
            return

        if route_path == "/api/settings/validate":
            try:
                output_path = self.server.settings_store.validate_output_path(
                    payload.get("output_path", "")
                )
            except ValueError as exc:
                self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"output_path": output_path})
            return

        if route_path == "/api/wrapper/start":
            settings = self.server.settings_store.load()
            requested_ip = payload.get("wrapper_decrypt_ip", settings.wrapper_decrypt_ip)
            try:
                requested_ip = self.server.settings_store.validate_wrapper_decrypt_ip(
                    str(requested_ip or "").strip()
                )
                network_config = normalize_network_config(
                    payload.get("network_mode", settings.network_mode),
                    payload.get("proxy_url", settings.proxy_url),
                )
                resolved_ip = self.server.wrapper_manager.ensure_running(requested_ip, network_config)
                wrapper_status = self.server.wrapper_manager.probe_status(resolved_ip, network_config)
            except ValueError as exc:
                category = (
                    "network"
                    if str(exc)
                    in {
                        NETWORK_MODE_ERROR,
                        PROXY_URL_REQUIRED_ERROR,
                        PROXY_URL_ERROR,
                        SOCKS_PROXY_SUPPORT_ERROR,
                    }
                    else "filesystem"
                )
                self._send_json(
                    {"error": str(exc), "category": category},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except RuntimeError as exc:
                self._send_json(
                    {
                        "error": str(exc),
                        "category": "wrapper",
                        "wrapper_status": self.server.wrapper_manager.probe_status(
                            requested_ip,
                            network_config,
                        ).__dict__,
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "resolved_ip": resolved_ip,
                    "wrapper_status": wrapper_status.__dict__,
                }
            )
            return

        if route_path == "/api/desktop/select-folder":
            self._handle_output_folder_selection()
            return

        if route_path == "/api/desktop/select-file":
            self._handle_existing_path_selection(
                picker=self.server.file_picker,
                unsupported_message="当前环境不支持原生文件选择器。",
                response_key="input_path",
                expected_kind="file",
            )
            return

        if route_path == "/api/desktop/select-input-folder":
            self._handle_existing_path_selection(
                picker=self.server.input_folder_picker,
                unsupported_message="当前环境不支持原生输入目录选择器。",
                response_key="input_path",
                expected_kind="directory",
            )
            return

        if route_path == "/api/jobs":
            try:
                job = self.server.job_manager.create_job(payload)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(job.to_dict(), HTTPStatus.CREATED)
            return

        if route_path.startswith("/api/jobs/") and route_path.endswith("/cancel"):
            job_id = route_path.split("/")[-2]
            cancelled = self.server.job_manager.cancel_job(job_id)
            if not cancelled:
                self._send_json(
                    {"error": "Job not found or not cancellable"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"ok": True})
            return

        if route_path.startswith("/api/jobs/") and route_path.endswith("/retry-failed"):
            self._handle_retry_failed_job(route_path)
            return

        if route_path.startswith("/api/jobs/") and route_path.endswith("/open-output"):
            self._handle_job_file_action(route_path, "open-output", payload)
            return

        if route_path.startswith("/api/jobs/") and route_path.endswith("/open-latest-file"):
            self._handle_job_file_action(route_path, "open-latest-file", payload)
            return

        if route_path.startswith("/api/jobs/") and route_path.endswith("/reveal-latest-file"):
            self._handle_job_file_action(route_path, "reveal-latest-file", payload)
            return

        if route_path == "/api/auth/import-browser":
            settings = self.server.settings_store.load()
            if not settings.browser_import_enabled:
                self._send_json(
                    {"error": "当前已关闭浏览器导入功能。请先在设置中重新开启。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            browser_name = payload.get("browser")
            if browser_name not in SUPPORTED_BROWSER_IMPORTS:
                self._send_json({"error": "Unsupported browser"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                session = self.server.auth_manager.import_from_browser(
                    BrowserType(browser_name),
                    language=self._resolve_request_language(payload, settings),
                )
            except Exception as exc:
                category = _classify_error(exc)
                if "元数据语言格式无效" in str(exc):
                    category = "settings"
                self._send_json(
                    {"error": add_network_guidance(str(exc), category), "category": category},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"session": session.__dict__})
            return

        if route_path == "/api/auth/login-webview":
            browser_name = payload.get("browser", BrowserType.CHROME.value)
            if browser_name not in SUPPORTED_BROWSER_IMPORTS:
                self._send_json({"error": "Unsupported browser"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                settings = self.server.settings_store.load()
                session = self.server.auth_manager.login_with_webview(
                    browser=BrowserType(browser_name),
                    language=self._resolve_request_language(payload, settings),
                )
            except Exception as exc:
                category = _classify_error(exc)
                if "元数据语言格式无效" in str(exc):
                    category = "settings"
                self._send_json(
                    {"error": add_network_guidance(str(exc), category), "category": category},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"session": session.__dict__})
            return

        if route_path == "/api/auth/logout":
            try:
                session = self.server.auth_manager.logout()
            except TokenDeletionError as exc:
                self._send_json(
                    {"error": str(exc), "category": "login"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"session": session.__dict__})
            return

        if route_path == "/api/diagnostics/export":
            bundle = self.server.diagnostics.export_bundle()
            self._send_json({"bundle_path": str(bundle)})
            return

        if route_path == "/api/logs/open-folder":
            try:
                self.server.file_actions.open_output(str(self.server.paths.logs_dir))
            except RuntimeError as exc:
                self._send_json(
                    {"error": str(exc), "category": "filesystem"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"ok": True, "folder_path": str(self.server.paths.logs_dir)})
            return

        self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求体长度无效。") from exc
        if not length:
            return {}
        if length > MAX_JSON_BODY_BYTES:
            raise RequestBodyTooLargeError(REQUEST_BODY_TOO_LARGE_ERROR)
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON。") from exc

    def _verify_local_request(self) -> bool:
        host_header = str(self.headers.get("Host") or "").strip()
        if not host_header:
            self._send_json({"error": "请求缺少 Host。"}, HTTPStatus.FORBIDDEN)
            return False

        hostname, _sep, _port = host_header.rpartition(":")
        candidate_host = hostname if hostname else host_header
        if candidate_host.startswith("[") and candidate_host.endswith("]"):
            candidate_host = candidate_host[1:-1]
        if not _is_loopback_hostname(candidate_host):
            self._send_json(
                {"error": "仅允许来自本机回环地址的请求。"},
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _verify_post_request(self) -> bool:
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(
                {"error": "仅支持 application/json 请求。"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return False
        return self._verify_request_token()

    def _verify_request_token(self) -> bool:
        request_token = str(self.headers.get(API_REQUEST_TOKEN_HEADER) or "").strip()
        if not secrets.compare_digest(request_token, self.server.api_request_token):
            self._send_json(
                {"error": LOCAL_SESSION_TOKEN_ERROR},
                HTTPStatus.FORBIDDEN,
            )
            return False
        return True

    def _route_path(self, request_path: str) -> str | None:
        session_base_path = self.server.session_base_path
        if request_path == session_base_path:
            return "/"
        base_with_trailing_slash = f"{session_base_path}/"
        if request_path.startswith(base_with_trailing_slash):
            suffix = request_path[len(session_base_path):]
            return suffix if suffix.startswith("/") else f"/{suffix}"
        return None

    def _send_security_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")

    def _send_html(self) -> None:
        html = (
            INDEX_HTML.replace(API_REQUEST_TOKEN_PLACEHOLDER, self.server.api_request_token)
            .replace(SESSION_BASE_PATH_PLACEHOLDER, self.server.session_base_path)
        )
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    @staticmethod
    def _build_preview_summary(preview: list[dict[str, Any]]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for item in preview:
            key = item["kind"]
            summary[key] = summary.get(key, 0) + 1
        return summary

    def _handle_output_folder_selection(self) -> None:
        if not self.server.folder_picker:
            self._send_json({"error": "当前环境不支持原生文件夹选择器。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            selected_path = self.server.folder_picker()
        except Exception as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return
        if not selected_path:
            self._send_json({"selected": False})
            return
        try:
            output_path = self.server.settings_store.validate_output_path(selected_path)
        except ValueError as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"selected": True, "output_path": output_path})

    def _handle_existing_path_selection(
        self,
        *,
        picker: Callable[[], str | None] | None,
        unsupported_message: str,
        response_key: str,
        expected_kind: str,
    ) -> None:
        if not picker:
            self._send_json({"error": unsupported_message}, HTTPStatus.BAD_REQUEST)
            return
        try:
            selected_path = picker()
        except Exception as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return
        if not selected_path:
            self._send_json({"selected": False})
            return
        try:
            normalized = self._validate_existing_input_path(selected_path, expected_kind)
        except ValueError as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"selected": True, response_key: normalized})

    @staticmethod
    def _validate_existing_input_path(path: str, expected_kind: str) -> str:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise ValueError("所选输入路径不存在。")
        if expected_kind == "file" and not candidate.is_file():
            raise ValueError("请选择有效输入文件。")
        if expected_kind == "directory" and not candidate.is_dir():
            raise ValueError("请选择有效输入目录。")
        return str(candidate.resolve())

    def _handle_job_file_action(
        self,
        path: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        job_id = path.split("/")[-2]
        job = self.server.job_manager.get_job(job_id)
        if not job:
            self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            if action == "open-output":
                output_path = str(job.payload.get("output_path") or "")
                if not output_path:
                    raise RuntimeError("当前任务没有可用的下载目录。")
                self.server.file_actions.open_output(output_path)
            else:
                latest_media_path = ((job.result or {}).get("latest_media_path") or "").strip()
                if not latest_media_path:
                    raise RuntimeError("当前任务没有可打开的下载文件。")
                if action == "open-latest-file":
                    choose_application = bool((payload or {}).get("choose_application"))
                    application = (
                        str((payload or {}).get("application_path") or (payload or {}).get("application") or "").strip()
                        or None
                    )
                    if choose_application:
                        application = self.server.file_actions.choose_application()
                        if application is None:
                            self._send_json({"cancelled": True})
                            return
                    elif application:
                        application = self._validate_open_application(latest_media_path, application)
                    self.server.file_actions.open_file(latest_media_path, application=application)
                else:
                    self.server.file_actions.reveal_file(latest_media_path)
        except RuntimeError as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"ok": True})

    def _validate_open_application(self, latest_media_path: str, application: str) -> str:
        settings = self.server.settings_store.load()
        options = self.server.file_actions.get_open_with_options(
            latest_media_path,
            configured_application=settings.open_file_application,
        )
        allowed_applications = {
            str(option.get("application_path") or "").strip()
            for option in options
            if option.get("kind") == "application"
        }
        if application not in allowed_applications:
            raise RuntimeError(INVALID_OPEN_APPLICATION_ERROR)
        return application

    def _handle_job_open_with_options(self, path: str) -> None:
        job_id = path.split("/")[-2]
        job = self.server.job_manager.get_job(job_id)
        if not job:
            self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return

        latest_media_path = ((job.result or {}).get("latest_media_path") or "").strip()
        if not latest_media_path:
            self._send_json(
                {"error": "当前任务没有可打开的下载文件。", "category": "filesystem"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        settings = self.server.settings_store.load()
        try:
            options = self.server.file_actions.get_open_with_options(
                latest_media_path,
                configured_application=settings.open_file_application,
            )
        except RuntimeError as exc:
            self._send_json({"error": str(exc), "category": "filesystem"}, HTTPStatus.BAD_REQUEST)
            return

        self._send_json(
            {
                "supported": True,
                "latest_media_path": latest_media_path,
                "options": options,
            }
        )

    def _handle_retry_failed_job(self, path: str) -> None:
        job_id = path.split("/")[-2]
        job = self.server.job_manager.get_job(job_id)
        if not job:
            self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return
        if job.kind != "download":
            self._send_json({"error": "仅下载任务支持重试失败项。"}, HTTPStatus.BAD_REQUEST)
            return
        if job.status not in {"completed", "failed"}:
            self._send_json({"error": "请等待当前任务结束后再重试失败项。"}, HTTPStatus.BAD_REQUEST)
            return

        retry_items = []
        retry_urls = []
        seen = set()
        for item in (job.result or {}).get("failed_items") or []:
            item = item or {}
            retry_target = item.get("retry_target")
            if isinstance(retry_target, dict):
                strategy = str(retry_target.get("strategy") or "").strip().lower()
                retry_url = str(retry_target.get("song_url") or retry_target.get("url") or "").strip()
                playlist_tags = retry_target.get("playlist_tags")
                if strategy == "playlist-track" and retry_url and isinstance(playlist_tags, dict):
                    dedupe_key = (
                        strategy,
                        retry_url,
                        playlist_tags.get("playlist_id"),
                        playlist_tags.get("playlist_track"),
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    retry_items.append(
                        {
                            "title": item.get("title"),
                            "kind": item.get("kind") or "song",
                            "source_url": item.get("source_url"),
                            "strategy": "playlist-track",
                            "song_url": retry_url,
                            "playlist_tags": dict(playlist_tags),
                        }
                    )
                    retry_urls.append(retry_url)
                    continue
                source_context = str(retry_target.get("source_context") or "").strip()
                artist_folder_name = str(retry_target.get("artist_folder_name") or "").strip()
                if strategy == "song-context" and retry_url and source_context:
                    dedupe_key = (
                        strategy,
                        retry_url,
                        source_context,
                        artist_folder_name,
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    retry_item = {
                        "title": item.get("title"),
                        "kind": item.get("kind") or "song",
                        "source_url": item.get("source_url"),
                        "strategy": "song-context",
                        "song_url": retry_url,
                        "source_context": source_context,
                    }
                    if artist_folder_name:
                        retry_item["artist_folder_name"] = artist_folder_name
                    retry_items.append(retry_item)
                    retry_urls.append(retry_url)
                    continue
                if strategy == "url" and retry_url:
                    dedupe_key = ("url", retry_url)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    retry_items.append(
                        {
                            "title": item.get("title"),
                            "kind": item.get("kind"),
                            "source_url": item.get("source_url"),
                            "strategy": "url",
                            "url": retry_url,
                        }
                    )
                    retry_urls.append(retry_url)
                    continue

            retry_url = str(item.get("retry_url") or "").strip()
            if retry_url:
                dedupe_key = ("url", retry_url)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                retry_items.append(
                    {
                        "title": item.get("title"),
                        "kind": item.get("kind"),
                        "source_url": item.get("source_url"),
                        "strategy": "url",
                        "url": retry_url,
                    }
                )
                retry_urls.append(retry_url)

        if not retry_items:
            self._send_json({"error": "当前任务没有可重试的失败歌曲。"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            retry_job = self.server.job_manager.create_job(
                {
                    **job.payload,
                    "url_text": "\n".join(retry_urls),
                    "retry_items": retry_items,
                }
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json(
            {
                "ok": True,
                "source_job_id": job.id,
                "retry_count": len(retry_items),
                "retry_job": retry_job.to_dict(),
            },
            HTTPStatus.CREATED,
        )


class WebGuiServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_cls,
        paths: AppPaths,
        log_store: AppLogStore | None,
        folder_picker: Callable[[], str | None] | None = None,
        file_picker: Callable[[], str | None] | None = None,
        input_folder_picker: Callable[[], str | None] | None = None,
        file_actions: DesktopFileActions | None = None,
    ) -> None:
        super().__init__(server_address, handler_cls)
        self.paths = paths
        self.log_store = log_store or AppLogStore()
        self.settings_store = AppSettingsStore(paths)
        self.auth_manager = AuthManager(paths)
        self.job_manager = JobManager(self.auth_manager, self.settings_store, paths)
        self.diagnostics = DiagnosticsService(paths, self.settings_store, self.log_store)
        self.wrapper_manager = WrapperManager()
        self.folder_picker = folder_picker
        self.file_picker = file_picker
        self.input_folder_picker = input_folder_picker
        self.file_actions = file_actions or DesktopFileActions()
        self.api_request_token = secrets.token_urlsafe(32)
        self.session_base_path = f"/{secrets.token_urlsafe(18)}"

    def server_close(self) -> None:
        self.job_manager.shutdown()
        super().server_close()

    def set_log_level(self, level: str) -> None:
        for logger_name in ("gamdl", "gamdl.app.auth", "gamdl.app.download", "gamdl.app.web"):
            logging.getLogger(logger_name).setLevel(level)

    def runtime(self):
        ffmpeg = resolve_executable("ffmpeg")
        return detect_desktop_runtime(
            folder_picker_supported=self.folder_picker is not None,
            file_picker_supported=self.file_picker is not None,
            file_actions_supported=self.file_actions.supported,
            conversion_supported=ffmpeg.available,
            conversion_message=(
                "ffmpeg 可用。"
                if ffmpeg.available
                else "未检测到 ffmpeg。"
            ),
        )

    def local_url(self, bind_host: str) -> str:
        formatted_host = _format_host_for_url(bind_host)
        return f"http://{formatted_host}:{self.server_address[1]}{self.session_base_path}/"

    def public_origin(self, bind_host: str) -> str:
        formatted_host = _format_host_for_url(bind_host)
        return f"http://{formatted_host}:{self.server_address[1]}"


class WebGuiServerIPv6(WebGuiServer):
    address_family = socket.AF_INET6


def create_server(
    host: str,
    port: int,
    *,
    paths: AppPaths | None = None,
    log_store: AppLogStore | None = None,
    folder_picker: Callable[[], str | None] | None = None,
    file_picker: Callable[[], str | None] | None = None,
    input_folder_picker: Callable[[], str | None] | None = None,
    file_actions: DesktopFileActions | None = None,
) -> WebGuiServer:
    validated_host = validate_bind_host(host)
    server_cls = WebGuiServerIPv6 if _is_ipv6_host(validated_host) else WebGuiServer
    return server_cls(
        (validated_host, port),
        WebGuiHandler,
        paths or AppPaths(),
        log_store or AppLogStore(),
        folder_picker=folder_picker,
        file_picker=file_picker,
        input_folder_picker=input_folder_picker,
        file_actions=file_actions,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Apple Music Downloader local web GUI")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Bind port")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't automatically open the browser",
    )
    args = parser.parse_args()

    paths = AppPaths()
    settings_store = AppSettingsStore(paths)
    log_store = AppLogStore()
    configure_app_logging(paths, log_store, settings_store.load().log_level)
    server = create_server(args.host, args.port, paths=paths, log_store=log_store)
    url = server.local_url(args.host)
    print(
        "Apple Music Downloader Web GUI listening on "
        f"{url}"
    )
    print(f"Loopback origin: {server.public_origin(args.host)}")
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if is_network_error(exc):
        return "network"
    if _is_invalid_apple_music_auth_error(exc):
        return "login"
    if "subscription" in message:
        return "session"
    if "permission" in message or "权限" in message:
        return "filesystem"
    if "login" in message or "token" in message or "登录" in message:
        return "login"
    return "download"


def _is_invalid_apple_music_auth_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ApiError) and current.status_code == 403:
            message = str(current).lower()
            if "invalid authentication" in message or "40300" in message:
                return True
        current = current.__cause__ or current.__context__
    return False


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apple Music Downloader</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23d7004e'/%3E%3Cpath d='M27 41V21l18-4v21' fill='none' stroke='%23fff' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3Ccircle cx='19' cy='43' r='6' fill='%23fff'/%3E%3Ccircle cx='45' cy='39' r='6' fill='%23fff'/%3E%3C/svg%3E">
  <style>
    :root {
      --bg: #f3f4f6;
      --bg-strong: #eceef2;
      --panel: rgba(255,255,255,.9);
      --panel-2: rgba(250,251,252,.98);
      --line: #e2e5ea;
      --line-strong: #d3d8e0;
      --ink: #15171a;
      --muted: #667085;
      --accent: #0a84ff;
      --accent-soft: #f2f4f7;
      --accent-soft-strong: #e7f0ff;
      --success: #0e7c4f;
      --warn: #b25d11;
      --danger: #a5332c;
      --shadow: 0 18px 48px rgba(15, 23, 42, .08);
      --shadow-soft: 0 4px 18px rgba(15, 23, 42, .04);
      --radius-xl: 22px;
      --radius-lg: 16px;
      --radius-md: 12px;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.9), transparent 22%),
        linear-gradient(180deg, #f8f9fb 0%, var(--bg) 100%);
      color: var(--ink);
      font-family: "SF Pro Display", "PingFang SC", -apple-system, BlinkMacSystemFont, sans-serif;
      transition: background .24s ease, color .24s ease;
    }
    .app-shell {
      min-height: 100vh;
      padding: 24px;
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      gap: 18px;
    }
    .sidebar, .main-panel {
      background: var(--panel);
      border: 1px solid rgba(255,255,255,.78);
      border-radius: 26px;
      box-shadow: var(--shadow);
      /* WKWebView treats blurred ancestors as containing blocks for fixed menus. */
    }
    .sidebar {
      padding: 16px 12px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .brand {
      padding: 12px 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .brand-head {
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }
    .brand-mark {
      width: 64px;
      height: 64px;
      flex: 0 0 64px;
      border-radius: 18px;
      box-shadow: 0 14px 28px rgba(93, 1, 41, .16);
      overflow: hidden;
    }
    .brand-copy {
      min-width: 0;
      display: grid;
      gap: 8px;
    }
    .brand h1 {
      margin: 0;
      max-width: 10ch;
      font-size: 26px;
      line-height: 1.02;
      letter-spacing: -.045em;
      text-wrap: balance;
    }
    .brand p {
      margin: 12px 0 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 13px;
      max-width: 24ch;
    }
    .nav {
      display: grid;
      gap: 4px;
    }
    .nav-btn {
      position: relative;
      border: 1px solid transparent;
      background: transparent;
      color: var(--ink);
      padding: 12px 14px 12px 20px;
      border-radius: 14px;
      font-size: 14px;
      font-weight: 590;
      text-align: left;
      cursor: pointer;
      transition: .18s ease;
    }
    .nav-btn::before {
      content: "";
      position: absolute;
      left: 10px;
      top: 50%;
      width: 4px;
      height: 18px;
      border-radius: 999px;
      background: transparent;
      transform: translateY(-50%) scaleY(.36);
      transition: transform .18s ease, background .18s ease;
    }
    .nav-btn:hover:not(.active) {
      background: var(--accent-soft);
      border-color: var(--line);
    }
    .nav-btn.active {
      background: rgba(255,255,255,.84);
      border-color: var(--line);
      color: var(--ink);
      box-shadow: 0 8px 18px rgba(15, 23, 42, .06);
    }
    .nav-btn.active::before {
      background: var(--accent);
      transform: translateY(-50%) scaleY(1);
    }
    .sidebar-foot {
      margin-top: auto;
      padding: 14px 14px 6px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.55;
    }
    .main-panel {
      padding: 26px 28px 30px;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }
    .topbar h2 {
      margin: 0;
      font-size: 30px;
      letter-spacing: -.05em;
    }
    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,.9);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: #c3c8d0;
    }
    .dot.success { background: var(--success); }
    .dot.warn { background: var(--warn); }
    .page {
      display: none;
      gap: 18px;
    }
    .page.active {
      display: grid;
    }
    .card {
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      overflow: hidden;
      box-shadow: var(--shadow-soft);
    }
    .card-head {
      padding: 18px 22px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
    }
    .card-head h3 {
      margin: 0;
      font-size: 20px;
      letter-spacing: -.03em;
    }
    .card-body {
      padding: 18px 22px 22px;
    }
    .lead {
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
      margin: 0;
    }
    .grid {
      display: grid;
      gap: 16px;
    }
    .grid.two {
      grid-template-columns: 1.25fr .95fr;
    }
    textarea, input, select {
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 14px;
      padding: 14px 16px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.78);
    }
    textarea:focus, input:focus, select:focus {
      outline: none;
      border-color: rgba(10,132,255,.55);
      box-shadow: 0 0 0 4px rgba(10,132,255,.12);
    }
    select {
      appearance: none;
      -webkit-appearance: none;
      -moz-appearance: none;
      background-color: #f3f4f6;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8' fill='none'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%23667085' stroke-width='1.75' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 16px center;
      background-size: 12px 8px;
      border-color: #e4e7ec;
      box-shadow: none;
      padding-right: 42px;
    }
    select:hover {
      background-color: #eef1f4;
      border-color: #d9dee6;
    }
    textarea {
      min-height: 190px;
      resize: vertical;
      line-height: 1.55;
    }
    .field {
      display: grid;
      gap: 8px;
    }
    #page-settings .grid.two {
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
    }
    #page-settings .field {
      min-width: 0;
      align-content: start;
    }
    #page-settings select {
      height: 48px;
      padding-top: 0;
      padding-bottom: 0;
      text-overflow: ellipsis;
    }
    #page-settings .field > .muted {
      line-height: 1.6;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      #page-settings .grid.two { grid-template-columns: 1fr; }
    }
    .field label {
      font-size: 13px;
      color: var(--muted);
    }
    .inline {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .path-picker {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: nowrap;
    }
    .path-picker input {
      flex: 1 1 auto;
    }
    .path-picker .btn {
      flex: 0 0 auto;
      white-space: nowrap;
    }
    .inline-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .btn {
      border: 1px solid var(--line-strong);
      background: #fff;
      color: var(--ink);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 12px 16px;
      border-radius: 16px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
      transition: .18s ease;
    }
    .btn:hover {
      transform: translateY(-1px);
      border-color: #c8ced8;
      background: #fbfcfd;
    }
    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      box-shadow: 0 10px 18px rgba(10, 132, 255, .18);
    }
    .btn.soft {
      background: var(--accent-soft);
      border-color: var(--line);
    }
    .btn.danger { color: var(--danger); }
    .btn[disabled] {
      cursor: not-allowed;
      opacity: .45;
      transform: none;
    }
    .menu-anchor {
      position: relative;
      display: inline-block;
    }
    .split-pill-anchor {
      position: relative;
      display: inline-flex;
      align-items: stretch;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--accent-soft);
      overflow: visible;
      transition: background-color .18s ease, border-color .18s ease, box-shadow .18s ease;
    }
    .split-pill-anchor:hover {
      border-color: #c8ced8;
      background: #fbfcfd;
    }
    .split-pill-anchor.open {
      border-color: rgba(10, 132, 255, .34);
      box-shadow: 0 10px 18px rgba(10, 132, 255, .12);
    }
    .split-pill-anchor.loading {
      border-color: rgba(10, 132, 255, .28);
      box-shadow: 0 8px 14px rgba(10, 132, 255, .08);
      cursor: progress;
    }
    .split-pill-anchor.disabled {
      opacity: .45;
    }
    .split-pill-anchor.disabled:hover {
      border-color: var(--line);
      background: var(--accent-soft);
      box-shadow: none;
    }
    .split-pill-main,
    .split-pill-toggle {
      border: 0;
      background: transparent;
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 48px;
      padding: 12px 16px;
      transition: background .18s ease, color .18s ease;
    }
    .split-pill-main {
      min-width: 108px;
      white-space: nowrap;
      border-radius: 16px 0 0 16px;
    }
    .split-pill-toggle {
      width: 46px;
      padding: 0;
      position: relative;
      border-radius: 0 16px 16px 0;
      flex: 0 0 46px;
    }
    .split-pill-toggle::before {
      content: "";
      position: absolute;
      left: 0;
      top: 10px;
      bottom: 10px;
      width: 1px;
      background: rgba(195, 201, 211, .95);
    }
    .split-pill-main:hover,
    .split-pill-toggle:hover {
      background: rgba(10, 132, 255, .08);
    }
    .split-pill-anchor.open .split-pill-toggle {
      background: rgba(10, 132, 255, .12);
    }
    .split-pill-anchor.loading .split-pill-toggle {
      background: rgba(10, 132, 255, .16);
    }
    .split-pill-main:focus-visible,
    .split-pill-toggle:focus-visible,
    .finder-menu-item:focus-visible {
      outline: 2px solid rgba(10, 132, 255, .35);
      outline-offset: -2px;
    }
    .split-pill-main[disabled],
    .split-pill-toggle[disabled] {
      cursor: not-allowed;
    }
    .split-pill-chevron {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 17px;
      line-height: 1;
      transform: translateX(1px);
    }
    .finder-menu {
      position: fixed;
      top: 0;
      left: 0;
      min-width: 0;
      max-width: min(360px, calc(100vw - 48px));
      max-height: min(360px, calc(100vh - 140px));
      overflow-y: auto;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
      padding: 8px;
      border: 1px solid rgba(195, 201, 211, .95);
      border-radius: 16px;
      background: rgba(255,255,255,.96);
      box-shadow:
        0 24px 52px rgba(15, 23, 42, .18),
        0 4px 14px rgba(15, 23, 42, .08);
      z-index: 200;
    }
    .finder-menu.measuring {
      visibility: hidden;
      pointer-events: none;
    }
    .finder-menu[hidden] {
      display: none;
    }
    .finder-menu-item {
      width: 100%;
      border: 0;
      background: transparent;
      text-align: left;
      border-radius: 12px;
      padding: 11px 13px;
      font: inherit;
      color: var(--ink);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .finder-menu-item:hover {
      background: rgba(10, 132, 255, .12);
    }
    .finder-menu-item[disabled] {
      cursor: not-allowed;
      opacity: .45;
      background: transparent;
    }
    .finder-menu-separator {
      height: 1px;
      margin: 6px 2px;
      background: rgba(210, 216, 225, .95);
    }
    .finder-menu-meta {
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .finder-menu-label {
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .stat {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px;
    }
    .stat .label {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .stat .value {
      margin-top: 10px;
      font-size: 28px;
      letter-spacing: -.04em;
    }
    .list {
      display: grid;
      gap: 10px;
    }
    .list-item {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
      display: grid;
      gap: 8px;
    }
    .list-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .muted { color: var(--muted); }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 7px 11px;
      font-size: 12px;
      background: #f5f7fa;
      border: 1px solid #e7ebf0;
      color: var(--muted);
    }
    .badge.success { color: var(--success); }
    .badge.warn { color: var(--warn); }
    .badge.danger { color: var(--danger); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: "SF Mono", "JetBrains Mono", monospace;
      font-size: 12px;
      line-height: 1.55;
      color: #27231f;
    }
    .log-scroll-panel {
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.74);
      overflow-y: auto;
      overflow-x: hidden;
      overscroll-behavior: contain;
      scrollbar-gutter: stable;
    }
    .job-log-scroll {
      height: 160px;
    }
    .app-log-scroll {
      max-height: min(58vh, 560px);
    }
    .log-scroll-panel::-webkit-scrollbar {
      width: 10px;
    }
    .log-scroll-panel::-webkit-scrollbar-track {
      background: rgba(226, 229, 234, .72);
      border-radius: 999px;
    }
    .log-scroll-panel::-webkit-scrollbar-thumb {
      background: rgba(125, 133, 149, .72);
      border-radius: 999px;
      border: 2px solid rgba(226, 229, 234, .72);
    }
    .log-scroll-panel::-webkit-scrollbar-thumb:hover {
      background: rgba(96, 105, 123, .82);
    }
    .segment {
      display: inline-flex;
      padding: 4px;
      border-radius: 999px;
      background: #f2f4f7;
      border: 1px solid var(--line);
    }
    .segment button {
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 9px 12px;
      border-radius: 999px;
      cursor: pointer;
      font: inherit;
    }
    .segment button.active {
      background: #fff;
      color: var(--ink);
      box-shadow: 0 4px 10px rgba(15, 23, 42, .08);
    }
    .preview-table {
      display: grid;
      gap: 8px;
    }
    .preview-row {
      display: grid;
      grid-template-columns: 110px 110px 1fr;
      gap: 12px;
      align-items: center;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
    }
    .toast {
      position: fixed;
      right: 26px;
      bottom: 26px;
      background: rgba(21, 21, 21, .92);
      color: #fff;
      padding: 14px 16px;
      border-radius: 16px;
      min-width: 240px;
      box-shadow: var(--shadow);
      opacity: 0;
      pointer-events: none;
      transform: translateY(10px);
      transition: .22s ease;
    }
    .toast.show {
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0);
    }
    .setup-overlay {
      position: fixed;
      inset: 0;
      padding: 32px;
      background: rgba(243, 245, 248, .72);
      backdrop-filter: blur(24px);
      z-index: 30;
      display: none;
      align-items: center;
      justify-content: center;
    }
    .setup-overlay.show {
      display: flex;
    }
    .setup-shell {
      width: min(1120px, 100%);
      display: grid;
      grid-template-columns: 0.92fr 1.08fr;
      gap: 18px;
    }
    .setup-hero,
    .setup-panel {
      background: rgba(255,255,255,.92);
      border: 1px solid rgba(255,255,255,.78);
      border-radius: 30px;
      box-shadow: var(--shadow);
      padding: 28px;
    }
    .setup-hero {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-height: 640px;
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.96), transparent 36%),
        linear-gradient(180deg, rgba(251,252,253,.98) 0%, rgba(243,245,248,.96) 100%);
    }
    .setup-kicker {
      font-size: 12px;
      color: var(--muted);
      letter-spacing: .14em;
      text-transform: uppercase;
    }
    .setup-hero h1 {
      margin: 14px 0 10px;
      font-size: 52px;
      line-height: .96;
      letter-spacing: -.06em;
    }
    .setup-hero p {
      margin: 0;
      color: var(--muted);
      line-height: 1.7;
      font-size: 15px;
      max-width: 34ch;
    }
    .setup-points {
      display: grid;
      gap: 12px;
      margin-top: 26px;
    }
    .setup-point {
      background: rgba(255,255,255,.8);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
    }
    .setup-point strong {
      display: block;
      font-size: 15px;
      margin-bottom: 4px;
    }
    .setup-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding-top: 20px;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }
    .setup-steps {
      display: grid;
      gap: 18px;
    }
    .setup-step {
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      display: grid;
      gap: 14px;
    }
    .setup-step-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .setup-step-index {
      width: 34px;
      height: 34px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 999px;
      background: var(--accent);
      color: #fff;
      font-size: 14px;
      box-shadow: 0 8px 18px rgba(10, 132, 255, .18);
    }
    .setup-actions {
      display: grid;
      gap: 12px;
      margin-top: 12px;
    }
    .setup-status {
      min-height: 22px;
      color: var(--muted);
      font-size: 13px;
    }
    .setup-complete {
      display: grid;
      gap: 10px;
      padding-top: 6px;
    }
    .setup-complete .btn[disabled] {
      cursor: not-allowed;
      opacity: .45;
      transform: none;
    }
    .guide-steps {
      display: grid;
      gap: 12px;
    }
    .guide-step {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 16px 18px;
      display: grid;
      gap: 10px;
    }
    .guide-step strong {
      font-size: 16px;
      letter-spacing: -.02em;
    }
    .guide-links {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .surface {
      position: relative;
      background: linear-gradient(180deg, rgba(255,255,255,.97) 0%, rgba(248,244,239,.94) 100%);
      border: 1px solid rgba(255,255,255,.92);
      border-radius: 28px;
      box-shadow: 0 20px 44px rgba(58, 35, 28, .10);
      overflow: hidden;
    }
    body {
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.94), transparent 22%),
        radial-gradient(circle at 82% 8%, rgba(255, 73, 120, .12), transparent 28%),
        linear-gradient(180deg, #f7f3ee 0%, #efe8df 100%);
    }
    .app-shell {
      width: min(1560px, calc(100vw - 32px));
      margin: 0 auto;
      min-height: 100vh;
      padding: 16px 0 24px;
      gap: 16px;
      grid-template-columns: 280px minmax(0, 1fr);
      align-items: start;
    }
    .sidebar, .main-panel {
      background: rgba(255,255,255,.76);
      border: 1px solid rgba(255,255,255,.88);
      border-radius: 30px;
      box-shadow: 0 22px 52px rgba(69, 38, 29, .12);
    }
    .sidebar {
      position: sticky;
      top: 16px;
      max-height: calc(100vh - 32px);
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.98), transparent 34%),
        linear-gradient(180deg, rgba(255,255,255,.92) 0%, rgba(247,241,234,.9) 100%);
      padding: 18px 14px;
      gap: 14px;
    }
    .brand {
      padding: 14px 14px 18px;
      border-bottom: 1px solid rgba(214, 205, 195, .58);
    }
    .brand h1 {
      font-size: 28px;
      line-height: 1.02;
    }
    .brand p,
    .sidebar-foot {
      color: #74685f;
    }
    .nav {
      gap: 5px;
    }
    .nav-btn {
      border-radius: 15px;
      font-weight: 600;
    }
    .nav-btn.active {
      background: rgba(255,255,255,.72);
      border-color: rgba(225, 212, 206, .88);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.86), 0 10px 20px rgba(94, 62, 53, .08);
    }
    .nav-btn.active::before {
      background: linear-gradient(180deg, #ff496a 0%, #d21b52 100%);
    }
    .main-panel {
      min-height: calc(100vh - 40px);
      padding: 28px 30px 30px;
      gap: 20px;
      background:
        radial-gradient(circle at top right, rgba(255,255,255,.92), transparent 28%),
        linear-gradient(180deg, rgba(255,255,255,.78) 0%, rgba(248,242,236,.72) 100%);
    }
    .topbar {
      padding: 2px 2px 4px;
    }
    .topbar > div:first-child {
      display: grid;
      gap: 6px;
    }
    .topbar h2 {
      font-size: 34px;
      line-height: .96;
    }
    .status-chip {
      background: rgba(255,255,255,.72);
      border-color: rgba(211, 216, 224, .84);
      color: #74685f;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.92);
    }
    .dot {
      box-shadow: 0 0 0 5px rgba(195, 200, 208, .18);
    }
    .page {
      gap: 16px;
    }
    .page.active {
      animation: page-rise .36s ease both;
    }
    .card {
      background: linear-gradient(180deg, rgba(255,255,255,.95) 0%, rgba(249,245,240,.92) 100%);
      border: 1px solid rgba(226, 229, 234, .9);
      border-radius: 24px;
      box-shadow: 0 12px 30px rgba(58, 35, 28, .06);
    }
    .card-head {
      padding: 20px 24px 14px;
      border-bottom-color: rgba(226, 229, 234, .85);
    }
    .card-body {
      padding: 20px 24px 24px;
    }
    .list-item,
    .stat,
    .preview-row {
      border-color: rgba(223, 216, 208, .88);
      background: rgba(255,255,255,.76);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.74);
    }
    .eyebrow {
      font-size: 11px;
      line-height: 1;
      letter-spacing: .18em;
      text-transform: uppercase;
      color: #8c7f74;
    }
    .download-shell {
      display: grid;
      grid-template-columns: minmax(0, 1.28fr) minmax(320px, .82fr);
      gap: 16px;
      align-items: start;
    }
    .composer-panel,
    .workspace-side > article,
    .preview-panel {
      min-width: 0;
    }
    .workspace-side {
      display: grid;
      gap: 16px;
    }
    .section-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 24px 26px 0;
    }
    .section-heading h3 {
      margin: 6px 0 0;
      font-size: 21px;
      letter-spacing: -.04em;
    }
    .section-body {
      display: grid;
      gap: 18px;
      padding: 20px 26px 26px;
    }
    .textarea-shell {
      padding: 16px 18px;
      border-radius: 24px;
      background: rgba(255,255,255,.62);
      border: 1px solid rgba(221, 213, 203, .88);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.74);
    }
    .textarea-shell textarea {
      min-height: 242px;
      padding: 0;
      border: 0;
      background: transparent;
      box-shadow: none;
    }
    .textarea-shell textarea:focus {
      box-shadow: none;
    }
    .action-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .quick-note-list {
      display: grid;
      gap: 10px;
    }
    .quick-note {
      display: grid;
      gap: 4px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(122, 36, 62, .04);
      border: 1px solid rgba(215, 197, 203, .86);
    }
    .quick-note strong {
      font-size: 15px;
      letter-spacing: -.02em;
    }
    .account-stack {
      display: grid;
      gap: 14px;
    }
    .account-card {
      display: grid;
      gap: 10px;
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,.72);
      border: 1px solid rgba(221, 213, 203, .88);
    }
    .readiness-list {
      display: grid;
      gap: 2px;
      padding: 6px 26px 26px;
    }
    .readiness-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      padding: 16px 0;
      border-top: 1px solid rgba(223, 216, 208, .82);
    }
    .readiness-row:first-child {
      border-top: 0;
      padding-top: 8px;
    }
    .readiness-row strong {
      display: block;
      margin-bottom: 4px;
      font-size: 15px;
      letter-spacing: -.02em;
    }
    .preview-panel .section-heading {
      padding-bottom: 0;
    }
    .download-bottom-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr);
      gap: 16px;
      align-items: start;
    }
    .preview-grid {
      display: grid;
      gap: 10px;
      padding: 20px 26px 26px;
    }
    .preview-row {
      grid-template-columns: 128px 132px 1fr;
      border-radius: 18px;
      padding: 14px 16px;
    }
    .empty-state {
      padding: 18px 18px 20px;
      border-radius: 20px;
      background: rgba(255,255,255,.68);
      border: 1px dashed rgba(213, 205, 196, .9);
      color: #74685f;
      line-height: 1.6;
    }
    .compact-job-list {
      display: grid;
      gap: 10px;
      padding: 20px 26px 26px;
    }
    .compact-job-item {
      display: grid;
      gap: 10px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(255,255,255,.76);
      border: 1px solid rgba(223, 216, 208, .88);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.74);
    }
    .compact-job-item pre {
      font-size: 11px;
      line-height: 1.45;
      max-height: 92px;
      overflow: auto;
    }
    .btn.primary {
      background: linear-gradient(135deg, #ff496a 0%, #d21b52 100%);
      border-color: transparent;
      box-shadow: 0 14px 24px rgba(210, 27, 82, .22);
    }
    .btn.soft {
      background: rgba(255,255,255,.68);
    }
    .badge {
      background: rgba(255,255,255,.74);
      border-color: rgba(226, 229, 234, .92);
    }
    .badge.success {
      background: rgba(18, 126, 85, .08);
    }
    .badge.warn {
      background: rgba(178, 93, 17, .08);
    }
    .badge.danger {
      background: rgba(165, 51, 44, .08);
    }
    body[data-theme="cool"] {
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.92), transparent 22%),
        linear-gradient(180deg, #f8f9fb 0%, #eef2f7 100%);
    }
    body[data-theme="cool"] .sidebar,
    body[data-theme="cool"] .main-panel {
      background: rgba(255,255,255,.88);
      border: 1px solid rgba(255,255,255,.86);
      box-shadow: 0 18px 48px rgba(15, 23, 42, .08);
    }
    body[data-theme="cool"] .sidebar {
      background:
        radial-gradient(circle at top left, rgba(255,255,255,.98), transparent 34%),
        linear-gradient(180deg, rgba(255,255,255,.96) 0%, rgba(243,246,250,.94) 100%);
    }
    body[data-theme="cool"] .main-panel {
      background:
        radial-gradient(circle at top right, rgba(255,255,255,.94), transparent 28%),
        linear-gradient(180deg, rgba(255,255,255,.84) 0%, rgba(244,247,251,.8) 100%);
    }
    body[data-theme="cool"] .brand,
    body[data-theme="cool"] .sidebar-foot {
      color: #667085;
    }
    body[data-theme="cool"] .nav-btn.active,
    body[data-theme="cool"] .btn.primary {
      border-color: #0a84ff;
      box-shadow: 0 10px 18px rgba(10, 132, 255, .18);
    }
    body[data-theme="cool"] .nav-btn.active {
      background: rgba(255,255,255,.9);
      border-color: #dbe2ea;
      color: #111827;
      box-shadow: 0 8px 18px rgba(15, 23, 42, .06);
    }
    body[data-theme="cool"] .nav-btn.active::before {
      background: linear-gradient(180deg, #0a84ff 0%, #2879ff 100%);
    }
    body[data-theme="cool"] .btn.primary {
      background: linear-gradient(135deg, #0a84ff 0%, #2879ff 100%);
    }
    body[data-theme="cool"] .nav-btn:hover:not(.active) {
      background: rgba(10, 132, 255, .06);
      border-color: rgba(148, 163, 184, .22);
    }
    body[data-theme="cool"] .btn.soft,
    body[data-theme="cool"] .split-pill-anchor,
    body[data-theme="cool"] .segment {
      background: #f2f4f7;
      border-color: #e2e5ea;
    }
    body[data-theme="cool"] .card,
    body[data-theme="cool"] .surface,
    body[data-theme="cool"] .list-item,
    body[data-theme="cool"] .stat,
    body[data-theme="cool"] .preview-row,
    body[data-theme="cool"] .compact-job-item,
    body[data-theme="cool"] .account-card,
    body[data-theme="cool"] .quick-note,
    body[data-theme="cool"] .empty-state {
      background: rgba(250,251,252,.98);
      border-color: #e2e5ea;
      box-shadow: 0 4px 18px rgba(15, 23, 42, .04);
    }
    body[data-theme="cool"] .textarea-shell,
    body[data-theme="cool"] .status-chip {
      background: rgba(255,255,255,.9);
      border-color: #e2e5ea;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.78);
      color: #667085;
    }
    body[data-theme="cool"] .badge {
      background: #f5f7fa;
      border-color: #e7ebf0;
      color: #667085;
    }
    body[data-theme="cool"] .badge.success { color: #0e7c4f; background: rgba(14,124,79,.08); }
    body[data-theme="cool"] .badge.warn { color: #b25d11; background: rgba(178,93,17,.08); }
    body[data-theme="cool"] .badge.danger { color: #a5332c; background: rgba(165,51,44,.08); }
    body[data-theme="cool"] textarea,
    body[data-theme="cool"] input,
    body[data-theme="cool"] select {
      border-color: #d3d8e0;
      background: #fff;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.78);
    }
    body[data-theme="cool"] select {
      background-color: #f3f4f6;
      border-color: #e4e7ec;
    }
    body[data-theme="cool"] .eyebrow,
    body[data-theme="cool"] .muted,
    body[data-theme="cool"] .lead,
    body[data-theme="cool"] .field label {
      color: #667085;
    }
    @keyframes page-rise {
      from {
        opacity: 0;
        transform: translateY(12px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    @media (max-width: 1320px) {
      .download-shell,
      .download-bottom-grid {
        grid-template-columns: 1fr;
      }
    }
    @media (max-width: 1180px) {
      .app-shell { grid-template-columns: 1fr; }
      .grid.two { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .setup-shell { grid-template-columns: 1fr; }
      .setup-hero { min-height: auto; }
      .sidebar {
        position: static;
        max-height: none;
      }
      .hero-stat-grid {
        grid-template-columns: repeat(4, minmax(0, 1fr));
      }
    }
    @media (max-width: 760px) {
      .app-shell {
        width: calc(100vw - 20px);
        padding-top: 10px;
      }
      .main-panel {
        padding: 22px 18px 20px;
      }
      .section-heading,
      .section-body,
      .readiness-list,
      .preview-grid {
        padding-left: 18px;
        padding-right: 18px;
      }
      .stats {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
      .preview-row {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body data-theme="warm">
  <div class="setup-overlay" id="setup-overlay">
    <div class="setup-shell">
      <section class="setup-hero">
        <div>
          <div class="setup-kicker">Welcome / Setup</div>
          <h1>完成首次设置</h1>
          <p>首次使用需要先选择下载目录并完成登录。</p>
          <div class="setup-points">
            <div class="setup-point">
              <strong>下载目录</strong>
              下载的歌曲、专辑和歌单都会保存到这里。你之后也可以在设置里修改。
            </div>
            <div class="setup-point">
              <strong>账号登录</strong>
              使用你自己的 Apple Music 账号登录。你可以选择浏览器辅助登录，也可以导入已存在的浏览器登录状态。
            </div>
            <div class="setup-point">
              <strong>完成设置后开始下载</strong>
              首次设置完成后，后续启动会直接进入主界面。
            </div>
          </div>
        </div>
        <div class="setup-meta">
          <span>Apple Music Downloader</span>
          <span id="setup-footer-status">等待完成首次配置</span>
        </div>
      </section>
      <section class="setup-panel">
        <div class="setup-steps">
          <div class="setup-step">
            <div class="setup-step-head">
              <div class="inline">
                <span class="setup-step-index">1</span>
                <div>
                  <strong>选择下载目录</strong>
                  <div class="muted">选择下载目录。</div>
                  <div class="muted">默认使用 AAC。</div>
                </div>
              </div>
              <span class="badge" id="setup-path-badge">待确认</span>
            </div>
            <div class="field">
              <label for="setup-output-path">下载目录</label>
              <div class="path-picker">
                <input id="setup-output-path" type="text" readonly />
                <button class="btn" id="setup-select-output-btn" type="button">选择文件夹</button>
              </div>
              <div class="muted" id="setup-output-path-copy">设置下载目录。</div>
            </div>
          </div>
          <div class="setup-step">
            <div class="setup-step-head">
              <div class="inline">
                <span class="setup-step-index">2</span>
                <div>
                  <strong>完成 Apple Music 登录</strong>
                  <div class="muted" id="setup-login-copy">登录 Apple Music 账号。</div>
                </div>
              </div>
              <span class="badge" id="setup-login-badge">待登录</span>
            </div>
            <div class="inline">
              <button class="btn primary" id="setup-login-webview-btn">浏览器辅助登录</button>
              <label class="sr-only" for="setup-browser-select">首次设置登录浏览器</label>
              <select id="setup-browser-select" aria-label="首次设置登录浏览器" style="max-width: 180px">
                <option value="chrome">Chrome</option>
                <option value="edge">Edge</option>
                <option value="brave">Brave</option>
                <option value="firefox">Firefox</option>
              </select>
              <button class="btn" id="setup-browser-import-btn">导入浏览器登录态</button>
            </div>
            <div class="setup-status" id="setup-login-status">未检测到可用会话。</div>
          </div>
          <div class="setup-complete">
            <button class="btn primary" id="complete-setup-btn" disabled>完成首次设置</button>
            <div class="setup-status" id="setup-complete-status">完成后进入主界面。</div>
          </div>
        </div>
      </section>
    </div>
  </div>

  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-head">
          <div class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 1024 1024" fill="none" xmlns="http://www.w3.org/2000/svg">
              <defs>
                <linearGradient id="brand-bg" x1="0%" y1="12%" x2="100%" y2="88%">
                  <stop offset="0%" stop-color="#ff2a3f"/>
                  <stop offset="52%" stop-color="#d7004e"/>
                  <stop offset="100%" stop-color="#5a0056"/>
                </linearGradient>
                <linearGradient id="brand-fg" x1="12%" y1="8%" x2="82%" y2="92%">
                  <stop offset="0%" stop-color="#fffefc"/>
                  <stop offset="65%" stop-color="#f7dbe8"/>
                  <stop offset="100%" stop-color="#efc3dc"/>
                </linearGradient>
              </defs>
              <rect width="1024" height="1024" rx="184" fill="url(#brand-bg)"/>
              <path d="M434 646V314L722 238V530" stroke="url(#brand-fg)" stroke-width="116" stroke-linecap="round" stroke-linejoin="round"/>
              <circle cx="314" cy="628" r="104" fill="url(#brand-fg)"/>
              <circle cx="626" cy="580" r="104" fill="url(#brand-fg)"/>
              <path d="M442 612H582V694H654L512 834L370 694H442Z" fill="url(#brand-fg)"/>
            </svg>
          </div>
          <div class="brand-copy">
            <h1>Apple Music Downloader</h1>
          </div>
        </div>
        <p>用于下载 Apple Music 歌曲、专辑、歌单和艺术家内容。</p>
      </div>
      <nav class="nav">
        <button class="nav-btn active" data-page="download">下载</button>
        <button class="nav-btn" data-page="account">账号</button>
        <button class="nav-btn" data-page="jobs">任务</button>
        <button class="nav-btn" data-page="convert">转换</button>
        <button class="nav-btn" data-page="logs">日志</button>
        <button class="nav-btn" data-page="settings">设置</button>
        <button class="nav-btn" data-page="about">关于</button>
      </nav>
      <div class="sidebar-foot">
        登录状态、日志和诊断信息只保存在当前这台设备上。
      </div>
    </aside>
    <main class="main-panel">
      <div class="topbar">
        <div>
          <div class="eyebrow">桌面应用</div>
          <h2 id="page-title">下载</h2>
        </div>
        <div class="status-chip"><span class="dot" id="session-dot"></span><span id="session-summary">正在读取登录状态</span></div>
      </div>

      <section class="page active" id="page-download">
        <div class="download-shell">
          <article class="surface composer-panel">
            <div class="section-heading">
              <div>
                <div class="eyebrow">新建任务</div>
                <h3>新建下载任务</h3>
              </div>
              <button class="btn soft" id="preview-btn">预览链接</button>
            </div>
            <div class="section-body">
              <p class="lead">支持歌曲、专辑、歌单和艺术家内容。</p>
              <div class="textarea-shell">
                <div class="field">
                  <label for="url-input">Apple Music 链接</label>
                  <textarea id="url-input" placeholder="每行一个链接，或者直接粘贴多个链接"></textarea>
                </div>
              </div>
              <div class="field" id="artist-download-field" hidden>
                <label for="artist-auto-select">艺术家下载内容</label>
                <select id="artist-auto-select" disabled>
                  <option value="">请选择艺术家内容</option>
                  <option value="top-songs">Top Songs</option>
                  <option value="main-albums">Main Albums</option>
                  <option value="singles-eps">Singles &amp; EPs</option>
                  <option value="all-albums">All Albums</option>
                </select>
                <div class="muted" id="artist-download-copy">仅当链接里包含 Artist 时才需要选择。</div>
              </div>
              <div class="field">
                <label for="save-playlist">歌单文件（.m3u8）</label>
                <select id="save-playlist">
                  <option value="false">不保存</option>
                  <option value="true">保存</option>
                </select>
                <div class="muted">仅影响本次任务；开启后会为 playlist 下载写入或补齐 `.m3u8`。</div>
              </div>
              <div class="action-row">
                <button class="btn primary" id="submit-job-btn">加入下载队列</button>
                <span class="muted" id="preview-summary">未预览</span>
              </div>
              <div class="quick-note-list">
                <div class="quick-note">
                  <strong>AAC</strong>
                  <span class="muted">可直接下载。</span>
                </div>
                <div class="quick-note">
                  <strong>ALAC / 杜比全景声</strong>
                  <span class="muted">先在设置里确认 wrapper 状态，再切高音质模式</span>
                </div>
              </div>
            </div>
          </article>

          <aside class="workspace-side">
            <article class="surface">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">账号会话</div>
                  <h3>当前账号</h3>
                </div>
              </div>
              <div class="section-body account-stack">
                <div class="account-card">
                  <div class="list-head">
                    <strong id="account-headline">未登录</strong>
                    <span class="badge" id="account-method">No Session</span>
                </div>
                <div class="muted" id="account-detail">显示当前账号状态。</div>
              </div>
              <div class="inline">
                <button class="btn primary" id="login-webview-btn">浏览器辅助登录</button>
                <label class="sr-only" for="browser-select">账号页登录浏览器</label>
                  <select id="browser-select" aria-label="账号页登录浏览器" style="max-width: 180px">
                  <option value="chrome">Chrome</option>
                  <option value="edge">Edge</option>
                  <option value="brave">Brave</option>
                    <option value="firefox">Firefox</option>
                  </select>
                  <button class="btn" id="browser-import-btn">导入浏览器登录态</button>
                </div>
              </div>
            </article>

            <article class="surface">
              <div class="section-heading">
                <div>
                  <div class="eyebrow">环境检查</div>
                  <h3>当前环境</h3>
                </div>
              </div>
              <div class="readiness-list">
                <div class="readiness-row">
                  <div>
                    <strong>下载目录</strong>
                    <div class="muted" id="workspace-path-detail">尚未设置下载目录。</div>
                  </div>
                  <span class="badge" id="workspace-path-badge-inline">待确认</span>
                </div>
                <div class="readiness-row">
                  <div>
                    <strong>Apple Music 登录</strong>
                    <div class="muted" id="workspace-session-detail">未检测到可用会话。</div>
                  </div>
                  <span class="badge" id="workspace-session-badge-inline">未登录</span>
                </div>
                <div class="readiness-row">
                  <div>
                    <strong>Wrapper 状态</strong>
                    <div class="muted" id="workspace-wrapper-detail">高音质和杜比全景声需要外部 wrapper。</div>
                  </div>
                  <span class="badge" id="workspace-wrapper-badge-inline">AAC 模式</span>
                </div>
                <div class="readiness-row">
                  <div>
                    <strong>FFmpeg 转换</strong>
                    <div class="muted" id="workspace-convert-detail">检测 ffmpeg 中</div>
                  </div>
                  <span class="badge" id="workspace-convert-badge-inline">检测中</span>
                </div>
              </div>
            </article>
          </aside>
        </div>

        <div class="download-bottom-grid">
          <article class="surface preview-panel">
            <div class="section-heading">
              <div>
                <div class="eyebrow">链接预览</div>
                <h3>链接预览</h3>
              </div>
            </div>
            <div class="preview-grid" id="preview-list">
              <div class="empty-state">显示链接类型和支持状态。</div>
            </div>
          </article>

          <article class="surface">
            <div class="section-heading">
              <div>
                <div class="eyebrow">最近任务</div>
                <h3>队列摘要</h3>
              </div>
            </div>
            <div class="compact-job-list" id="download-jobs-compact">
              <div class="empty-state">暂无任务。</div>
            </div>
          </article>
        </div>
      </section>

      <section class="page" id="page-account">
        <article class="card">
          <div class="card-head">
            <h3>账号与登录态</h3>
            <button class="btn danger" id="logout-btn">退出登录</button>
          </div>
          <div class="card-body grid">
            <p class="lead" id="account-login-copy">支持浏览器辅助登录和浏览器导入，登录状态保存在本机和 Keychain。</p>
            <div class="list" id="account-status-list"></div>
          </div>
        </article>
      </section>

      <section class="page" id="page-jobs">
        <div class="stats">
          <div class="stat"><div class="label">Queued</div><div class="value" id="stat-queued">0</div></div>
          <div class="stat"><div class="label">Running</div><div class="value" id="stat-running">0</div></div>
          <div class="stat"><div class="label">Completed</div><div class="value" id="stat-completed">0</div></div>
          <div class="stat"><div class="label">Failed</div><div class="value" id="stat-failed">0</div></div>
        </div>
        <article class="card">
          <div class="card-head">
            <h3>任务队列</h3>
            <button class="btn soft" id="refresh-jobs-btn">刷新</button>
          </div>
          <div class="card-body">
            <div class="list" id="jobs-list"><div class="muted">还没有任务。</div></div>
          </div>
        </article>
      </section>

      <section class="page" id="page-convert">
        <article class="card">
          <div class="card-head">
            <h3>FFmpeg 本地转换</h3>
            <button class="btn primary" id="submit-convert-btn">加入转换队列</button>
          </div>
          <div class="card-body grid">
            <p class="lead" id="convert-copy">使用 ffmpeg 转换本地文件。</p>
            <div class="grid two">
              <div class="field">
                <label for="convert-input-mode">输入路径类型</label>
                <select id="convert-input-mode">
                  <option value="file">文件</option>
                  <option value="directory">目录</option>
                </select>
              </div>
              <div class="field">
                <label for="convert-target-format">输出格式</label>
                <select id="convert-target-format">
                  <option value="flac">FLAC（无损）</option>
                  <option value="mp3">MP3（高质量有损）</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label for="convert-input-path">输入文件 / 目录</label>
              <div class="path-picker">
                <input id="convert-input-path" type="text" />
                <button class="btn" id="select-convert-file-btn" type="button">选择文件</button>
                <button class="btn" id="select-convert-input-folder-btn" type="button">选择目录</button>
              </div>
              <div class="muted" id="convert-input-copy">选择输入文件或目录。</div>
            </div>
            <div class="field">
              <label for="convert-output-path">输出目录</label>
              <div class="path-picker">
                <input id="convert-output-path" type="text" />
                <button class="btn" id="select-convert-output-btn" type="button">选择文件夹</button>
              </div>
              <div class="muted" id="convert-output-copy">选择输出目录。</div>
            </div>
            <div class="list-item">
              <div class="list-head">
                <strong>当前可用性</strong>
                <span class="badge" id="convert-runtime-badge">检测中</span>
              </div>
              <div class="muted" id="convert-runtime-copy">检测 ffmpeg 中</div>
            </div>
          </div>
        </article>
      </section>

      <section class="page" id="page-logs">
        <article class="card">
          <div class="card-head">
            <h3>应用日志</h3>
            <div class="inline">
              <div class="segment">
                <button class="active" data-log-channel="app">App</button>
                <button data-log-channel="auth">Auth</button>
                <button data-log-channel="download">Download</button>
              </div>
              <button class="btn" id="open-logs-folder-btn" type="button">打开日志目录</button>
            </div>
          </div>
          <div class="card-body">
            <div class="muted" id="logs-folder-path">日志目录：读取中…</div>
            <div class="log-scroll-panel app-log-scroll"><pre id="logs-output">正在加载日志…</pre></div>
          </div>
        </article>
      </section>

      <section class="page" id="page-settings">
        <article class="card">
          <div class="card-head">
            <h3>桌面版设置</h3>
            <button class="btn primary" id="save-settings-btn">保存设置</button>
          </div>
          <div class="card-body grid">
            <div class="field">
              <label for="output-path">下载目录</label>
              <div class="path-picker">
                <input id="output-path" type="text" readonly />
                <button class="btn" id="select-output-btn" type="button">选择文件夹</button>
              </div>
              <div class="muted" id="output-path-copy">设置下载目录。</div>
            </div>
            <div class="grid two">
              <div class="field">
                <label for="log-level">日志级别</label>
                <select id="log-level">
                  <option value="DEBUG">DEBUG</option>
                  <option value="INFO">INFO</option>
                  <option value="WARNING">WARNING</option>
                  <option value="ERROR">ERROR</option>
                </select>
              </div>
              <div class="field">
                <label for="folder-structure">下载文件夹结构</label>
                <select id="folder-structure">
                  <option value="true">保留文件夹结构（默认）</option>
                  <option value="false">舍弃文件夹结构（所有歌曲放在同一文件夹）</option>
                </select>
                <div class="muted">舍弃结构时使用“[歌曲序号]歌名-专辑名-歌手”命名（歌单使用歌单顺序，专辑使用专辑曲序，不足两位补零）；仅影响新任务，同名文件按覆盖设置处理。</div>
              </div>
            </div>
            <div class="grid two">
              <div class="field">
                <label for="download-output-format">下载后格式</label>
                <select id="download-output-format">
                  <option value="original">保留原格式</option>
                  <option value="flac">转换成 FLAC</option>
                  <option value="mp3">转换成 MP3</option>
                </select>
              </div>
              <div class="field">
                <label for="keep-original-files">转换后保留原文件</label>
                <select id="keep-original-files">
                  <option value="false">否，仅保留转换后的文件</option>
                  <option value="true">是，同时保留原文件</option>
                </select>
              </div>
            </div>
            <div class="muted">转换参数与转换菜单一致。保留原文件选项仅在转换格式时生效；转换失败时保留原文件，已有原文件不删除。</div>
            <div class="grid two">
              <div class="field">
                <label for="download-concurrency">下载并发数</label>
                <input id="download-concurrency" type="number" min="1" max="32" step="1" value="8" />
              </div>
              <div class="field">
                <label for="conversion-concurrency">转换并发数</label>
                <input id="conversion-concurrency" type="number" min="1" max="32" step="1" value="8" />
              </div>
            </div>
            <div class="muted">并发数可设为 1–32，默认 8；仅影响新任务。转换并发数同时用于下载后转换和转换菜单。</div>
            <div class="grid two">
              <div class="field">
                <label for="song-codec">音质</label>
                <select id="song-codec">
                  <option value="alac">ALAC（需要外部 wrapper）</option>
                  <option value="aac-legacy">AAC</option>
                  <option value="atmos">杜比全景声（需要外部 wrapper）</option>
                </select>
              </div>
              <div class="field">
                <label for="metadata-language-select">元数据语言</label>
                <select id="metadata-language-select">
                  <option value="zh-CN">简体中文（zh-CN，默认）</option>
                  <option value="zh-TW">繁体中文（台湾，zh-TW）</option>
                  <option value="zh-HK">繁体中文（香港，zh-HK）</option>
                  <option value="ja-JP">日语（ja-JP）</option>
                  <option value="en-US">英语（en-US）</option>
                  <option value="ko-KR">韩语（ko-KR）</option>
                  <option value="__custom__">自定义…</option>
                </select>
                <div class="field" id="metadata-language-custom-field" hidden>
                  <label for="metadata-language-custom">自定义语言代码</label>
                  <input id="metadata-language-custom" type="text" placeholder="例如 fr-FR" />
                </div>
                <div class="muted">影响后续新下载任务的歌曲、专辑、艺人等元数据语言。已下载文件不会自动改名。</div>
              </div>
            </div>
            <div class="grid two">
              <div class="field">
                <label for="theme-select">界面主题</label>
                <select id="theme-select">
                  <option value="warm">暖色</option>
                  <option value="cool">白蓝灰</option>
                </select>
              </div>
              <div class="field">
                <label for="save-cover">封面文件</label>
                <select id="save-cover">
                  <option value="true">保存</option>
                  <option value="false">不保存</option>
                </select>
              </div>
            </div>
            <div class="grid two">
              <div class="field">
                <label for="overwrite">覆盖已存在文件</label>
                <select id="overwrite">
                  <option value="false">关闭</option>
                  <option value="true">开启</option>
                </select>
              </div>
              <div class="field">
                <label for="browser-import-enabled">浏览器导入</label>
                <select id="browser-import-enabled">
                  <option value="true">允许</option>
                  <option value="false">关闭</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label for="network-mode">网络模式</label>
              <select id="network-mode">
                <option value="auto">自动（推荐）</option>
                <option value="direct">直连</option>
                <option value="custom">高级</option>
              </select>
              <div class="muted" id="network-mode-copy">如果你电脑开了代理工具，下载偶尔失败时，可以切到“直连”试试。</div>
            </div>
            <div class="field" id="proxy-url-field" hidden>
              <label for="proxy-url">代理地址</label>
              <input id="proxy-url" type="text" placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:7890" />
            </div>
            <div class="grid two">
              <div class="field">
                <label for="use-wrapper">启用 wrapper</label>
                <select id="use-wrapper">
                  <option value="true">开启</option>
                  <option value="false">关闭</option>
                </select>
              </div>
            </div>
            <div class="field">
              <label for="wrapper-decrypt-ip">Wrapper 解密地址</label>
              <input id="wrapper-decrypt-ip" type="text" />
            </div>
            <div class="field">
              <label>Wrapper 控制</label>
              <div class="inline-actions">
                <button class="btn soft" id="start-wrapper-btn" type="button">启动 wrapper</button>
                <span class="muted" id="wrapper-action-copy">如果已创建 wrapper-latest-10022 容器，可在这里手动拉起。</span>
              </div>
            </div>
            <div class="field" id="open-file-application-field">
              <label for="open-file-application">打开文件应用（可选）</label>
              <input id="open-file-application" type="text" placeholder="Windows 可填 exe 完整路径，例如 C:\\Program Files\\VLC\\vlc.exe" />
              <div class="muted">仅 Windows 使用。</div>
            </div>
            <p class="lead" id="wrapper-status-copy">高音质和杜比全景声需要外部 wrapper。</p>
          </div>
        </article>
      </section>

      <section class="page" id="page-about">
        <article class="card">
          <div class="card-head">
            <h3>关于应用</h3>
            <button class="btn" id="export-diagnostics-btn">导出诊断包</button>
          </div>
          <div class="card-body grid">
            <p class="lead">用于下载 Apple Music 歌曲、专辑、歌单和艺术家内容。</p>
            <div class="list" id="about-list"></div>
          </div>
        </article>
        <article class="card">
          <div class="card-head">
            <h3>ALAC / Wrapper 安装指南</h3>
            <div class="guide-links">
              <a class="btn soft" href="https://docs.docker.com/desktop/setup/install/mac-install/" target="_blank" rel="noreferrer">Docker 官方安装文档</a>
              <a class="btn soft" href="https://github.com/WorldObservationLog/wrapper/releases" target="_blank" rel="noreferrer">Wrapper Releases</a>
              <a class="btn soft" href="https://github.com/WorldObservationLog/wrapper" target="_blank" rel="noreferrer">Wrapper 源码仓库</a>
            </div>
          </div>
          <div class="card-body">
            <p class="muted">优先用 release 里的 zip 包，不建议普通用户直接 clone 源码。先按默认值照抄命令，先跑通再改。第一次安装时不要改容器名、不要改端口。</p>
            <div class="guide-steps">
              <div class="guide-step">
                <strong>1. 先安装 Docker Desktop</strong>
                <div class="muted">在 Mac 上先装好 Docker Desktop，再继续 ALAC。Apple Silicon 和 Intel 要下载不同安装包；官方文档还建议预留至少 4 GB RAM，Apple Silicon 机器建议装 Rosetta 2。</div>
                <pre>可选：Apple Silicon 机器执行
softwareupdate --install-rosetta</pre>
              </div>
              <div class="guide-step">
                <strong>2. 启动 Docker 并确认可用</strong>
                <div class="muted">把 Docker.app 拖进 Applications 后启动，等菜单栏鲸鱼图标显示运行正常，再执行下面命令。</div>
                <pre>docker version</pre>
              </div>
              <div class="guide-step">
                <strong>3. 普通用户直接复制这一段：自动下载、解压、构建</strong>
                <div class="muted">这一段会固定在 <code>~/wrapper-release</code> 目录里操作，不需要你自己理解当前目录。直接整段复制执行即可。为了减少 Apple Silicon 机器踩坑，这里默认直接带上 <code>--platform linux/amd64</code>。</div>
                <pre>mkdir -p ~/wrapper-release && cd ~/wrapper-release
rm -rf rootfs wrapper Dockerfile wrapper.zip
curl -fsSL https://api.github.com/repos/WorldObservationLog/wrapper/releases/latest \
  | grep browser_download_url \
  | grep 'Wrapper.x86_64.*\\.zip' \
  | cut -d '"' -f 4 \
  | xargs -n 1 curl -L -o wrapper.zip
unzip -o wrapper.zip
docker build --platform linux/amd64 -t wrapper-local .</pre>
              </div>
              <div class="guide-step">
                <strong>4. 如果上一步失败，再执行诊断</strong>
                <div class="muted">只有在第 3 步失败时才需要看这里。主要用来确认 release 包有没有完整下载和解压。</div>
                <pre>
pwd
ls -l
test -f ./wrapper && echo "wrapper ok" || echo "wrapper missing"
test -d ./rootfs && echo "rootfs ok" || echo "rootfs missing"
test -f ./Dockerfile && echo "dockerfile ok" || echo "dockerfile missing"
ls -l wrapper.zip</pre>
              </div>
              <div class="guide-step">
                <strong>5. 首次登录 wrapper</strong>
                <div class="muted">把下面命令里的 <code>your_apple_id@example.com:your_password</code> 改成你自己的 Apple Music 账号。这个命令会把账号口令暴露在 shell history 和进程列表里，建议只在临时 shell 里执行。这里默认直接带上 <code>--platform linux/amd64</code>。出现 2FA 时按终端提示完成，看到 <code>response type 6</code> 再结束。</div>
                <pre>docker run --rm -it \
  --platform linux/amd64 \
  -v "$PWD/rootfs/data:/app/rootfs/data" \
  -e args="-L your_apple_id@example.com:your_password -F -H 0.0.0.0 -D 10022 -M 20022" \
  wrapper-local</pre>
              </div>
              <div class="guide-step">
                <strong>6. 创建长期运行的 wrapper 容器</strong>
                <div class="muted">容器名请保持 <code>wrapper-latest-10022</code>。这样 App 才能自动识别，并在 Docker 已启动时帮你自动拉起。这里不要映射 account 端口；wrapper 在容器里仍监听 <code>0.0.0.0</code>，但宿主机端口只绑定到 <code>127.0.0.1</code>。这里默认直接带上 <code>--platform linux/amd64</code>。</div>
                <pre>docker run -d \
  --platform linux/amd64 \
  --name wrapper-latest-10022 \
  -v "$PWD/rootfs/data:/app/rootfs/data" \
  -p 127.0.0.1:10022:10022 \
  -p 127.0.0.1:20022:20022 \
  -e args="-H 0.0.0.0 -D 10022 -M 20022" \
  wrapper-local</pre>
              </div>
              <div class="guide-step">
                <strong>7. 自检端口，然后回到 App 切 ALAC</strong>
                <div class="muted">如果 10022 和 20022 都能连通，App 里把音质切到 ALAC，并保持解密地址为 <code>127.0.0.1:10022</code>。如果容器存在但端口不通，先看日志，不要急着重装。</div>
                <pre>docker ps --filter name=wrapper-latest-10022
nc -vz 127.0.0.1 10022
nc -vz 127.0.0.1 20022
docker logs --tail 30 wrapper-latest-10022</pre>
              </div>
              <div class="guide-step">
                <strong>8. 以后怎么启动</strong>
                <div class="muted">以后只要 Docker Desktop 已经打开，App 会优先检测现有端口，也会尝试启动这个容器。手动命令只需要这两个：</div>
                <pre>docker start wrapper-latest-10022
docker stop wrapper-latest-10022</pre>
              </div>
              <div class="guide-step">
                <strong>9. 常见报错先看这里</strong>
                <div class="muted"><code>无法启用 Rosetta 2</code> 或镜像架构不匹配：回到第 3、5、6 步，确认命令里保留了 <code>--platform linux/amd64</code>。<code>COPY ./wrapper /app: not found</code>：说明当前 release 解压目录不完整，先回到第 3、4 步重新下载并检查。<code>127.0.0.1:10022 connection refused</code>：说明容器没在运行，先执行 <code>docker start wrapper-latest-10022</code>，再回到第 7 步检查。如果 release 包下载失败，再回退到源码仓库方案。</div>
              </div>
            </div>
          </div>
        </article>
      </section>
    </main>
  </div>

  <div class="finder-menu" id="open-with-menu" hidden>
    <button class="finder-menu-item" type="button" disabled>
      <span>正在读取打开方式…</span>
    </button>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const API_REQUEST_TOKEN = '__GAMDL_API_REQUEST_TOKEN__';
    const SESSION_BASE_PATH = '__GAMDL_SESSION_BASE_PATH__';

    const state = {
      jobs: [],
      openWithMenuJobId: null,
      openWithMenuRequestId: 0,
      openWithOptionsCache: {},
      session: null,
      settings: null,
      runtime: null,
      wrapperStatus: null,
      preview: [],
      logs: {},
      logFolderPath: '',
      currentLogChannel: 'app',
      needsSetup: true,
      pages: {
        download: '下载',
        account: '账号',
        jobs: '任务',
        convert: '转换',
        logs: '日志',
        settings: '设置',
        about: '关于',
      },
    };

    async function api(path, options = {}) {
      const headers = new Headers(options.headers || {});
      headers.set('Content-Type', 'application/json');
      headers.set('X-Gamdl-Request-Token', API_REQUEST_TOKEN);
      const response = await fetch(`${SESSION_BASE_PATH}${path}`, {
        ...options,
        headers,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || '请求失败');
      }
      return data;
    }

    function showToast(message) {
      const toast = document.getElementById('toast');
      toast.textContent = message;
      toast.classList.add('show');
      window.clearTimeout(showToast.timer);
      showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 2400);
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function getJobById(jobId) {
      return state.jobs.find((job) => job.id === jobId) || null;
    }

    function getLatestMediaPath(jobId) {
      const job = getJobById(jobId);
      return job?.result?.latest_media_path || '';
    }

    function getCachedOpenWithOptions(jobId) {
      const latestMediaPath = getLatestMediaPath(jobId);
      const cached = state.openWithOptionsCache[jobId];
      if (!cached) return null;
      if (!latestMediaPath || cached.latestMediaPath !== latestMediaPath) {
        delete state.openWithOptionsCache[jobId];
        return null;
      }
      return cached.data;
    }

    function cacheOpenWithOptions(jobId, latestMediaPath, data) {
      if (!latestMediaPath) return;
      state.openWithOptionsCache[jobId] = {
        latestMediaPath,
        data,
      };
    }

    function setOpenWithMenuLoading(jobId, loading) {
      const anchor = document.querySelector(`.split-pill-anchor[data-job-id="${jobId}"]`);
      if (!anchor) return;
      anchor.classList.toggle('loading', loading);
    }

    function activatePage(page) {
      closeOpenWithMenus();
      document.querySelectorAll('.nav-btn').forEach((button) => {
        button.classList.toggle('active', button.dataset.page === page);
      });
      document.querySelectorAll('.page').forEach((node) => node.classList.remove('active'));
      document.getElementById(`page-${page}`).classList.add('active');
      document.getElementById('page-title').textContent = state.pages[page];
      if (page === 'jobs') {
        document.querySelectorAll('.job-log-scroll').forEach((panel) => {
          panel.scrollTop = panel._followLogs !== false ? panel.scrollHeight : (panel._savedScrollTop || 0);
        });
      }
    }

    function syncSetupOverlay() {
      const overlay = document.getElementById('setup-overlay');
      const completeButton = document.getElementById('complete-setup-btn');
      const pathBadge = document.getElementById('setup-path-badge');
      const loginBadge = document.getElementById('setup-login-badge');
      const footerStatus = document.getElementById('setup-footer-status');
      const completeStatus = document.getElementById('setup-complete-status');
      const pathValue = document.getElementById('setup-output-path').value.trim();
      const hasPath = Boolean(pathValue);
      const hasSession = Boolean(state.session && state.session.connected);

      if (hasPath) {
        pathBadge.textContent = '已确认';
        pathBadge.className = 'badge success';
      } else {
        pathBadge.textContent = '待确认';
        pathBadge.className = 'badge';
      }

      if (hasSession) {
        loginBadge.textContent = '已登录';
        loginBadge.className = 'badge success';
      } else {
        loginBadge.textContent = '待登录';
        loginBadge.className = 'badge';
      }

      state.needsSetup = !(state.settings && state.settings.setup_completed);
      overlay.classList.toggle('show', state.needsSetup);
      completeButton.disabled = !(hasPath && hasSession);

      if (!state.needsSetup) {
        footerStatus.textContent = '首次配置已完成';
        completeStatus.textContent = '你已经完成首次设置，后续可在设置页修改下载目录。';
        return;
      }

      if (hasPath && hasSession) {
        footerStatus.textContent = '可以完成首次设置';
        completeStatus.textContent = '现在可以进入主界面了。';
      } else if (!hasPath && !hasSession) {
        footerStatus.textContent = '等待目录和登录完成';
        completeStatus.textContent = '先确认下载目录，再完成登录。';
      } else if (!hasPath) {
        footerStatus.textContent = '等待目录确认';
        completeStatus.textContent = '还需要确认下载目录。';
      } else {
        footerStatus.textContent = '等待登录完成';
        completeStatus.textContent = '还需要完成 Apple Music 登录。';
      }
    }

    function badgeForStatus(jobOrStatus) {
      const status = typeof jobOrStatus === 'string' ? jobOrStatus : jobOrStatus?.status;
      const cancelRequested = typeof jobOrStatus === 'object' && Boolean(jobOrStatus?.cancel_requested);
      if (cancelRequested && status === 'running') {
        return ['取消中，当前文件完成后停止', 'badge warn'];
      }
      const statusMap = {
        queued: ['Queued', 'badge'],
        running: ['Running', 'badge warn'],
        completed: ['Completed', 'badge success'],
        failed: ['Failed', 'badge danger'],
        cancelled: ['Cancelled', 'badge'],
      };
      return statusMap[status] || [status, 'badge'];
    }

    function setNodeText(id, value) {
      const node = document.getElementById(id);
      if (node) {
        node.textContent = value;
      }
    }

    function setNodeBadge(id, value, tone = 'default') {
      const node = document.getElementById(id);
      if (!node) return;
      node.textContent = value;
      const toneClass = tone === 'success'
        ? 'badge success'
        : tone === 'warn'
          ? 'badge warn'
          : tone === 'danger'
            ? 'badge danger'
            : 'badge';
      node.className = toneClass;
    }

    function codecLabel(codec) {
      const labelMap = {
        'aac-legacy': 'AAC',
        alac: 'ALAC',
        atmos: '杜比全景声',
      };
      return labelMap[codec] || (codec || 'AAC');
    }

    function compactPath(path) {
      const value = String(path || '').trim();
      if (!value) return '未设置';
      const normalized = value.replace(/^\\/Users\\/[^/]+/, '~');
      if (normalized.length <= 34) {
        return normalized;
      }
      const parts = normalized.split('/');
      if (parts.length <= 3) {
        return normalized;
      }
      return `.../${parts.slice(-2).join('/')}`;
    }

    function applyTheme(theme) {
      const resolvedTheme = theme === 'cool' ? 'cool' : 'warm';
      document.body.dataset.theme = resolvedTheme;
      const selector = document.getElementById('theme-select');
      if (selector && selector.value !== resolvedTheme) {
        selector.value = resolvedTheme;
      }
    }

    function syncNetworkModeControls() {
      const networkMode = document.getElementById('network-mode')?.value || 'auto';
      const proxyField = document.getElementById('proxy-url-field');
      if (proxyField) {
        proxyField.hidden = networkMode !== 'custom';
      }
    }

    function syncMetadataLanguageControls() {
      const select = document.getElementById('metadata-language-select');
      const customField = document.getElementById('metadata-language-custom-field');
      const customInput = document.getElementById('metadata-language-custom');
      const isCustom = select?.value === '__custom__';
      if (customField) {
        customField.hidden = !isCustom;
      }
      if (customInput) {
        customInput.disabled = !isCustom;
      }
    }

    function setMetadataLanguageValue(value) {
      const select = document.getElementById('metadata-language-select');
      const customInput = document.getElementById('metadata-language-custom');
      const normalized = String(value || 'zh-CN').trim() || 'zh-CN';
      const presetValues = new Set(['zh-CN', 'zh-TW', 'zh-HK', 'ja-JP', 'en-US', 'ko-KR']);
      if (presetValues.has(normalized)) {
        select.value = normalized;
        customInput.value = '';
      } else {
        select.value = '__custom__';
        customInput.value = normalized;
      }
      syncMetadataLanguageControls();
    }

    function readMetadataLanguageValue() {
      const select = document.getElementById('metadata-language-select');
      if (select?.value !== '__custom__') {
        return select?.value || 'zh-CN';
      }
      return document.getElementById('metadata-language-custom')?.value.trim() || 'zh-CN';
    }

    function currentMetadataLanguage() {
      if (document.getElementById('metadata-language-select')) {
        return readMetadataLanguageValue();
      }
      return state.settings?.language || state.session?.language || 'zh-CN';
    }

    function loginMethodLabel(session) {
      const method = String(session?.login_method || '').trim();
      if (method === 'webview') return '浏览器辅助登录';
      if (method === 'browser-import') return '浏览器导入';
      if (method === 'saved-session') return '本地会话';
      return method || '未知方式';
    }

    function syncDownloadWorkspace() {
      const counts = { queued: 0, running: 0, completed: 0, failed: 0 };
      (state.jobs || []).forEach((job) => {
        if (counts[job.status] !== undefined) {
          counts[job.status] += 1;
        }
      });

      setNodeText('dashboard-queued', String(counts.queued));
      setNodeText('dashboard-running', String(counts.running));
      setNodeText('dashboard-completed', String(counts.completed));
      setNodeText('dashboard-failed', String(counts.failed));

      const outputPath = String(state.settings?.output_path || '').trim();
      const session = state.session;
      const runtime = state.runtime;
      const wrapperStatus = state.wrapperStatus;
      const codec = codecLabel(state.settings?.song_codec || 'aac-legacy');
      const codecSummary = codec !== 'AAC' && !wrapperStatus?.available
        ? `音质 ${codec}（需 wrapper）`
        : `音质 ${codec}`;
      const hasSession = Boolean(session && session.connected);
      const metadataLanguage = currentMetadataLanguage();
      const sessionLabel = hasSession
        ? (session.active_subscription ? '账号已连接' : '账号已登录')
        : '账号未连接';
      const sessionDetail = hasSession
        ? ((session.active_subscription ? '已连接 Apple Music，可直接开始下载。' : '已登录，但未检测到有效订阅') + ` 当前下载元数据语言：${metadataLanguage}。`)
        : (session?.last_error || '未检测到可用会话。');
      const sessionTone = hasSession ? (session.active_subscription ? 'success' : 'warn') : 'default';

      const wrapperAvailable = Boolean(wrapperStatus?.available);
      const wrapperBadge = wrapperAvailable
        ? (wrapperStatus.mode === 'docker' ? 'Docker 可用' : '外部可用')
        : (wrapperStatus?.mode === 'docker' ? '待启动' : 'AAC 模式');
      const wrapperTone = wrapperAvailable ? 'success' : (wrapperStatus?.mode === 'docker' ? 'warn' : 'default');

      const convertTone = runtime?.conversion_supported ? 'success' : 'danger';
      const convertBadge = runtime
        ? (runtime.conversion_supported ? '可用' : '不可用')
        : '检测中';

      setNodeText('download-summary-session', sessionLabel);
      setNodeText('download-summary-codec', codecSummary);
      setNodeText('download-summary-path', outputPath ? `目录 ${compactPath(outputPath)}` : '目录未设置');

      setNodeText('workspace-path-detail', outputPath ? outputPath : '尚未设置下载目录。');
      setNodeBadge('workspace-path-badge-inline', outputPath ? '已确认' : '待确认', outputPath ? 'success' : 'default');

      setNodeText('workspace-session-detail', sessionDetail);
      setNodeBadge('workspace-session-badge-inline', hasSession ? (session.active_subscription ? '已连接' : '已登录') : '未登录', sessionTone);

      setNodeText(
        'workspace-wrapper-detail',
        wrapperStatus?.message || '正式分发版默认使用 AAC；高音质模式依赖外部 wrapper。',
      );
      setNodeBadge('workspace-wrapper-badge-inline', wrapperBadge, wrapperTone);

      setNodeText(
        'workspace-convert-detail',
        runtime?.conversion_message || '检测 ffmpeg 中',
      );
      setNodeBadge('workspace-convert-badge-inline', convertBadge, runtime ? convertTone : 'default');
    }

    function renderSession(session) {
      state.session = session;
      const dot = document.getElementById('session-dot');
      const summary = document.getElementById('session-summary');
      const headline = document.getElementById('account-headline');
      const method = document.getElementById('account-method');
      const detail = document.getElementById('account-detail');
      const accountList = document.getElementById('account-status-list');

      if (!session || !session.connected) {
        dot.className = 'dot';
        summary.textContent = session?.last_error || 'Apple Music 未登录';
        headline.textContent = '未登录';
        method.textContent = '未登录';
        method.className = 'badge';
        detail.textContent = state.runtime?.native_login_supported
          ? '请先完成浏览器辅助登录或浏览器导入。'
          : '请先在本机浏览器登录 Apple Music，再导入浏览器登录态。';
        accountList.innerHTML = '<div class="list-item"><div class="muted">当前没有可用会话。</div></div>';
        document.getElementById('setup-login-status').textContent = session?.last_error || '未检测到可用会话。';
        syncDownloadWorkspace();
        syncSetupOverlay();
        return;
      }

      dot.className = session.active_subscription ? 'dot success' : 'dot warn';
      summary.textContent = session.active_subscription
        ? 'Apple Music 账号已连接'
        : '已登录，但未检测到有效订阅';
      headline.textContent = session.active_subscription ? '账号已连接' : '账号已登录';
      method.textContent = loginMethodLabel(session);
      method.className = 'badge success';
      detail.textContent = session.active_subscription
        ? '已连接 Apple Music，可直接开始下载。'
        : '已登录，但未检测到有效订阅';

      const items = [
        ['登录方式', loginMethodLabel(session)],
        ['浏览器来源', session.browser || (state.runtime?.native_login_supported ? '浏览器辅助登录' : '浏览器导入')],
        ['Storefront', String(session.storefront || 'unknown').toUpperCase()],
        ['下载元数据语言', currentMetadataLanguage()],
        ['订阅状态', session.active_subscription ? '有效' : '无效'],
      ];
      if (session.account_restrictions) {
        items.push(['内容限制', JSON.stringify(session.account_restrictions)]);
      }
      accountList.innerHTML = items.map(([label, value]) => `
        <div class="list-item">
          <div class="list-head"><strong>${escapeHtml(label)}</strong><span class="muted">${escapeHtml(value)}</span></div>
        </div>
      `).join('');
      document.getElementById('setup-login-status').textContent = session.active_subscription
        ? `已登录 ${session.storefront || 'unknown'}，订阅状态有效。`
        : '已登录，但未检测到有效订阅';
      syncDownloadWorkspace();
      syncSetupOverlay();
    }

    function renderPreview(data) {
      state.preview = data.preview || [];
      const summary = document.getElementById('preview-summary');
      summary.textContent = `${data.count} 个链接，${Object.entries(data.summary).map(([kind, count]) => `${kind} ${count}`).join(' / ') || '无'}`;
      const container = document.getElementById('preview-list');
      syncArtistDownloadOptions(state.preview);
      if (!data.preview.length) {
        container.innerHTML = '<div class="empty-state">显示链接类型和支持状态。</div>';
        return;
      }
      container.innerHTML = data.preview.map((item) => `
        <div class="preview-row">
          <span class="${item.supported ? 'badge success' : 'badge danger'}">${item.supported ? 'Supported' : 'Unsupported'}</span>
          <strong>${escapeHtml(item.label)}</strong>
          <span class="muted">${escapeHtml(item.url)}</span>
        </div>
      `).join('');
    }

    function updateJobLog(panel, lines) {
      const output = panel.querySelector('pre');
      const previous = panel._logLines || [];
      const firstRender = !panel._logLines;
      const follow = firstRender || (panel.offsetParent === null
        ? panel._followLogs !== false
        : panel.scrollHeight - panel.clientHeight - panel.scrollTop <= 8);
      if (firstRender) {
        panel.addEventListener('scroll', () => {
          if (panel.offsetParent !== null) {
            panel._followLogs = panel.scrollHeight - panel.clientHeight - panel.scrollTop <= 8;
            panel._savedScrollTop = panel.scrollTop;
          }
        }, { passive: true });
      }
      const top = panel.offsetParent === null ? (panel._savedScrollTop || 0) : panel.scrollTop;
      const next = Array.isArray(lines) ? lines : [];
      const unchanged = previous.length === next.length && previous.every((line, i) => line === next[i]);
      if (!firstRender && unchanged) return;
      const appended = previous.length <= next.length && previous.every((line, i) => line === next[i]);
      if (appended && previous.length) {
        output.appendChild(document.createTextNode('\\n' + next.slice(previous.length).join('\\n')));
      } else {
        output.textContent = next.join('\\n') || '暂无日志';
      }
      panel._logLines = next.slice();
      panel._followLogs = follow;
      panel.scrollTop = follow ? panel.scrollHeight : top;
      if (panel.offsetParent !== null) panel._savedScrollTop = panel.scrollTop;
    }

    function renderJobs(jobs) {
      state.jobs = jobs;
      const activeJobIds = new Set(jobs.map((job) => job.id));
      Object.keys(state.openWithOptionsCache).forEach((jobId) => {
        if (!activeJobIds.has(jobId)) {
          delete state.openWithOptionsCache[jobId];
        }
      });
      const counts = { queued: 0, running: 0, completed: 0, failed: 0 };
      jobs.forEach((job) => {
        if (counts[job.status] !== undefined) counts[job.status] += 1;
      });
      document.getElementById('stat-queued').textContent = counts.queued;
      document.getElementById('stat-running').textContent = counts.running;
      document.getElementById('stat-completed').textContent = counts.completed;
      document.getElementById('stat-failed').textContent = counts.failed;
      syncDownloadWorkspace();
      renderCompactJobs(jobs);

      const list = document.getElementById('jobs-list');
      if (!jobs.length) {
        list.innerHTML = '<div class="muted">还没有任务。</div>';
        return;
      }

      const cardsHtml = jobs.map((job) => {
        const [statusLabel, statusClass] = badgeForStatus(job);
        const result = job.result || {};
        const isConvert = job.kind === 'convert';
        const latestMediaPath = result.latest_media_path || '';
        const failedItems = Array.isArray(result.failed_items) ? result.failed_items : [];
        const retryableFailedItems = failedItems.filter((item) => Boolean(item?.retry_url));
        const failedSummary = failedItems.length
          ? `<div class="muted">失败项：${failedItems.slice(0, 3).map((item) => escapeHtml(item.title || item.source_url || 'Unknown')).join(' / ')}${failedItems.length > 3 ? ' / …' : ''}</div>`
          : '';
        const supportsFileActions = Boolean(state.runtime?.file_actions_supported);
        const canShowOutputAction = supportsFileActions && Boolean(job.payload?.output_path);
        const canShowMediaActions = supportsFileActions && Boolean(latestMediaPath);
        const errorCount = typeof result.errors === 'number' ? result.errors : (job.error_message ? 1 : 0);
        const retryActions = !isConvert && retryableFailedItems.length
          ? `<button class="btn soft" onclick="retryFailedItems('${job.id}')">重试失败歌曲（${retryableFailedItems.length}）</button>`
          : '';
        const fileActionHint = latestMediaPath
          ? `<div class="muted">最近成功文件：${escapeHtml(latestMediaPath)}</div>`
          : `<div class="muted">当前${isConvert ? '转换' : '下载'}任务没有成功输出的媒体文件。</div>`;
        const canCancel = job.status === 'queued' || job.status === 'running';
        const actions = canCancel
          ? `<button class="btn" onclick="cancelJob('${job.id}')" ${job.cancel_requested ? 'disabled' : ''}>${job.cancel_requested ? '取消中，当前文件完成后停止' : '取消'}</button>`
          : '';
        const primaryMeta = isConvert
          ? `
            <div>
              <strong>${job.payload.input_mode === 'directory' ? '目录转换' : '文件转换'}</strong>
              <div class="muted">${new Date(job.created_at * 1000).toLocaleString()}</div>
            </div>
          `
          : `
            <div>
              <strong>${job.urls.length} 个链接</strong>
              <div class="muted">${new Date(job.created_at * 1000).toLocaleString()}</div>
            </div>
          `;
        const detailCopy = isConvert
          ? `<div class="muted">输入：${escapeHtml(job.payload.input_path || '')}<br>输出：${escapeHtml(job.payload.output_path || '')}<br>格式：${escapeHtml((job.payload.target_format || '').toUpperCase())}</div>`
          : `<div class="muted">${job.urls.map((url) => escapeHtml(url)).join('<br>')}</div>`;
        const summaryBadges = isConvert
          ? `
              <span class="badge">处理中 ${result.active_conversions || 0}/${result.conversion_concurrency || job.payload?.conversion_concurrency || 8}</span>
              <span class="badge">成功 ${result.converted_files || 0}</span>
              <span class="badge">跳过 ${result.skipped_files || 0}</span>
              <span class="badge">文件 ${result.total_files || 0}</span>
              <span class="badge ${job.error_message ? 'danger' : ''}">错误 ${errorCount}</span>
            `
          : `
              <span class="badge">处理中 ${result.active_downloads || 0}/${result.download_concurrency || job.payload?.download_concurrency || 8}</span>
              <span class="badge">成功 ${result.downloaded_items || 0}</span>
              <span class="badge">跳过 ${result.skipped_items || 0}</span>
              <span class="badge ${job.error_message ? 'danger' : ''}">错误 ${errorCount}</span>
            `;
        const outputFormat = job.payload?.download_output_format || 'original';
        const downloadDone = (result.downloaded_items || 0) + (result.skipped_items || 0)
          + Math.max(0, (result.errors || 0) - (result.conversion_errors || 0));
        const conversionDone = (result.converted_items || 0) + (result.conversion_skipped || 0)
          + (result.conversion_errors || 0);
        const stageProgress = isConvert
          ? `<div class="muted">转换：已完成 ${(result.converted_files || 0) + (result.skipped_files || 0) + (result.errors || 0)} / ${result.total_files || 0}</div>
             <progress style="width:100%" value="${(result.converted_files || 0) + (result.skipped_files || 0) + (result.errors || 0)}" max="${Math.max(1, result.total_files || 0)}"></progress>`
          : `<div class="muted">歌曲信息准备：${result.prepared_items || 0} / ${result.preparation_total || 0}</div>
             <progress style="width:100%" value="${result.prepared_items || 0}" max="${Math.max(1, result.preparation_total || 0)}"></progress>
             <div class="muted">下载：已处理 ${downloadDone} / ${result.total_items || 0}，同时处理 ${result.active_downloads || 0}/${result.download_concurrency || job.payload?.download_concurrency || 8} 首</div>
             <progress style="width:100%" value="${downloadDone}" max="${Math.max(1, result.total_items || 0)}"></progress>
             <div class="muted">正在下载：${escapeHtml((result.downloading_titles || []).join(' / ') || '无')}</div>
             ${outputFormat !== 'original' ? `
               <div class="muted">${escapeHtml(outputFormat.toUpperCase())} 转换：成功 ${result.converted_items || 0}，跳过 ${result.conversion_skipped || 0}，失败 ${result.conversion_errors || 0}，待转换 ${Math.max(0, (result.conversion_total || 0) - conversionDone - (result.active_conversions || 0))}，同时处理 ${result.active_conversions || 0}/${result.conversion_concurrency || job.payload?.conversion_concurrency || 8} 首</div>
               <progress style="width:100%" value="${conversionDone}" max="${Math.max(1, result.total_items || 0, result.conversion_total || 0)}"></progress>
               <div class="muted">正在转换：${escapeHtml((result.converting_titles || []).join(' / ') || '无')}</div>
             ` : '<div class="muted">下载后保留原格式</div>'}`;
        const fileActions = canShowOutputAction
          ? `
            <div class="inline">
              <button class="btn soft" onclick="openJobOutput('${job.id}')">打开下载目录</button>
              <div class="menu-anchor split-pill-anchor ${latestMediaPath ? '' : 'disabled'}" data-job-id="${job.id}">
                <button class="split-pill-main" type="button" onclick="openJobFile('${job.id}')" ${canShowMediaActions ? '' : 'disabled title="当前任务没有可打开的下载文件"'}>打开文件</button>
                <button class="split-pill-toggle" type="button" aria-label="打开方式" aria-haspopup="menu" aria-expanded="false" onclick="toggleOpenWithMenu('${job.id}', event)" ${canShowMediaActions ? '' : 'disabled title="当前任务没有可打开的下载文件"'}><span class="split-pill-chevron" aria-hidden="true">›</span></button>
              </div>
              <button class="btn soft" onclick="revealJobFile('${job.id}')" ${canShowMediaActions ? '' : 'disabled title="当前任务没有可显示位置的下载文件"'}>显示文件位置</button>
            </div>
            ${fileActionHint}
          `
          : '';
        return `
          <div class="list-item" data-job-card="${escapeHtml(job.id)}">
            <div class="job-details">
            <div class="list-head">
              ${primaryMeta}
              <span class="${statusClass}">${statusLabel}</span>
            </div>
            <div class="inline">
              <span class="badge">${isConvert ? '转换' : '下载'}</span>
            </div>
            ${detailCopy}
            <div class="inline">
              ${summaryBadges}
              ${actions}
              ${retryActions}
            </div>
            ${stageProgress}
            ${failedSummary}
            ${fileActions}
            </div>
            <div class="log-scroll-panel job-log-scroll">
              <pre></pre>
            </div>
          </div>
        `;
      }).join('');
      const template = document.createElement('template');
      template.innerHTML = cardsHtml;
      const existing = new Map(Array.from(list.children).map((card) => [card.dataset.jobCard, card]));
      const jobById = new Map(jobs.map((job) => [job.id, job]));
      Array.from(list.children).forEach((card) => {
        if (!jobById.has(card.dataset.jobCard)) card.remove();
      });
      Array.from(template.content.children).forEach((incoming, index) => {
        const id = incoming.dataset.jobCard;
        const card = existing.get(id) || incoming;
        if (card !== incoming) {
          const details = card.querySelector('.job-details');
          const nextDetails = incoming.querySelector('.job-details');
          if (details.innerHTML !== nextDetails.innerHTML) details.innerHTML = nextDetails.innerHTML;
        }
        if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
        updateJobLog(card.querySelector('.job-log-scroll'), jobById.get(id).logs);
      });
    }

    function renderCompactJobs(jobs) {
      const container = document.getElementById('download-jobs-compact');
      if (!container) return;
      if (!jobs.length) {
        container.innerHTML = '<div class="empty-state">暂无任务。</div>';
        return;
      }

      const latestJobs = jobs
        .slice()
        .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
        .slice(0, 3);

      container.innerHTML = latestJobs.map((job) => {
        const [statusLabel, statusClass] = badgeForStatus(job);
        const isConvert = job.kind === 'convert';
        const title = isConvert
          ? (job.payload.input_mode === 'directory' ? '目录转换' : '文件转换')
          : `${job.urls.length} 个链接`;
        const meta = isConvert
          ? `${(job.payload.target_format || '').toUpperCase()} · ${escapeHtml(job.payload.input_path || '')}`
          : `${job.urls.slice(0, 2).map((url) => escapeHtml(url)).join('<br>')}${job.urls.length > 2 ? '<br>…' : ''}`;
        const logTail = (job.logs || []).slice(-4).join('\\n') || '暂无日志';
        return `
          <div class="compact-job-item">
            <div class="list-head">
              <strong>${title}</strong>
              <span class="${statusClass}">${statusLabel}</span>
            </div>
            <div class="muted">${new Date(job.created_at * 1000).toLocaleString()}</div>
            <div class="muted">${meta}</div>
            <pre>${escapeHtml(logTail)}</pre>
          </div>
        `;
      }).join('');
    }

    function renderLogs(data) {
      const channels = data.channels || {};
      state.logs = channels;
      state.logFolderPath = data.folder_path || '';
      const output = document.getElementById('logs-output');
      const scroller = output ? output.closest('.log-scroll-panel') : null;
      const folderPath = document.getElementById('logs-folder-path');
      const nextText = (channels[state.currentLogChannel] || []).join('\\n') || '暂无日志';
      const activeSelection = window.getSelection();
      const selectionInsideLogs = Boolean(
        output
        && activeSelection
        && activeSelection.rangeCount > 0
        && output.contains(activeSelection.anchorNode)
        && output.contains(activeSelection.focusNode)
        && activeSelection.toString()
      );
      const shouldStickToBottom = Boolean(
        scroller
        && (scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop) <= 4
      );
      if (output && output.textContent !== nextText && !selectionInsideLogs) {
        output.textContent = nextText;
        if (shouldStickToBottom && scroller) {
          scroller.scrollTop = scroller.scrollHeight;
        }
      }
      if (folderPath) {
        folderPath.textContent = `日志目录：${state.logFolderPath || '未知'}`;
      }
      document.querySelectorAll('[data-log-channel]').forEach((button) => {
        button.classList.toggle('active', button.dataset.logChannel === state.currentLogChannel);
      });
    }

    function renderWrapperStatus(wrapperStatus) {
      state.wrapperStatus = wrapperStatus;
      const copy = document.getElementById('wrapper-status-copy');
      const actionCopy = document.getElementById('wrapper-action-copy');
      const startButton = document.getElementById('start-wrapper-btn');
      if (!copy) return;
      if (!wrapperStatus) {
        copy.textContent = '高音质和杜比全景声需要外部 wrapper。';
        if (actionCopy) {
          actionCopy.textContent = '如果已创建 wrapper-latest-10022 容器，可在这里手动拉起。';
        }
        if (startButton) {
          startButton.disabled = false;
        }
        syncDownloadWorkspace();
        return;
      }
      copy.textContent = wrapperStatus.available
        ? (wrapperStatus.message || '高音质和杜比全景声需要外部 wrapper。')
        : '高音质和杜比全景声需要外部 wrapper。';
      if (actionCopy) {
        actionCopy.textContent = wrapperStatus.available
          ? 'wrapper 已就绪；如需切换端口，请先修改地址再重新启动。'
          : (wrapperStatus.mode === 'docker'
            ? '已检测到 Docker 容器但未运行；点击按钮可直接拉起。'
            : '当前未检测到可自动启动的 wrapper 容器。');
      }
      if (startButton) {
        startButton.disabled = wrapperStatus.available;
      }
      syncDownloadWorkspace();
    }

    function syncArtistDownloadOptions(preview = []) {
      const field = document.getElementById('artist-download-field');
      const select = document.getElementById('artist-auto-select');
      const copy = document.getElementById('artist-download-copy');
      if (!field || !select || !copy) return;
      const hasArtist = preview.some((item) => item.kind === 'artist' && item.valid);
      field.hidden = !hasArtist;
      select.disabled = !hasArtist;
      copy.textContent = hasArtist
        ? '检测到 Artist 链接；本次任务里的所有 Artist 链接会共用这一下载内容。'
        : '仅当链接里包含 Artist 时才需要选择。';
      if (!hasArtist) {
        select.value = '';
      }
    }

    function syncArtistDownloadOptionsFromInput() {
      const input = document.getElementById('url-input');
      if (!input) return;
      const tokens = input.value.split(/\\s+/).filter(Boolean);
      const hasArtist = tokens.some((url) => url.includes('music.apple.com') && url.includes('/artist/'));
      if (hasArtist) {
        syncArtistDownloadOptions([{ kind: 'artist', valid: true }]);
        return;
      }
      syncArtistDownloadOptions([]);
    }

    function renderRuntime(runtime) {
      state.runtime = runtime;
      if (!runtime) return;

      const loginButtons = [
        document.getElementById('login-webview-btn'),
        document.getElementById('setup-login-webview-btn'),
      ];
      loginButtons.forEach((button) => {
        button.disabled = !runtime.native_login_supported;
        button.title = runtime.native_login_supported ? '' : runtime.native_login_message;
      });

      document.getElementById('setup-login-copy').textContent = runtime.native_login_supported
        ? '登录 Apple Music 账号。'
        : runtime.native_login_message;
      document.getElementById('account-login-copy').textContent = runtime.native_login_supported
        ? '支持浏览器辅助登录和浏览器导入，登录状态保存在本机和 Keychain。'
        : runtime.native_login_message;

      const outputInputs = [
        document.getElementById('output-path'),
        document.getElementById('setup-output-path'),
      ];
      outputInputs.forEach((input) => {
        input.readOnly = runtime.folder_picker_supported;
        if (!runtime.folder_picker_supported) {
          input.placeholder = '请输入完整下载目录';
        }
      });

      const pickerButtons = [
        document.getElementById('select-output-btn'),
        document.getElementById('setup-select-output-btn'),
      ];
      pickerButtons.forEach((button) => {
        button.disabled = !runtime.folder_picker_supported;
        button.title = runtime.folder_picker_supported ? '' : runtime.output_path_message;
      });

      document.getElementById('output-path-copy').textContent = runtime.folder_picker_supported ? '设置下载目录。' : runtime.output_path_message;
      document.getElementById('setup-output-path-copy').textContent = runtime.folder_picker_supported ? '设置下载目录。' : runtime.output_path_message;
      document.getElementById('convert-output-copy').textContent = runtime.folder_picker_supported ? '选择输出目录。' : runtime.output_path_message;
      document.getElementById('convert-input-copy').textContent = runtime.file_picker_supported ? '选择输入文件或目录。' : runtime.input_path_message;
      document.getElementById('open-file-application-field').style.display = runtime.platform === 'Windows' ? '' : 'none';
      document.getElementById('convert-runtime-copy').textContent = runtime.conversion_message;
      const convertRuntimeBadge = document.getElementById('convert-runtime-badge');
      convertRuntimeBadge.textContent = runtime.conversion_supported ? '可用' : '不可用';
      convertRuntimeBadge.className = runtime.conversion_supported ? 'badge success' : 'badge danger';

      const convertInput = document.getElementById('convert-input-path');
      convertInput.readOnly = runtime.file_picker_supported;
      if (!runtime.file_picker_supported) {
        convertInput.placeholder = '请输入完整输入文件或目录路径';
      }
      const convertOutput = document.getElementById('convert-output-path');
      convertOutput.readOnly = runtime.folder_picker_supported;
      if (!runtime.folder_picker_supported) {
        convertOutput.placeholder = '请输入完整输出目录路径';
      }
      const openLogsButton = document.getElementById('open-logs-folder-btn');
      if (openLogsButton) {
        openLogsButton.disabled = !runtime.file_actions_supported;
        openLogsButton.title = runtime.file_actions_supported ? '' : '当前环境不支持直接打开本机目录';
      }

      document.getElementById('select-convert-file-btn').disabled = !runtime.file_picker_supported;
      document.getElementById('select-convert-file-btn').title = runtime.file_picker_supported ? '' : runtime.input_path_message;
      document.getElementById('select-convert-input-folder-btn').disabled = !runtime.file_picker_supported;
      document.getElementById('select-convert-input-folder-btn').title = runtime.file_picker_supported ? '' : runtime.input_path_message;
      document.getElementById('select-convert-output-btn').disabled = !runtime.folder_picker_supported;
      document.getElementById('select-convert-output-btn').title = runtime.folder_picker_supported ? '' : runtime.output_path_message;
      document.getElementById('submit-convert-btn').disabled = !runtime.conversion_supported;
      document.getElementById('submit-convert-btn').title = runtime.conversion_supported ? '' : runtime.conversion_message;
      syncConvertMode();
      syncDownloadWorkspace();
      refreshJobs();
    }

    function renderSettings(settings, wrapperStatus = null, runtime = null) {
      state.settings = settings;
      closeOpenWithMenus();
      document.getElementById('output-path').value = settings.output_path || '';
      document.getElementById('setup-output-path').value = settings.output_path || '';
      document.getElementById('log-level').value = settings.log_level || 'INFO';
      document.getElementById('folder-structure').value = String(settings.preserve_folder_structure !== false);
      document.getElementById('save-cover').value = String(settings.save_cover);
      document.getElementById('song-codec').value = settings.song_codec || 'aac-legacy';
      document.getElementById('download-output-format').value = settings.download_output_format || 'original';
      document.getElementById('keep-original-files').value = String(settings.keep_original_files === true);
      document.getElementById('download-concurrency').value = settings.download_concurrency || 8;
      document.getElementById('conversion-concurrency').value = settings.conversion_concurrency || 8;
      setMetadataLanguageValue(settings.language || 'zh-CN');
      document.getElementById('theme-select').value = settings.theme || 'warm';
      document.getElementById('overwrite').value = String(settings.overwrite);
      document.getElementById('network-mode').value = settings.network_mode || 'auto';
      document.getElementById('proxy-url').value = settings.proxy_url || '';
      document.getElementById('use-wrapper').value = String(settings.use_wrapper);
      document.getElementById('wrapper-decrypt-ip').value = settings.wrapper_decrypt_ip || DEFAULT_WRAPPER_DECRYPT_IP;
      document.getElementById('artist-auto-select').value = settings.artist_auto_select || '';
      document.getElementById('open-file-application').value = settings.open_file_application || '';
      document.getElementById('browser-import-enabled').value = String(settings.browser_import_enabled);
      const browserImportButtons = [
        document.getElementById('browser-import-btn'),
        document.getElementById('setup-browser-import-btn'),
      ];
      const browserImportEnabled = settings.browser_import_enabled !== false;
      const browserImportMessage = browserImportEnabled ? '' : '当前已关闭浏览器导入功能。请先在设置中重新开启。';
      browserImportButtons.forEach((button) => {
        if (!button) return;
        button.disabled = !browserImportEnabled;
        button.title = browserImportMessage;
      });
      applyTheme(settings.theme || 'warm');
      syncNetworkModeControls();
      if (!document.getElementById('convert-output-path').value.trim()) {
        document.getElementById('convert-output-path').value = settings.output_path || '';
      }
      syncDownloadWorkspace();
      renderRuntime(runtime);
      renderWrapperStatus(wrapperStatus);
      syncArtistDownloadOptions(state.preview || []);
      syncSetupOverlay();
    }

    function renderAbout(data) {
      const list = document.getElementById('about-list');
      const browserImports = (data.supported_browser_imports || []).join(', ');
      const kinds = (data.supported_download_kinds || []).join(', ');
      const defaultCodec = data.distribution_audio_default === 'aac-legacy' ? 'AAC' : (data.distribution_audio_default || '未知');
      const originalProjectName = data.original_project_name || 'Gamdl';
      const originalProjectUrl = data.original_project_url || 'https://github.com/glomatico/gamdl';
      const modifiedBy = data.modified_by || '@Mrgu2';
      const modifiedProjectUrl = data.modified_project_url || 'https://github.com/Mrgu2/gu_music_downloader';
      const downloadSafetyNote = data.download_safety_note || '请确保从 GitHub @Mrgu2 下载该软件，以保证软件安全、没有后门且来源可核验。';
      const alacMaxSpec = data.alac_max_spec || '24-bit / 192 kHz';
      const alacSpecNote = data.alac_spec_note || '具体取决于歌曲本身是否提供对应规格。';
      const qualityMode = data.alac_mode === 'external-wrapper-only'
        ? 'ALAC / 杜比全景声需要外部 wrapper'
        : (data.alac_mode || '未知');
      const runtime = data.runtime || {};
      list.innerHTML = `
        <div class="list-item"><div class="list-head"><strong>版本</strong><span class="muted">${data.version}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>项目来源</strong><span class="muted">改编自 ${originalProjectName}</span></div><div class="muted">原版项目地址：<a href="${originalProjectUrl}" target="_blank" rel="noreferrer">${originalProjectUrl}</a></div></div>
        <div class="list-item"><div class="list-head"><strong>修改者</strong><span class="muted">${modifiedBy}</span></div><div class="muted">当前桌面版项目地址：<a href="${modifiedProjectUrl}" target="_blank" rel="noreferrer">${modifiedProjectUrl}</a></div></div>
        <div class="list-item"><div class="list-head"><strong>下载安全提示</strong><span class="muted">请从 GitHub 获取</span></div><div class="muted">${downloadSafetyNote}</div></div>
        <div class="list-item"><div class="list-head"><strong>当前平台</strong><span class="muted">${runtime.platform || 'unknown'}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>可导入的浏览器</strong><span class="muted">${browserImports}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>支持的链接类型</strong><span class="muted">${kinds}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>默认音质</strong><span class="muted">${defaultCodec}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>高音质模式</strong><span class="muted">${qualityMode}</span></div></div>
        <div class="list-item"><div class="list-head"><strong>登录能力</strong><span class="muted">${runtime.native_login_supported ? '浏览器辅助登录 + 浏览器导入' : '仅浏览器导入'}</span></div><div class="muted">${runtime.native_login_message || ''}</div></div>
        <div class="list-item"><div class="list-head"><strong>ALAC 规格</strong><span class="muted">最高支持 ${alacMaxSpec}</span></div><div class="muted">${alacSpecNote}</div></div>
        <div class="list-item"><div class="list-head"><strong>Wrapper 状态</strong><span class="muted">${data.wrapper_status?.message || 'unknown'}</span></div></div>
      `;
    }

    async function refreshSession() {
      const data = await api('/api/auth/status');
      renderSession(data.session);
    }

    async function refreshSettings() {
      const data = await api('/api/settings');
      renderSettings(data.settings, data.wrapper_status, data.runtime);
    }

    let jobsRefreshInFlight = false;
    async function refreshJobs(options = {}) {
      const shouldPreserveOpenMenu = Boolean(options.preserveOpenMenu);
      if (shouldPreserveOpenMenu && state.openWithMenuJobId) {
        return;
      }
      if (jobsRefreshInFlight) return;
      jobsRefreshInFlight = true;
      try {
        closeOpenWithMenus();
        const data = await api('/api/jobs');
        renderJobs(data.jobs);
      } finally {
        jobsRefreshInFlight = false;
      }
    }

    async function refreshLogs() {
      const data = await api('/api/logs');
      renderLogs(data);
    }

    async function refreshAbout() {
      const data = await api('/api/about');
      renderAbout(data);
    }

    async function previewLinks() {
      const urlText = document.getElementById('url-input').value;
      const data = await api('/api/preview', {
        method: 'POST',
        body: JSON.stringify({ url_text: urlText }),
      });
      renderPreview(data);
    }

    async function submitJob() {
      if (!state.session || !state.session.connected) {
        throw new Error('请先完成 Apple Music 登录。');
      }
      const selectedCodec = document.getElementById('song-codec').value;
      const useWrapper = document.getElementById('use-wrapper').value === 'true';
      if ((selectedCodec === 'alac' || selectedCodec === 'atmos') && !useWrapper) {
        throw new Error('当前所选音质需要启用外部 wrapper。请先打开“启用 wrapper”，或切回 AAC。');
      }
      const payload = collectSettingsPayload();
      payload.save_playlist = document.getElementById('save-playlist').value === 'true';
      payload.url_text = document.getElementById('url-input').value;
      const data = await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      showToast(`任务 ${data.id} 已加入队列`);
      document.getElementById('url-input').value = '';
      await refreshJobs();
      activatePage('jobs');
    }

    async function cancelJob(jobId) {
      await api(`/api/jobs/${jobId}/cancel`, { method: 'POST', body: '{}' });
      showToast('已发送取消请求，当前文件完成后停止');
      await refreshJobs();
    }
    window.cancelJob = cancelJob;

    function getOpenWithMenu() {
      return document.getElementById('open-with-menu');
    }

    function closeOpenWithMenus() {
      const menu = getOpenWithMenu();
      if (menu) {
        menu.classList.remove('measuring');
        menu.hidden = true;
      }
      document.querySelectorAll('.split-pill-anchor').forEach((anchor) => {
        anchor.classList.remove('open');
        anchor.classList.remove('loading');
      });
      document.querySelectorAll('.split-pill-toggle').forEach((toggle) => toggle.setAttribute('aria-expanded', 'false'));
      state.openWithMenuRequestId += 1;
      state.openWithMenuJobId = null;
    }
    window.closeOpenWithMenus = closeOpenWithMenus;

    function renderOpenWithMenu(jobId, data) {
      const menu = getOpenWithMenu();
      if (!menu) return;
      const items = data.options || [];
      menu.innerHTML = items.map((item) => {
        if (item.kind === 'separator') {
          return '<div class="finder-menu-separator"></div>';
        }
        const label = escapeHtml(item.label || '');
        const meta = item.is_default ? '<span class="finder-menu-meta">默认</span>' : '';
        if (item.kind === 'default') {
          return `<button class="finder-menu-item" type="button" onclick="executeOpenWithOption('${jobId}', 'default')"><span class="finder-menu-label">${label}</span>${meta}</button>`;
        }
        if (item.kind === 'pick-application') {
          return `<button class="finder-menu-item" type="button" onclick="executeOpenWithOption('${jobId}', 'pick-application')"><span class="finder-menu-label">${label}</span></button>`;
        }
        const applicationPath = encodeURIComponent(item.application_path || '');
        return `<button class="finder-menu-item" type="button" onclick="executeOpenWithOption('${jobId}', 'application', decodeURIComponent('${applicationPath}'))"><span class="finder-menu-label">${label}</span>${meta}</button>`;
      }).join('') || '<button class="finder-menu-item" type="button" disabled><span class="finder-menu-label">没有可用的打开方式</span></button>';
    }

    function revealMeasuredOpenWithMenu(jobId) {
      const menu = getOpenWithMenu();
      if (!menu) return;
      menu.hidden = false;
      menu.classList.add('measuring');
      positionOpenWithMenu(jobId);
      menu.classList.remove('measuring');
    }

    function positionOpenWithMenu(jobId) {
      const menu = getOpenWithMenu();
      const toggle = document.querySelector(`.split-pill-anchor[data-job-id="${jobId}"] .split-pill-toggle`);
      if (!menu || !toggle) return;

      const gap = 0;
      const seamOverlap = 1;
      const margin = 16;
      const desiredMenuWidth = 300;
      const fallbackMinWidth = 150;
      menu.style.width = '';

      const toggleRect = toggle.getBoundingClientRect();
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const maxMenuWidth = Math.max(120, Math.min(360, viewportWidth - margin * 2));
      const availableRightWidth = Math.max(0, viewportWidth - (toggleRect.right - seamOverlap) - gap - margin);
      let computedMenuWidth = Math.min(desiredMenuWidth, maxMenuWidth);
      if (availableRightWidth > 0) {
        computedMenuWidth = Math.min(computedMenuWidth, availableRightWidth);
      }
      if (availableRightWidth >= fallbackMinWidth) {
        computedMenuWidth = Math.max(computedMenuWidth, fallbackMinWidth);
      } else if (availableRightWidth > 0) {
        computedMenuWidth = Math.max(computedMenuWidth, Math.min(availableRightWidth, maxMenuWidth));
      }
      computedMenuWidth = Math.max(120, Math.min(computedMenuWidth, maxMenuWidth));
      menu.style.width = `${computedMenuWidth}px`;

      const sizedMenuRect = menu.getBoundingClientRect();
      const finalMenuWidth = sizedMenuRect.width || computedMenuWidth;
      const menuHeight = sizedMenuRect.height || 220;
      const preferredTop = toggleRect.top - 2;
      const maxLeft = viewportWidth - finalMenuWidth - margin;
      let left = Math.max(margin, Math.min(toggleRect.right - seamOverlap + gap, maxLeft));
      let top = Math.max(margin, Math.min(preferredTop, viewportHeight - menuHeight - margin));

      menu.style.left = `${Math.round(left)}px`;
      menu.style.top = `${Math.round(top)}px`;
    }

    async function toggleOpenWithMenu(jobId, event) {
      try {
        event.stopPropagation();
        const menu = getOpenWithMenu();
        const anchor = event.currentTarget.closest('.split-pill-anchor');
        const toggle = event.currentTarget;
        if (!menu) return;
        const willShow = state.openWithMenuJobId !== jobId || menu.hidden;
        closeOpenWithMenus();
        if (!willShow) {
          return;
        }
        state.openWithMenuJobId = jobId;
        if (anchor) {
          anchor.classList.add('open');
        }
        if (toggle) {
          toggle.setAttribute('aria-expanded', 'true');
        }
        const cached = getCachedOpenWithOptions(jobId);
        if (cached) {
          setOpenWithMenuLoading(jobId, false);
          renderOpenWithMenu(jobId, cached);
          revealMeasuredOpenWithMenu(jobId);
          return;
        }
        setOpenWithMenuLoading(jobId, true);
        menu.classList.remove('measuring');
        menu.hidden = true;
        const requestId = ++state.openWithMenuRequestId;
        const data = await api(`/api/jobs/${jobId}/open-with-options`);
        if (state.openWithMenuJobId !== jobId || state.openWithMenuRequestId !== requestId) {
          return;
        }
        cacheOpenWithOptions(jobId, data.latest_media_path || getLatestMediaPath(jobId), data);
        setOpenWithMenuLoading(jobId, false);
        renderOpenWithMenu(jobId, data);
        revealMeasuredOpenWithMenu(jobId);
      } catch (error) {
        setOpenWithMenuLoading(jobId, false);
        closeOpenWithMenus();
        showToast(error.message || '读取打开方式失败');
      }
    }
    window.toggleOpenWithMenu = toggleOpenWithMenu;

    async function executeOpenWithOption(jobId, kind, applicationPath = null) {
      if (kind === 'default') {
        await openJobFile(jobId);
        return;
      }
      const payload = kind === 'pick-application'
        ? { choose_application: true }
        : { application_path: applicationPath };
      const data = await api(`/api/jobs/${jobId}/open-latest-file`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      closeOpenWithMenus();
      if (data.cancelled) {
        return;
      }
      if (kind === 'pick-application') {
        showToast('已使用所选应用打开最近下载文件');
        return;
      }
      showToast('已使用指定应用打开最近下载文件');
    }
    window.executeOpenWithOption = executeOpenWithOption;

    async function openJobOutput(jobId) {
      await api(`/api/jobs/${jobId}/open-output`, { method: 'POST', body: '{}' });
      showToast('已打开下载目录');
    }
    window.openJobOutput = openJobOutput;

    async function openJobFile(jobId) {
      await api(`/api/jobs/${jobId}/open-latest-file`, { method: 'POST', body: '{}' });
      closeOpenWithMenus();
      showToast('已打开最近下载文件');
    }
    window.openJobFile = openJobFile;

    async function revealJobFile(jobId) {
      await api(`/api/jobs/${jobId}/reveal-latest-file`, { method: 'POST', body: '{}' });
      closeOpenWithMenus();
      showToast('已在目录中显示最近下载文件');
    }
    window.revealJobFile = revealJobFile;

    async function retryFailedItems(jobId) {
      const data = await api(`/api/jobs/${jobId}/retry-failed`, { method: 'POST', body: '{}' });
      showToast(`已将 ${data.retry_count} 个失败项重新加入队列`);
      await refreshJobs();
      activatePage('jobs');
    }
    window.retryFailedItems = retryFailedItems;

    function collectSettingsPayload() {
      return {
        output_path: document.getElementById('output-path').value.trim(),
        log_level: document.getElementById('log-level').value,
        preserve_folder_structure: document.getElementById('folder-structure').value === 'true',
        save_cover: document.getElementById('save-cover').value === 'true',
        song_codec: document.getElementById('song-codec').value,
        download_output_format: document.getElementById('download-output-format').value,
        keep_original_files: document.getElementById('keep-original-files').value === 'true',
        download_concurrency: Number(document.getElementById('download-concurrency').value),
        conversion_concurrency: Number(document.getElementById('conversion-concurrency').value),
        language: readMetadataLanguageValue(),
        artist_auto_select: document.getElementById('artist-auto-select').value,
        theme: document.getElementById('theme-select').value,
        overwrite: document.getElementById('overwrite').value === 'true',
        network_mode: document.getElementById('network-mode').value,
        proxy_url: document.getElementById('proxy-url').value.trim(),
        use_wrapper: document.getElementById('use-wrapper').value === 'true',
        wrapper_decrypt_ip: document.getElementById('wrapper-decrypt-ip').value.trim(),
        open_file_application: document.getElementById('open-file-application').value.trim(),
        browser_import_enabled: document.getElementById('browser-import-enabled').value === 'true',
      };
    }

    function collectConvertPayload() {
      return {
        kind: 'convert',
        input_mode: document.getElementById('convert-input-mode').value,
        input_path: document.getElementById('convert-input-path').value.trim(),
        output_path: document.getElementById('convert-output-path').value.trim(),
        target_format: document.getElementById('convert-target-format').value,
        overwrite: document.getElementById('overwrite').value === 'true',
      };
    }

    function collectSetupPayload() {
      return {
        output_path: document.getElementById('setup-output-path').value.trim(),
      };
    }

    async function saveSettings() {
      const data = await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify(collectSettingsPayload()),
      });
      renderSettings(data.settings, data.wrapper_status, data.runtime);
      showToast('设置已保存');
    }

    async function startWrapper() {
      const requestedIp = document.getElementById('wrapper-decrypt-ip').value.trim() || DEFAULT_WRAPPER_DECRYPT_IP;
      const data = await api('/api/wrapper/start', {
        method: 'POST',
        body: JSON.stringify({
          wrapper_decrypt_ip: requestedIp,
          network_mode: document.getElementById('network-mode').value,
          proxy_url: document.getElementById('proxy-url').value.trim(),
        }),
      });
      document.getElementById('wrapper-decrypt-ip').value = data.resolved_ip || requestedIp;
      renderWrapperStatus(data.wrapper_status);
      showToast(`wrapper 已启动：${data.resolved_ip || requestedIp}`);
    }

    async function chooseOutputFolder(targetInputId, successMessage = '下载目录已确认') {
      const data = await api('/api/desktop/select-folder', {
        method: 'POST',
        body: '{}',
      });
      if (!data.selected) {
        return;
      }
      document.getElementById(targetInputId).value = data.output_path;
      syncSetupOverlay();
      showToast(successMessage);
    }

    async function chooseExistingPath(apiPath, targetInputId, successMessage) {
      const data = await api(apiPath, {
        method: 'POST',
        body: '{}',
      });
      if (!data.selected) {
        return;
      }
      document.getElementById(targetInputId).value = data.input_path;
      showToast(successMessage);
    }

    function syncConvertMode() {
      const mode = document.getElementById('convert-input-mode').value;
      const fileButton = document.getElementById('select-convert-file-btn');
      const folderButton = document.getElementById('select-convert-input-folder-btn');
      fileButton.classList.toggle('soft', mode === 'file');
      folderButton.classList.toggle('soft', mode === 'directory');
      fileButton.disabled = !state.runtime?.file_picker_supported || mode !== 'file';
      folderButton.disabled = !state.runtime?.file_picker_supported || mode !== 'directory';
      fileButton.title = mode === 'file' ? (state.runtime?.file_picker_supported ? '' : state.runtime?.input_path_message || '') : '当前模式为目录';
      folderButton.title = mode === 'directory' ? (state.runtime?.file_picker_supported ? '' : state.runtime?.input_path_message || '') : '当前模式为文件';
    }

    async function submitConvertJob() {
      if (!state.runtime?.conversion_supported) {
        throw new Error(state.runtime?.conversion_message || '当前环境缺少 ffmpeg，无法转换');
      }
      const payload = collectConvertPayload();
      const data = await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      showToast(`转换任务 ${data.id} 已加入队列`);
      document.getElementById('convert-input-path').value = '';
      await refreshJobs();
      activatePage('jobs');
    }

    async function completeSetup() {
      const payload = collectSetupPayload();
      if (!payload.output_path) {
        throw new Error('请先确认下载目录');
      }
      if (!state.session || !state.session.connected) {
        throw new Error('请先完成 Apple Music 登录');
      }
      const data = await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          output_path: payload.output_path,
          setup_completed: true,
        }),
      });
      renderSettings(data.settings, data.wrapper_status);
      showToast('首次设置已完成');
    }

    async function loginWithWebview() {
      const browser = document.getElementById('browser-select').value;
      const language = readMetadataLanguageValue();
      const data = await api('/api/auth/login-webview', {
        method: 'POST',
        body: JSON.stringify({ browser, language }),
      });
      renderSession(data.session);
      showToast(`已通过 ${browser} 完成浏览器辅助登录`);
    }

    async function importFromBrowser() {
      if (document.getElementById('browser-import-enabled').value !== 'true') {
        throw new Error('当前已关闭浏览器导入功能。请先在设置中重新开启。');
      }
      const browser = document.getElementById('browser-select').value;
      const language = readMetadataLanguageValue();
      const data = await api('/api/auth/import-browser', {
        method: 'POST',
        body: JSON.stringify({ browser, language }),
      });
      renderSession(data.session);
      showToast(`已从 ${browser} 导入登录状态`);
    }

    async function setupLoginWithWebview() {
      const browser = document.getElementById('setup-browser-select').value;
      const language = readMetadataLanguageValue();
      const data = await api('/api/auth/login-webview', {
        method: 'POST',
        body: JSON.stringify({ browser, language }),
      });
      renderSession(data.session);
      document.getElementById('browser-select').value = browser;
      showToast(`已通过 ${browser} 完成浏览器辅助登录`);
    }

    async function setupImportFromBrowser() {
      if (document.getElementById('browser-import-enabled').value !== 'true') {
        throw new Error('当前已关闭浏览器导入功能。请先在设置中重新开启。');
      }
      const browser = document.getElementById('setup-browser-select').value;
      const language = readMetadataLanguageValue();
      const data = await api('/api/auth/import-browser', {
        method: 'POST',
        body: JSON.stringify({ browser, language }),
      });
      renderSession(data.session);
      document.getElementById('browser-select').value = browser;
      showToast(`已从 ${browser} 导入登录状态`);
    }

    async function logout() {
      const data = await api('/api/auth/logout', {
        method: 'POST',
        body: '{}',
      });
      renderSession(data.session);
      showToast('已退出登录');
    }

    async function exportDiagnostics() {
      const data = await api('/api/diagnostics/export', {
        method: 'POST',
        body: '{}',
      });
      showToast(`诊断包已生成：${data.bundle_path}`);
    }

    async function openLogsFolder() {
      const data = await api('/api/logs/open-folder', {
        method: 'POST',
        body: '{}',
      });
      showToast(`已打开日志目录：${data.folder_path}`);
    }

    function bindEvents() {
      document.addEventListener('click', (event) => {
        if (!event.target.closest('.split-pill-anchor')) {
          closeOpenWithMenus();
        }
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          closeOpenWithMenus();
        }
      });
      document.querySelectorAll('.nav-btn').forEach((button) => {
        button.addEventListener('click', () => activatePage(button.dataset.page));
      });
      document.getElementById('url-input').addEventListener('input', syncArtistDownloadOptionsFromInput);
      document.getElementById('preview-btn').addEventListener('click', () => runAction(previewLinks));
      document.getElementById('submit-job-btn').addEventListener('click', () => runAction(submitJob));
      document.getElementById('submit-convert-btn').addEventListener('click', () => runAction(submitConvertJob));
      document.getElementById('refresh-jobs-btn').addEventListener('click', () => runAction(refreshJobs));
      document.getElementById('save-settings-btn').addEventListener('click', () => runAction(saveSettings));
      document.getElementById('start-wrapper-btn').addEventListener('click', () => runAction(startWrapper));
      document.getElementById('metadata-language-select').addEventListener('change', syncMetadataLanguageControls);
      document.getElementById('select-output-btn').addEventListener('click', () => runAction(() => chooseOutputFolder('output-path', '下载目录已更新')));
      document.getElementById('select-convert-output-btn').addEventListener('click', () => runAction(() => chooseOutputFolder('convert-output-path', '转换输出目录已更新')));
      document.getElementById('select-convert-file-btn').addEventListener('click', () => runAction(() => chooseExistingPath('/api/desktop/select-file', 'convert-input-path', '已选择输入文件')));
      document.getElementById('select-convert-input-folder-btn').addEventListener('click', () => runAction(() => chooseExistingPath('/api/desktop/select-input-folder', 'convert-input-path', '已选择输入目录')));
      document.getElementById('convert-input-mode').addEventListener('change', syncConvertMode);
      document.getElementById('theme-select').addEventListener('change', (event) => applyTheme(event.target.value));
      document.getElementById('network-mode').addEventListener('change', syncNetworkModeControls);
      document.getElementById('login-webview-btn').addEventListener('click', () => runAction(loginWithWebview));
      document.getElementById('browser-import-btn').addEventListener('click', () => runAction(importFromBrowser));
      document.getElementById('setup-login-webview-btn').addEventListener('click', () => runAction(setupLoginWithWebview));
      document.getElementById('setup-browser-import-btn').addEventListener('click', () => runAction(setupImportFromBrowser));
      document.getElementById('setup-select-output-btn').addEventListener('click', () => runAction(() => chooseOutputFolder('setup-output-path')));
      document.getElementById('complete-setup-btn').addEventListener('click', () => runAction(completeSetup));
      document.getElementById('logout-btn').addEventListener('click', () => runAction(logout));
      document.getElementById('export-diagnostics-btn').addEventListener('click', () => runAction(exportDiagnostics));
      document.getElementById('open-logs-folder-btn').addEventListener('click', () => runAction(openLogsFolder));
      document.querySelectorAll('[data-log-channel]').forEach((button) => {
        button.addEventListener('click', () => {
          state.currentLogChannel = button.dataset.logChannel;
          renderLogs({ channels: state.logs, folder_path: state.logFolderPath });
        });
      });
      syncConvertMode();
    }

    async function runAction(action) {
      try {
        await action();
      } catch (error) {
        console.error(error);
        showToast(error.message);
      }
    }

    async function bootstrap() {
      bindEvents();
      await Promise.all([refreshSession(), refreshSettings(), refreshJobs(), refreshLogs(), refreshAbout()]);
      await previewLinks();
      window.setInterval(() => {
        refreshJobs({ preserveOpenMenu: true }).catch(console.error);
        refreshLogs().catch(console.error);
      }, 2500);
    }

    bootstrap().catch((error) => {
      console.error(error);
      showToast(error.message);
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
