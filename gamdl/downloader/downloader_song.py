from pathlib import Path

from ..api.exceptions import ApiError
from ..interface.enums import CoverFormat, SongCodec, SyncedLyricsFormat
from ..interface.interface_song import AppleMusicSongInterface
from ..interface.types import DecryptionKeyAv, PlaylistTags
from .amdecrypt import decrypt_file, decrypt_file_hex
from .downloader_base import AppleMusicBaseDownloader
from .exceptions import ExperimentalCodecRequiresWrapper, FormatNotAvailable
from .types import DownloadItem


class AppleMusicSongDownloader(AppleMusicBaseDownloader):
    def __init__(
        self,
        base_downloader: AppleMusicBaseDownloader,
        interface: AppleMusicSongInterface,
        codec_priority: SongCodec = [SongCodec.AAC_LEGACY],
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        no_synced_lyrics: bool = False,
        synced_lyrics_only: bool = False,
        use_album_date: bool = False,
        fetch_extra_tags: bool = False,
    ):
        self.__dict__.update(base_downloader.__dict__)
        self.interface = interface
        self.codec_priority = codec_priority
        self.synced_lyrics_format = synced_lyrics_format
        self.no_synced_lyrics = no_synced_lyrics
        self.synced_lyrics_only = synced_lyrics_only
        self.use_album_date = use_album_date
        self.fetch_extra_tags = fetch_extra_tags

    async def get_download_item(
        self,
        song_metadata: dict,
        playlist_metadata: dict = None,
        playlist_tags_override: PlaylistTags | None = None,
        source_context: str | None = None,
        artist_folder_name: str | None = None,
    ) -> DownloadItem:
        download_item = DownloadItem()

        download_item.media_metadata = song_metadata
        download_item.playlist_metadata = playlist_metadata
        download_item.source_context = source_context
        download_item.artist_folder_name = artist_folder_name

        song_id = self.interface.get_media_id_of_library_media(song_metadata)

        download_item.lyrics = await self.interface.get_lyrics(
            song_metadata,
            self.synced_lyrics_format,
        )

        is_library = (
            song_metadata.get("type") == "library-songs"
            or song_metadata.get("attributes", {})
            .get("playParams", {})
            .get("isLibrary", False)
        )
        webplayback_id = song_metadata["id"] if is_library else song_id
        webplayback = await self.interface.apple_music_api.get_webplayback(
            webplayback_id,
            is_library=is_library,
        )
        download_item.media_tags = await self.interface.get_tags(
            webplayback,
            download_item.lyrics.unsynced if download_item.lyrics else None,
            self.use_album_date,
        )
        if self.fetch_extra_tags:
            download_item.extra_tags = await self.interface.get_extra_tags(
                song_metadata,
            )

        if playlist_tags_override:
            download_item.playlist_tags = playlist_tags_override
            download_item.playlist_file_path = self.get_playlist_file_path(
                download_item.playlist_tags,
            )
        elif playlist_metadata:
            download_item.playlist_tags = self.get_playlist_tags(
                playlist_metadata,
                song_metadata,
            )
            download_item.playlist_file_path = self.get_playlist_file_path(
                download_item.playlist_tags,
            )

        download_item.final_path = self.get_final_path(
            download_item.media_tags,
            ".m4a",
            download_item.playlist_tags,
            download_item.source_context,
            download_item.artist_folder_name,
        )
        download_item.synced_lyrics_path = self.get_lyrics_synced_path(
            download_item.final_path,
        )

        if self.synced_lyrics_only:
            return download_item

        for codec in self.codec_priority:
            download_item.stream_info = await self.interface.get_stream_info(
                codec,
                song_metadata,
                webplayback,
                is_library=is_library,
            )
            if download_item.stream_info:
                if self.use_wrapper and not download_item.stream_info.audio_track.legacy:
                    wrapper_master_url = await self.get_wrapper_song_m3u8_url(song_id)
                    if wrapper_master_url:
                        wrapper_stream_info = await self.interface.get_stream_info(
                            codec,
                            song_metadata,
                            webplayback,
                            m3u8_master_url=wrapper_master_url,
                        )
                        if wrapper_stream_info:
                            download_item.stream_info = wrapper_stream_info
                break

        if not download_item.stream_info or not download_item.stream_info.audio_track:
            raise FormatNotAvailable(song_id)

        if download_item.stream_info.audio_track.legacy:
            download_item.decryption_key = (
                await self.interface.get_decryption_key_legacy(
                    download_item.stream_info,
                    self.cdm,
                )
            )
        elif download_item.stream_info.audio_track.drm_free:
            download_item.decryption_key = None
        elif (
            not self.use_wrapper
            and download_item.stream_info
            and download_item.stream_info.audio_track.widevine_pssh
        ):
            try:
                download_item.decryption_key = await self.interface.get_decryption_key(
                    download_item.stream_info,
                    self.cdm,
                )
            except ApiError as exc:
                if exc.status_code == 200 and '{"status":-1002}' in str(exc):
                    raise ExperimentalCodecRequiresWrapper(codec.value) from exc
                raise

        download_item.cover_url_template = self.interface.get_cover_url_template(
            song_metadata,
            self.cover_format,
        )
        download_item.cover_url = self.interface.get_cover_url(
            song_metadata,
            download_item.cover_url_template,
            self.cover_size,
            self.cover_format,
        )

        download_item.random_uuid = self.get_random_uuid()
        if download_item.stream_info and download_item.stream_info.file_format:
            download_item.staged_path = self.get_temp_path(
                song_id,
                download_item.random_uuid,
                "staged",
                "." + download_item.stream_info.file_format.value,
            )
        else:
            download_item.staged_path = None

        cover_file_extension = await self.interface.get_cover_file_extension(
            download_item.cover_url,
            self.cover_format,
        )
        if cover_file_extension:
            download_item.cover_path = self.get_cover_path(
                download_item.final_path,
                cover_file_extension,
                download_item.source_context,
                album=download_item.media_tags.album,
            )

        return download_item

    async def decrypt_amdecrypt(
        self,
        input_path: str,
        output_path: str,
        media_id: str,
        fairplay_key: str,
        use_single_content_key: bool = False,
    ) -> None:
        await decrypt_file(
            self.wrapper_decrypt_ip,
            media_id,
            fairplay_key,
            input_path,
            output_path,
            use_single_content_key=use_single_content_key,
            file_backed_samples=True,
        )

    async def decrypt_amdecrypt_hex(
        self,
        input_path: str,
        output_path: str,
        decryption_key: str,
        legacy: bool = False,
        use_cenc: bool = False,
        use_single_content_key: bool = False,
    ) -> None:
        await decrypt_file_hex(
            input_path,
            output_path,
            decryption_key,
            legacy=legacy,
            use_cenc=use_cenc,
            use_single_content_key=use_single_content_key,
            file_backed_samples=True,
        )

    async def stage(
        self,
        encrypted_path: str,
        staged_path: str,
        decryption_key: DecryptionKeyAv,
        legacy: bool,
        media_id: str,
        fairplay_key: str,
        use_cenc: bool = False,
        use_single_content_key: bool = False,
    ):
        if self.use_wrapper and not legacy:
            await self.decrypt_amdecrypt(
                encrypted_path,
                staged_path,
                media_id,
                fairplay_key,
                use_single_content_key=use_single_content_key,
            )
        else:
            await self.decrypt_amdecrypt_hex(
                encrypted_path,
                staged_path,
                decryption_key.audio_track.key,
                legacy,
                use_cenc=use_cenc,
                use_single_content_key=use_single_content_key,
            )

    def get_lyrics_synced_path(self, final_path: str) -> str:
        return str(Path(final_path).with_suffix("." + self.synced_lyrics_format.value))

    def get_cover_path(
        self,
        final_path: str,
        file_extension: str,
        source_context: str | None = None,
        album: str | None = None,
    ) -> str:
        filename = self.sanitize_string("cover-" + (album or "Unknown Album"), file_ext=file_extension)
        return str(Path(final_path).parent / filename)

    def write_synced_lyrics(
        self,
        synced_lyrics: str,
        lyrics_synced_path: str,
    ):
        Path(lyrics_synced_path).parent.mkdir(parents=True, exist_ok=True)
        Path(lyrics_synced_path).write_text(synced_lyrics, encoding="utf8")

    async def download(
        self,
        download_item: DownloadItem,
    ) -> None:
        if self.synced_lyrics_only:
            return

        if download_item.stream_info.audio_track.drm_free:
            await self.download_stream(
                download_item.stream_info.audio_track.stream_url,
                download_item.staged_path,
            )
        else:
            encrypted_path = self.get_temp_path(
                download_item.media_metadata["id"],
                download_item.random_uuid,
                "encrypted",
                ".m4a",
            )
            await self.download_stream(
                download_item.stream_info.audio_track.stream_url,
                encrypted_path,
            )

            await self.stage(
                encrypted_path,
                download_item.staged_path,
                download_item.decryption_key,
                download_item.stream_info.audio_track.legacy,
                download_item.media_metadata["id"],
                download_item.stream_info.audio_track.fairplay_key,
                getattr(download_item.stream_info.audio_track, "use_cenc", False),
                getattr(
                    download_item.stream_info.audio_track,
                    "use_single_content_key",
                    False,
                ),
            )

        cover_bytes = (
            await self.interface.get_cover_bytes(download_item.cover_url)
            if self.cover_format != CoverFormat.RAW
            else None
        )
        await self.apply_tags(
            download_item.staged_path,
            download_item.media_tags,
            cover_bytes,
            download_item.extra_tags,
        )
