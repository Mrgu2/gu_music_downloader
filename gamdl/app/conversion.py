from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from enum import Enum
from io import BytesIO
import logging
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Callable

from mutagen import File
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from PIL import Image

from .cancellation import JobCancelledError
from .settings import validate_concurrency
from .executables import resolve_executable

logger = logging.getLogger("gamdl.app.conversion")

CONVERSION_DESCRIPTION = "软件纯免费开源，无病毒，无额外广告，请确保你是从 GitHub @Mrgu2 下载的该软件。"
AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".ape",
    ".caf",
    ".flac",
    ".m4a",
    ".m4b",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
MP3_SUPPORTED_SAMPLE_RATES = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
COMMENT_LANGUAGE = "eng"


class ConversionFormat(str, Enum):
    FLAC = "flac"
    MP3 = "mp3"


@dataclass
class ConversionJobSpec:
    input_mode: str
    input_path: str
    output_path: str
    target_format: str
    overwrite: bool = False


@dataclass
class ConversionResult:
    total_files: int = 0
    converted_files: int = 0
    skipped_files: int = 0
    errors: int = 0
    active_conversions: int = 0
    conversion_concurrency: int = 8
    latest_media_path: str | None = None
    latest_media_dir: str | None = None
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NormalizedPicture:
    data: bytes
    mime: str
    width: int | None = None
    height: int | None = None
    depth: int | None = None


@dataclass
class NormalizedTags:
    sample_rate: int
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    composer: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    date: str | None = None
    genre: str | None = None
    lyrics: str | None = None
    picture: NormalizedPicture | None = None


class ConversionService:
    def __init__(
        self,
        ffmpeg_path: str = "ffmpeg",
        log_callback: Callable[[str], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        concurrency: int = 8,
    ) -> None:
        self.concurrency = validate_concurrency(concurrency)
        self.ffmpeg_path = ffmpeg_path
        self.ffmpeg_resolution = resolve_executable("ffmpeg", ffmpeg_path)
        self.full_ffmpeg_path = self.ffmpeg_resolution.path
        self.log_callback = log_callback
        self.cancel_callback = cancel_callback
        self.progress_callback = progress_callback
        self._result_lock = threading.RLock()

    def _publish_progress(self, result: ConversionResult) -> None:
        if self.progress_callback:
            self.progress_callback(result.to_dict())

    def _emit(self, message: str) -> None:
        logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def _check_cancel(self, result: ConversionResult) -> None:
        if not self.cancel_callback or not self.cancel_callback():
            return
        result.finished_at = time.time()
        self._emit("任务取消请求已收到，正在停止转换。")
        raise JobCancelledError(result.to_dict())

    def run(self, job: ConversionJobSpec) -> ConversionResult:
        if not self.full_ffmpeg_path:
            raise RuntimeError(f"找不到 ffmpeg 可执行文件：{self.ffmpeg_path}")
        self._emit(
            f"使用 ffmpeg: {self.full_ffmpeg_path}"
            + ("（内置）" if self.ffmpeg_resolution.source == "bundled" else "")
        )

        input_root = Path(job.input_path).expanduser()
        if not input_root.exists():
            raise ValueError("输入路径不存在，请重新选择。")

        output_root = Path(job.output_path).expanduser()
        output_root.mkdir(parents=True, exist_ok=True)

        result = ConversionResult(conversion_concurrency=self.concurrency)
        self._publish_progress(result)
        target_format = ConversionFormat(job.target_format)
        self._check_cancel(result)

        if job.input_mode == "file":
            self._process_file_mode(input_root, output_root, target_format, bool(job.overwrite), result)
        elif job.input_mode == "directory":
            self._process_directory_mode(input_root, output_root, target_format, bool(job.overwrite), result)
        else:
            raise ValueError("输入模式无效，请选择文件或目录。")

        result.finished_at = time.time()
        self._emit(
            "转换完成: "
            f"成功 {result.converted_files}，跳过 {result.skipped_files}，错误 {result.errors}"
        )
        return result

    def _process_file_mode(
        self,
        source_path: Path,
        output_root: Path,
        target_format: ConversionFormat,
        overwrite: bool,
        result: ConversionResult,
    ) -> None:
        self._check_cancel(result)
        result.total_files = 1
        if source_path.suffix.lower() not in AUDIO_EXTENSIONS:
            result.errors = 1
            raise ValueError("当前仅支持转换常见音频文件。")
        destination = output_root / source_path.with_suffix(f".{target_format.value}").name
        result.active_conversions = 1
        self._publish_progress(result)
        try:
            self._convert_path(source_path, destination, target_format, overwrite, result)
        finally:
            result.active_conversions = 0
            self._publish_progress(result)

    def _process_directory_mode(
        self,
        input_root: Path,
        output_root: Path,
        target_format: ConversionFormat,
        overwrite: bool,
        result: ConversionResult,
    ) -> None:
        if not input_root.is_dir():
            raise ValueError("目录模式下请输入有效的输入目录。")

        discovered_files = sorted(
            path for path in input_root.rglob("*")
            if path.is_file() and not path.name.startswith(".amd-convert-")
        )
        if not discovered_files:
            raise ValueError("输入目录中没有可处理的文件。")

        groups: dict[Path, list[Path]] = {}
        result.total_files = len(discovered_files)
        for source_path in discovered_files:
            self._check_cancel(result)
            relative_path = source_path.relative_to(input_root)
            if source_path.suffix.lower() not in AUDIO_EXTENSIONS:
                result.skipped_files += 1
                self._emit(f"[Skip] 非音频文件，已跳过: {relative_path}")
                continue
            destination = output_root / relative_path.with_suffix(f".{target_format.value}")
            groups.setdefault(destination.resolve(), []).append(source_path)

        def convert_group(destination: Path, sources: list[Path]) -> ConversionResult:
            for source in sources:
                if self.cancel_callback and self.cancel_callback():
                    break
                local_result = ConversionResult()
                with self._result_lock:
                    result.active_conversions += 1
                    self._publish_progress(result)
                try:
                    self._convert_path(source, destination, target_format, overwrite, local_result)
                except JobCancelledError:
                    break
                finally:
                    with self._result_lock:
                        result.active_conversions -= 1
                        result.converted_files += local_result.converted_files
                        result.skipped_files += local_result.skipped_files
                        result.errors += local_result.errors
                        if local_result.latest_media_path:
                            result.latest_media_path = local_result.latest_media_path
                            result.latest_media_dir = local_result.latest_media_dir
                        self._publish_progress(result)
            return result

        self._emit(f"转换并发数: {self.concurrency}")
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="audio-conversion") as executor:
            futures = [executor.submit(convert_group, dest, sources) for dest, sources in groups.items()]
            for future in as_completed(futures):
                future.result()
        self._check_cancel(result)

    def _convert_path(
        self,
        source_path: Path,
        destination: Path,
        target_format: ConversionFormat,
        overwrite: bool,
        result: ConversionResult,
    ) -> None:
        self._check_cancel(result)
        if destination.exists() and not overwrite:
            result.skipped_files += 1
            self._emit(f"[Skip] 目标文件已存在: {destination}")
            return

        temporary: Path | None = None
        try:
            metadata = self._read_metadata(source_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # FFmpeg cannot read and write the same file. Only replace the
            # destination once both encoding and metadata writing succeed.
            with tempfile.NamedTemporaryFile(prefix=".amd-convert-", suffix=destination.suffix,
                                             dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
            self._run_ffmpeg(source_path, temporary, target_format, metadata.sample_rate, True, result)
            self._write_metadata(temporary, target_format, metadata)
            temporary.replace(destination)
            result.converted_files += 1
            result.latest_media_path = str(destination.resolve())
            result.latest_media_dir = str(destination.resolve().parent)
            self._emit(f"[OK] {source_path.name} -> {destination}")
        except JobCancelledError:
            raise
        except Exception as exc:
            result.errors += 1
            self._emit(f"[Error] {source_path}: {str(exc) or type(exc).__name__}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _run_ffmpeg(
        self,
        source_path: Path,
        destination: Path,
        target_format: ConversionFormat,
        sample_rate: int,
        overwrite: bool,
        result: ConversionResult,
    ) -> None:
        command = [
            self.full_ffmpeg_path,
            "-v",
            "error",
            "-y" if overwrite else "-n",
            "-i",
            str(source_path),
            "-map_metadata",
            "-1",
            "-map",
            "0:a:0",
            "-vn",
        ]
        if target_format == ConversionFormat.FLAC:
            command.extend(["-c:a", "flac"])
        elif target_format == ConversionFormat.MP3:
            if sample_rate not in MP3_SUPPORTED_SAMPLE_RATES:
                raise ValueError(
                    f"MP3 不支持原样保留 {sample_rate} Hz 采样率，请改用 FLAC。"
                )
            command.extend(
                [
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "0",
                    "-ar",
                    str(sample_rate),
                    "-id3v2_version",
                    "3",
                ]
            )
        command.append(str(destination))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate()
                process_stdout = stdout or ""
                process_stderr = stderr or ""
                if return_code != 0:
                    stderr = process_stderr.strip()
                    raise RuntimeError(stderr or "ffmpeg 转换失败。")
                break
            if self.cancel_callback and self.cancel_callback():
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                self._check_cancel(result)
            time.sleep(0.1)

    def _read_metadata(self, source_path: Path) -> NormalizedTags:
        audio = File(source_path)
        if audio is None or getattr(audio, "info", None) is None:
            raise ValueError("无法识别音频文件或读取元数据。")
        sample_rate = getattr(audio.info, "sample_rate", None)
        if not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("无法读取输入文件采样率。")

        normalized = NormalizedTags(sample_rate=sample_rate)
        normalized.title = self._first_tag(audio, ("title", "©nam"), ("TIT2",))
        normalized.artist = self._first_tag(audio, ("artist", "©ART"), ("TPE1",))
        normalized.album = self._first_tag(audio, ("album", "©alb"), ("TALB",))
        normalized.album_artist = self._first_tag(audio, ("albumartist", "aART"), ("TPE2",))
        normalized.composer = self._first_tag(audio, ("composer", "©wrt"), ("TCOM",))
        normalized.date = self._first_tag(audio, ("date", "year", "©day"), ("TDRC",))
        normalized.genre = self._first_tag(audio, ("genre", "©gen"), ("TCON",))
        normalized.lyrics = self._first_tag(audio, ("lyrics", "unsyncedlyrics", "©lyr"), ("USLT",))
        normalized.track_number, normalized.track_total = self._read_number_pair(
            audio,
            primary_keys=("tracknumber", "tracktotal", "trkn"),
            id3_key="TRCK",
        )
        normalized.disc_number, normalized.disc_total = self._read_number_pair(
            audio,
            primary_keys=("discnumber", "disctotal", "disk"),
            id3_key="TPOS",
        )
        normalized.picture = self._read_picture(audio)
        return normalized

    def _write_metadata(
        self,
        destination: Path,
        target_format: ConversionFormat,
        metadata: NormalizedTags,
    ) -> None:
        if target_format == ConversionFormat.FLAC:
            self._write_flac_metadata(destination, metadata)
            return
        if target_format == ConversionFormat.MP3:
            self._write_mp3_metadata(destination, metadata)
            return
        raise ValueError(f"不支持的输出格式：{target_format.value}")

    def _write_flac_metadata(self, destination: Path, metadata: NormalizedTags) -> None:
        flac = FLAC(destination)
        flac.clear()
        self._set_vorbis_text(flac, "title", metadata.title)
        self._set_vorbis_text(flac, "artist", metadata.artist)
        self._set_vorbis_text(flac, "album", metadata.album)
        self._set_vorbis_text(flac, "albumartist", metadata.album_artist)
        self._set_vorbis_text(flac, "composer", metadata.composer)
        self._set_vorbis_text(flac, "date", metadata.date)
        self._set_vorbis_text(flac, "genre", metadata.genre)
        self._set_vorbis_text(flac, "lyrics", metadata.lyrics)
        self._set_vorbis_text(flac, "description", CONVERSION_DESCRIPTION)
        self._set_vorbis_text(flac, "comment", CONVERSION_DESCRIPTION)
        self._set_vorbis_text(flac, "tracknumber", metadata.track_number)
        self._set_vorbis_text(flac, "tracktotal", metadata.track_total)
        self._set_vorbis_text(flac, "totaltracks", metadata.track_total)
        self._set_vorbis_text(flac, "discnumber", metadata.disc_number)
        self._set_vorbis_text(flac, "disctotal", metadata.disc_total)
        self._set_vorbis_text(flac, "totaldiscs", metadata.disc_total)
        flac.clear_pictures()
        if metadata.picture:
            picture = Picture()
            picture.data = metadata.picture.data
            picture.mime = metadata.picture.mime
            picture.type = 3
            picture.width = metadata.picture.width or 0
            picture.height = metadata.picture.height or 0
            picture.depth = metadata.picture.depth or 0
            flac.add_picture(picture)
        flac.save()

    def _write_mp3_metadata(self, destination: Path, metadata: NormalizedTags) -> None:
        tags = ID3()
        self._add_id3_text(tags, TIT2, metadata.title)
        self._add_id3_text(tags, TPE1, metadata.artist)
        self._add_id3_text(tags, TALB, metadata.album)
        self._add_id3_text(tags, TPE2, metadata.album_artist)
        self._add_id3_text(tags, TCOM, metadata.composer)
        self._add_id3_text(tags, TDRC, metadata.date)
        self._add_id3_text(tags, TCON, metadata.genre)
        if metadata.track_number:
            tags.add(
                TRCK(
                    encoding=1,
                    text=[self._number_pair_text(metadata.track_number, metadata.track_total)],
                )
            )
        if metadata.disc_number:
            tags.add(
                TPOS(
                    encoding=1,
                    text=[self._number_pair_text(metadata.disc_number, metadata.disc_total)],
                )
            )
        if metadata.lyrics:
            tags.add(USLT(encoding=1, lang=COMMENT_LANGUAGE, desc="", text=metadata.lyrics))
        tags.add(COMM(encoding=1, lang=COMMENT_LANGUAGE, desc="", text=CONVERSION_DESCRIPTION))
        tags.add(TXXX(encoding=1, desc="description", text=[CONVERSION_DESCRIPTION]))
        if metadata.picture:
            tags.add(
                APIC(
                    encoding=1,
                    mime=metadata.picture.mime,
                    type=3,
                    desc="Cover",
                    data=metadata.picture.data,
                )
            )
        tags.save(destination, v2_version=3)
        MP3(destination)

    @staticmethod
    def _set_vorbis_text(container: FLAC, key: str, value: str | int | None) -> None:
        if value is None or value == "":
            return
        container[key] = [str(value)]

    @staticmethod
    def _add_id3_text(tags: ID3, frame_cls, value: str | None) -> None:
        if not value:
            return
        tags.add(frame_cls(encoding=1, text=[value]))

    @staticmethod
    def _number_pair_text(number: int, total: int | None) -> str:
        return f"{number}/{total}" if total else str(number)

    def _first_tag(
        self,
        audio,
        generic_keys: tuple[str, ...],
        id3_keys: tuple[str, ...],
    ) -> str | None:
        if isinstance(audio, MP4):
            for key in generic_keys:
                value = audio.tags.get(key) if audio.tags else None
                text = self._extract_text_value(value)
                if text:
                    return text
            return None

        if isinstance(audio, (FLAC, OggOpus, OggVorbis)):
            for key in generic_keys:
                # MP4 atoms (such as ©nam) are invalid Vorbis field names.
                if not key.isascii():
                    continue
                value = audio.tags.get(key) if audio.tags else None
                text = self._extract_text_value(value)
                if text:
                    return text
            return None

        for key in id3_keys:
            if not getattr(audio, "tags", None):
                continue
            if key == "USLT":
                frame = next(iter(audio.tags.getall("USLT")), None)
                if frame and frame.text:
                    return str(frame.text)
                continue
            frame = audio.tags.get(key)
            if not frame:
                continue
            text = getattr(frame, "text", None)
            value = self._extract_text_value(text)
            if value:
                return value
        return None

    def _read_number_pair(
        self,
        audio,
        *,
        primary_keys: tuple[str, ...],
        id3_key: str,
    ) -> tuple[int | None, int | None]:
        if isinstance(audio, MP4):
            values = audio.tags.get(primary_keys[-1]) if audio.tags else None
            if values:
                first = values[0]
                if isinstance(first, tuple) and len(first) == 2:
                    return self._safe_int(first[0]), self._safe_int(first[1])

        if isinstance(audio, (FLAC, OggOpus, OggVorbis)):
            number = self._safe_int(self._extract_text_value(audio.tags.get(primary_keys[0]) if audio.tags else None))
            total = self._safe_int(self._extract_text_value(audio.tags.get(primary_keys[1]) if audio.tags else None))
            return number, total

        if getattr(audio, "tags", None):
            frame = audio.tags.get(id3_key)
            text = self._extract_text_value(getattr(frame, "text", None))
            if text:
                parts = text.split("/", 1)
                number = self._safe_int(parts[0])
                total = self._safe_int(parts[1]) if len(parts) > 1 else None
                return number, total
        return None, None

    def _read_picture(self, audio) -> NormalizedPicture | None:
        if isinstance(audio, MP4) and getattr(audio, "tags", None):
            covers = audio.tags.get("covr") or []
            if covers:
                cover = covers[0]
                mime = "image/jpeg"
                if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG:
                    mime = "image/png"
                return self._build_picture(bytes(cover), mime)
            return None

        if isinstance(audio, FLAC):
            if audio.pictures:
                picture = audio.pictures[0]
                return NormalizedPicture(
                    data=picture.data,
                    mime=picture.mime or self._guess_mime(picture.data),
                    width=picture.width or None,
                    height=picture.height or None,
                    depth=picture.depth or None,
                )

        if isinstance(audio, (OggOpus, OggVorbis)):
            pictures = audio.get("metadata_block_picture", [])
            if pictures:
                raw = base64.b64decode(pictures[0])
                picture = Picture(raw)
                return NormalizedPicture(
                    data=picture.data,
                    mime=picture.mime or self._guess_mime(picture.data),
                    width=picture.width or None,
                    height=picture.height or None,
                    depth=picture.depth or None,
                )

        if getattr(audio, "tags", None) and hasattr(audio.tags, "getall"):
            frame = next(iter(audio.tags.getall("APIC")), None)
            if frame:
                return self._build_picture(frame.data, frame.mime or self._guess_mime(frame.data))
        return None

    def _build_picture(self, data: bytes, mime: str) -> NormalizedPicture:
        width = height = depth = None
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                bands = len(image.getbands() or ())
                depth = bands * 8 if bands else None
        except Exception:
            pass
        return NormalizedPicture(data=data, mime=mime, width=width, height=height, depth=depth)

    @staticmethod
    def _extract_text_value(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            if not value:
                return None
            return ConversionService._extract_text_value(value[0])
        if hasattr(value, "text"):
            return ConversionService._extract_text_value(value.text)
        text = str(value).strip()
        return text or None

    @staticmethod
    def _safe_int(value: str | int | None) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(str(value))
        except ValueError:
            return None

    @staticmethod
    def _guess_mime(data: bytes) -> str:
        try:
            with Image.open(BytesIO(data)) as image:
                if (image.format or "").upper() == "PNG":
                    return "image/png"
        except Exception:
            pass
        return "image/jpeg"
