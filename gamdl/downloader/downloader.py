import asyncio
import logging
import typing
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from ..api.exceptions import ApiError
from ..interface import AppleMusicInterface
from ..interface.types import PlaylistTags
from ..utils import safe_gather
from .constants import (
    ALBUM_MEDIA_TYPE,
    ARTIST_MEDIA_TYPE,
    MUSIC_VIDEO_MEDIA_TYPE,
    PLAYLIST_MEDIA_TYPE,
    SONG_MEDIA_TYPE,
    UPLOADED_VIDEO_MEDIA_TYPE,
    VALID_URL_PATTERN,
)
from .downloader_base import AppleMusicBaseDownloader
from .downloader_music_video import AppleMusicMusicVideoDownloader
from .downloader_song import AppleMusicSongDownloader
from .downloader_uploaded_video import AppleMusicUploadedVideoDownloader
from .enums import ArtistAutoSelect, DownloadMode, RemuxMode
from .exceptions import (
    ExecutableNotFound,
    FormatNotAvailable,
    MediaFileExists,
    NotStreamable,
    SyncedLyricsOnly,
    UnsupportedMediaType,
)
from .types import DownloadItem, UrlInfo

logger = logging.getLogger("gamdl.downloader")


class AppleMusicDownloader:
    def __init__(
        self,
        interface: AppleMusicInterface,
        base_downloader: AppleMusicBaseDownloader,
        song_downloader: AppleMusicSongDownloader,
        music_video_downloader: AppleMusicMusicVideoDownloader,
        uploaded_video_downloader: AppleMusicUploadedVideoDownloader,
        artist_auto_select: ArtistAutoSelect | None = None,
        skip_music_videos: bool = False,
        skip_processing: bool = False,
        flat_filter: typing.Callable = None,
    ):
        self.interface = interface
        self.base_downloader = base_downloader
        self.song_downloader = song_downloader
        self.music_video_downloader = music_video_downloader
        self.uploaded_video_downloader = uploaded_video_downloader
        self.artist_auto_select = artist_auto_select
        self.skip_music_videos = skip_music_videos
        self.skip_processing = skip_processing
        self.flat_filter = flat_filter

    async def get_single_download_item(
        self,
        media_metadata: dict,
        playlist_metadata: dict = None,
        source_context: str | None = None,
        artist_folder_name: str | None = None,
        playlist_tags_override: PlaylistTags | None = None,
    ) -> DownloadItem:
        if self.flat_filter:
            flat_filter_result = self.flat_filter(media_metadata)
            if asyncio.iscoroutine(flat_filter_result):
                flat_filter_result = await flat_filter_result

            if flat_filter_result:
                return DownloadItem(
                    media_metadata=media_metadata,
                    playlist_metadata=playlist_metadata,
                    source_context=source_context,
                    artist_folder_name=artist_folder_name,
                    flat_filter_result=flat_filter_result,
                    playlist_tags=playlist_tags_override,
                )

        return await self.get_single_download_item_no_filter(
            media_metadata,
            playlist_metadata,
            source_context,
            artist_folder_name,
            **({"playlist_tags_override": playlist_tags_override} if playlist_tags_override else {}),
        )

    async def get_single_download_item_no_filter(
        self,
        media_metadata: dict,
        playlist_metadata: dict = None,
        source_context: str | None = None,
        artist_folder_name: str | None = None,
        playlist_tags_override: PlaylistTags | None = None,
    ) -> DownloadItem:
        try:
            if not self.base_downloader.is_media_streamable(
                media_metadata,
            ):
                raise NotStreamable(media_metadata["id"])

            if media_metadata["type"] in SONG_MEDIA_TYPE:
                if not self.song_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.song_downloader.get_download_item(
                    media_metadata,
                    playlist_metadata,
                    source_context=source_context,
                    artist_folder_name=artist_folder_name,
                    **({"playlist_tags_override": playlist_tags_override} if playlist_tags_override else {}),
                )

            if media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
                if not self.music_video_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.music_video_downloader.get_download_item(
                    media_metadata,
                    playlist_metadata,
                )

            if media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
                if not self.uploaded_video_downloader:
                    raise UnsupportedMediaType(media_metadata["type"])

                download_item = await self.uploaded_video_downloader.get_download_item(
                    media_metadata,
                )
        except Exception as e:
            download_item = DownloadItem(
                media_metadata=media_metadata,
                playlist_metadata=playlist_metadata,
                source_context=source_context,
                artist_folder_name=artist_folder_name,
                error=e,
                playlist_tags=playlist_tags_override,
            )

        return download_item

    async def get_collection_download_items(
        self,
        collection_metadata: dict,
    ) -> list[DownloadItem]:
        tracks_metadata = collection_metadata["relationships"]["tracks"]["data"]
        async for extended_data in self.interface.apple_music_api.extend_api_data(
            collection_metadata["relationships"]["tracks"],
        ):
            tracks_metadata.extend(extended_data["data"])

        tasks = [
            self.get_single_download_item(
                media_metadata,
                (
                    collection_metadata
                    if collection_metadata["type"] in PLAYLIST_MEDIA_TYPE
                    else None
                ),
            )
            for media_metadata in tracks_metadata
        ]

        download_items = await safe_gather(*tasks)
        return download_items

    async def get_artist_download_items(
        self,
        artist_metadata: dict,
    ) -> list[DownloadItem]:
        if not self.artist_auto_select:
            available_choices = []
            for artist_auto_select_option in list(ArtistAutoSelect):
                relation_key, type_key = artist_auto_select_option.path_key
                available_choices.append(
                    Choice(
                        name=str(artist_auto_select_option),
                        value=(artist_auto_select_option,),
                    ),
                )

            (artist_auto_select,) = await inquirer.select(
                message=f'Select which type to download for artist "{artist_metadata["attributes"]["name"]}":',
                choices=available_choices,
                validate=lambda result: artist_metadata.get(result[0].path_key[0], {})
                .get(result[0].path_key[1], {})
                .get("data"),
            ).execute_async()
        else:
            artist_auto_select = self.artist_auto_select

        relation_key, type_key = artist_auto_select.path_key
        artist_selection = (
            artist_metadata.get(relation_key, {})
            .get(type_key)
        )
        if not artist_selection or not artist_selection.get("data"):
            return []

        async for extended_data in self.interface.apple_music_api.extend_api_data(
            artist_selection,
        ):
            artist_selection["data"].extend(extended_data["data"])

        selected_items = artist_selection["data"]
        select_all = self.artist_auto_select is not None

        if artist_auto_select in {
            ArtistAutoSelect.MAIN_ALBUMS,
            ArtistAutoSelect.COMPILATION_ALBUMS,
            ArtistAutoSelect.LIVE_ALBUMS,
            ArtistAutoSelect.SINGLES_EPS,
            ArtistAutoSelect.ALL_ALBUMS,
        }:
            return await self.get_artist_albums_download_items(
                selected_items,
                select_all,
            )
        elif artist_auto_select == ArtistAutoSelect.TOP_SONGS:
            return await self.get_artist_songs_download_items(
                selected_items,
                select_all,
                artist_metadata["attributes"]["name"],
            )
        elif artist_auto_select == ArtistAutoSelect.MUSIC_VIDEOS:
            return await self.get_artist_music_videos_download_items(
                selected_items,
                select_all,
            )

    async def get_artist_albums_download_items(
        self,
        albums_metadata: list[dict],
        select_all: bool = False,
    ) -> list[DownloadItem]:
        if not select_all:
            choices = [
                Choice(
                    name=" | ".join(
                        [
                            f'{album["attributes"]["trackCount"]:03d}',
                            f'{album["attributes"]["releaseDate"]:<10}',
                            f'{album["attributes"].get("contentRating", "None").title():<8}',
                            f'{album["attributes"]["name"]}',
                        ]
                    ),
                    value=album,
                )
                for album in albums_metadata
                if album.get("attributes")
            ]
            selected = await inquirer.select(
                message="Select which albums to download: (Track Count | Release Date | Rating | Title)",
                choices=choices,
                multiselect=True,
            ).execute_async()
        else:
            selected = albums_metadata

        download_items = []

        album_tasks = [
            self.interface.apple_music_api.get_album(album_metadata["id"])
            for album_metadata in selected
        ]
        album_responses = await safe_gather(*album_tasks)

        track_tasks = [
            self.get_collection_download_items(album_response["data"][0])
            for album_response in album_responses
        ]
        track_results = await safe_gather(*track_tasks)

        for track_result in track_results:
            download_items.extend(track_result)

        return download_items

    async def get_artist_music_videos_download_items(
        self,
        music_videos_metadata: list[dict],
        select_all: bool = False,
    ) -> list[DownloadItem]:
        if not select_all:
            choices = [
                Choice(
                    name=" | ".join(
                        [
                            self.millis_to_min_sec(
                                music_video["attributes"]["durationInMillis"]
                            ),
                            f'{music_video["attributes"].get("contentRating", "None").title():<8}',
                            music_video["attributes"]["name"],
                        ],
                    ),
                    value=music_video,
                )
                for music_video in music_videos_metadata
                if music_video.get("attributes")
            ]
            selected = await inquirer.select(
                message="Select which music videos to download: (Duration | Rating | Title)",
                choices=choices,
                multiselect=True,
            ).execute_async()
        else:
            selected = music_videos_metadata

        music_video_tasks = [
            self.get_single_download_item(
                music_video_metadata,
            )
            for music_video_metadata in selected
        ]
        download_items = await safe_gather(*music_video_tasks)

        return download_items

    async def get_artist_songs_download_items(
        self,
        songs_metadata: list[dict],
        select_all: bool = False,
        artist_folder_name: str | None = None,
    ) -> list[DownloadItem]:
        if not select_all:
            choices = [
                Choice(
                    name=" | ".join(
                        [
                            self.millis_to_min_sec(
                                song["attributes"]["durationInMillis"]
                            ),
                            f'{song["attributes"].get("contentRating", "None").title():<8}',
                            song["attributes"]["name"],
                        ],
                    ),
                    value=song,
                )
                for song in songs_metadata
                if song.get("attributes")
            ]
            selected = await inquirer.select(
                message="Select which songs to download: (Duration | Rating | Title)",
                choices=choices,
                multiselect=True,
            ).execute_async()
        else:
            selected = songs_metadata

        song_tasks = [
            self.get_single_download_item(
                song_metadata,
                source_context="artist-top-songs",
                artist_folder_name=artist_folder_name,
            )
            for song_metadata in selected
        ]
        download_items = await safe_gather(*song_tasks)

        return download_items

    def millis_to_min_sec(self, millis) -> str:
        minutes, seconds = divmod(millis // 1000, 60)
        return f"{minutes:02}:{seconds:02}"

    def get_url_info(self, url: str) -> UrlInfo | None:
        match = VALID_URL_PATTERN.match(url)
        if not match:
            return None

        return UrlInfo(
            **match.groupdict(),
        )

    async def iter_prepared_download_items(
        self,
        url_info: UrlInfo,
        *,
        preparation_concurrency: int = 8,
        cancel_callback: typing.Callable[[], bool] | None = None,
    ) -> typing.AsyncIterator[tuple[int, int, DownloadItem | None]]:
        """Yield (original position, collection size, item) as each item is ready.

        Position zero announces the total before expensive per-song preparation.
        A bounded queue prevents slow downloads from creating an unbounded backlog.
        """
        if not 1 <= preparation_concurrency <= 32:
            raise ValueError("歌曲信息准备并发数必须在 1 到 32 之间。")
        kind = "song" if url_info.sub_id else url_info.type or url_info.library_type
        if kind not in ALBUM_MEDIA_TYPE | PLAYLIST_MEDIA_TYPE:
            items = await self.get_download_queue(url_info) or []
            yield 0, len(items), None
            for index, item in enumerate(items, 1):
                if cancel_callback and cancel_callback():
                    return
                yield index, len(items), item
            return

        media_id = url_info.sub_id or url_info.id or url_info.library_id
        api = self.interface.apple_music_api
        is_library = url_info.library_id is not None
        if kind in PLAYLIST_MEDIA_TYPE:
            fetch = api.get_library_playlist if is_library else api.get_playlist
        else:
            fetch = api.get_library_album if is_library else api.get_album
        try:
            response = await fetch(media_id)
        except ApiError as exc:
            if exc.status_code == 404:
                yield 0, 0, None
                return
            raise
        if not response or not response.get("data"):
            yield 0, 0, None
            return
        collection = response["data"][0]
        tracks = collection["relationships"]["tracks"]["data"]
        async for page in api.extend_api_data(collection["relationships"]["tracks"]):
            if cancel_callback and cancel_callback():
                return
            tracks.extend(page["data"])
        total = len(tracks)
        yield 0, total, None
        if not total:
            return
        pending = iter(enumerate(tracks, 1))
        ready = asyncio.Queue(maxsize=max(1, preparation_concurrency))
        is_playlist = kind in PLAYLIST_MEDIA_TYPE

        async def prepare_worker():
            cancelled = False
            try:
                for index, metadata in pending:
                    if cancel_callback and cancel_callback():
                        break
                    playlist_tags = None
                    try:
                        if is_playlist and metadata["type"] in SONG_MEDIA_TYPE:
                            playlist_tags = self.base_downloader.get_playlist_tags(collection, metadata)
                            # Preserve position even for duplicate songs and out-of-order completion.
                            playlist_tags.playlist_track = index
                        item = await self.get_single_download_item(
                            metadata, collection if is_playlist else None,
                            **({"playlist_tags_override": playlist_tags} if playlist_tags else {}),
                        )
                    except Exception as exc:
                        item = DownloadItem(media_metadata=metadata,
                            playlist_metadata=collection if is_playlist else None,
                            playlist_tags=playlist_tags, error=exc)
                    await ready.put((index, total, item))
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                if not cancelled:
                    await ready.put(None)

        workers = [asyncio.create_task(prepare_worker())
                   for _ in range(min(preparation_concurrency, total))]
        finished = 0
        try:
            while finished < len(workers):
                value = await ready.get()
                if value is None:
                    finished += 1
                else:
                    yield value
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def get_download_queue(
        self,
        url_info: UrlInfo,
    ) -> list[DownloadItem] | None:
        return await self._get_download_queue(
            "song" if url_info.sub_id else url_info.type or url_info.library_type,
            url_info.sub_id or url_info.id or url_info.library_id,
            url_info.library_id is not None,
        )

    async def _get_download_queue(
        self,
        url_type: str,
        id: str,
        is_library: bool,
    ) -> list[DownloadItem] | None:
        download_items = []

        if url_type in ARTIST_MEDIA_TYPE:
            try:
                artist_response = await self.interface.apple_music_api.get_artist(
                    id,
                )
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if artist_response is None:
                return None

            download_items = await self.get_artist_download_items(
                artist_response["data"][0],
            )

        if url_type in SONG_MEDIA_TYPE:
            try:
                if is_library:
                    song_respose = await self.interface.apple_music_api.get_library_song(id)
                else:
                    song_respose = await self.interface.apple_music_api.get_song(id)
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if song_respose is None:
                return None

            download_items.append(
                await self.get_single_download_item(song_respose["data"][0])
            )

        if url_type in ALBUM_MEDIA_TYPE:
            try:
                if is_library:
                    album_response = (
                        await self.interface.apple_music_api.get_library_album(id)
                    )
                else:
                    album_response = await self.interface.apple_music_api.get_album(id)
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if album_response is None:
                return None

            download_items = await self.get_collection_download_items(
                album_response["data"][0],
            )

        if url_type in PLAYLIST_MEDIA_TYPE:
            try:
                if is_library:
                    playlist_response = (
                        await self.interface.apple_music_api.get_library_playlist(id)
                    )
                else:
                    playlist_response = (
                        await self.interface.apple_music_api.get_playlist(id)
                    )
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if playlist_response is None:
                return None

            download_items = await self.get_collection_download_items(
                playlist_response["data"][0],
            )

        if url_type in MUSIC_VIDEO_MEDIA_TYPE:
            try:
                if is_library:
                    music_video_response = (
                        await self.interface.apple_music_api.get_library_music_video(id)
                    )
                else:
                    music_video_response = (
                        await self.interface.apple_music_api.get_music_video(id)
                    )
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if music_video_response is None:
                return None

            download_items.append(
                await self.get_single_download_item(music_video_response["data"][0])
            )

        if url_type in UPLOADED_VIDEO_MEDIA_TYPE:
            try:
                uploaded_video = (
                    await self.interface.apple_music_api.get_uploaded_video(id)
                )
            except ApiError as e:
                if e.status_code == 404:
                    return None
                raise e

            if uploaded_video is None:
                return None

            download_items.append(
                await self.get_single_download_item(uploaded_video["data"][0])
            )

        return download_items

    async def download(
        self,
        download_item: DownloadItem,
    ) -> DownloadItem:
        try:
            if download_item.flat_filter_result:
                download_item = await self.get_single_download_item_no_filter(
                    download_item.media_metadata,
                    download_item.playlist_metadata,
                    download_item.source_context,
                    download_item.artist_folder_name,
                    **({"playlist_tags_override": download_item.playlist_tags}
                       if download_item.playlist_tags else {}),
                )

            if download_item.error:
                raise download_item.error

            if self._should_skip_existing_media(download_item):
                await self._write_post_download_artifacts(
                    download_item,
                    media_file_exists=True,
                )
                raise MediaFileExists(download_item.final_path)

            await self._download(download_item)
            await self._final_processing(download_item)

            return download_item
        finally:
            if isinstance(download_item, DownloadItem) and not self.skip_processing:
                self.base_downloader.cleanup_temp(download_item.random_uuid)

    async def _download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if (
            self.song_downloader.synced_lyrics_only
            and download_item.media_metadata["type"] not in SONG_MEDIA_TYPE
        ):
            raise SyncedLyricsOnly()

        if self.song_downloader.synced_lyrics_only:
            return

        if (
            self.base_downloader.download_mode == DownloadMode.NM3U8DLRE
            and not self.base_downloader.full_nm3u8dlre_path
        ):
            raise ExecutableNotFound("N_m3u8DL-RE")

        if download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
            if (
                self.music_video_downloader.remux_mode == RemuxMode.FFMPEG
                and not self.base_downloader.full_ffmpeg_path
            ):
                raise ExecutableNotFound("ffmpeg")

            if (
                self.music_video_downloader.remux_mode == RemuxMode.MP4BOX
                and not self.base_downloader.full_mp4box_path
            ):
                raise ExecutableNotFound("MP4Box")

            if (
                download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE
                or self.music_video_downloader.remux_mode == RemuxMode.MP4BOX
            ) and not self.base_downloader.full_mp4decrypt_path:
                raise ExecutableNotFound("mp4decrypt")

        if (
            not download_item.stream_info
            or not download_item.stream_info.audio_track
            or not download_item.stream_info.audio_track.stream_url
            or (
                (
                    not download_item.decryption_key
                    or not download_item.decryption_key.audio_track
                    or not download_item.decryption_key.audio_track.key
                )
                and not download_item.stream_info.audio_track.drm_free
                and not self.base_downloader.use_wrapper
            )
        ):
            raise FormatNotAvailable(download_item.media_metadata["id"])

        if download_item.media_metadata["type"] in SONG_MEDIA_TYPE:
            await self.song_downloader.download(download_item)

        if download_item.media_metadata["type"] in MUSIC_VIDEO_MEDIA_TYPE:
            await self.music_video_downloader.download(download_item)

        if download_item.media_metadata["type"] in UPLOADED_VIDEO_MEDIA_TYPE:
            await self.uploaded_video_downloader.download(download_item)

    def _should_skip_existing_media(
        self,
        download_item: DownloadItem,
    ) -> bool:
        return (
            not self.song_downloader.synced_lyrics_only
            and bool(download_item.final_path)
            and Path(download_item.final_path).exists()
            and not self.base_downloader.overwrite
        )

    async def _write_post_download_artifacts(
        self,
        download_item: DownloadItem,
        *,
        media_file_exists: bool | None = None,
    ) -> None:
        if self.skip_processing:
            return

        final_path_exists = (
            media_file_exists
            if media_file_exists is not None
            else bool(download_item.final_path and Path(download_item.final_path).exists())
        )
        allow_sidecars_without_media = self.song_downloader.synced_lyrics_only

        if (
            download_item.cover_path
            and self.base_downloader.save_cover
            and (final_path_exists or allow_sidecars_without_media)
        ):
            try:
                cover_bytes = await self.interface.get_cover_bytes(download_item.cover_url)
                if cover_bytes:
                    self.base_downloader.write_cover_image(
                        cover_bytes,
                        download_item.cover_path,
                    )
            except Exception as exc:
                self._log_post_download_artifact_warning(
                    download_item,
                    "cover",
                    exc,
                )

        if (
            download_item.lyrics
            and download_item.lyrics.synced
            and not self.song_downloader.no_synced_lyrics
            and (final_path_exists or allow_sidecars_without_media)
            and (
                self.base_downloader.overwrite
                or not Path(download_item.synced_lyrics_path).exists()
            )
        ):
            try:
                self.song_downloader.write_synced_lyrics(
                    download_item.lyrics.synced,
                    download_item.synced_lyrics_path,
                )
            except Exception as exc:
                self._log_post_download_artifact_warning(
                    download_item,
                    "synced-lyrics",
                    exc,
                )

        if (
            download_item.playlist_tags
            and self.base_downloader.save_playlist
            and final_path_exists
        ):
            try:
                self.base_downloader.update_playlist_file(
                    download_item.playlist_file_path,
                    download_item.final_path,
                    download_item.playlist_tags.playlist_track,
                )
            except Exception as exc:
                self._log_post_download_artifact_warning(
                    download_item,
                    "playlist-file",
                    exc,
                )

    def _log_post_download_artifact_warning(
        self,
        download_item: DownloadItem,
        artifact_kind: str,
        error: Exception,
    ) -> None:
        metadata = getattr(download_item, "media_metadata", None) or {}
        attributes = metadata.get("attributes", {}) or {}
        title = attributes.get("name") or metadata.get("id") or "unknown media"
        download_item.sidecar_failures.append(
            {
                "artifact_kind": artifact_kind,
                "error": str(error),
            }
        )
        logger.warning(
            'Post-download artifact "%s" failed for "%s": %s',
            artifact_kind,
            title,
            error,
        )

    async def _final_processing(
        self,
        download_item: DownloadItem,
    ) -> None:
        if self.skip_processing:
            return

        if download_item.staged_path and Path(download_item.staged_path).exists():
            self.base_downloader.move_to_final_path(
                download_item.staged_path,
                download_item.final_path,
            )

        await self._write_post_download_artifacts(download_item)
