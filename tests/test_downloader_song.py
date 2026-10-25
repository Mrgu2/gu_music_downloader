import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gamdl.downloader import AppleMusicBaseDownloader, AppleMusicDownloader, AppleMusicSongDownloader
from gamdl.downloader.exceptions import FormatNotAvailable, MediaFileExists
from gamdl.downloader.types import DownloadItem
from gamdl.interface import SongCodec
from gamdl.interface.enums import MediaFileFormat
from gamdl.interface.types import Lyrics, MediaTags, PlaylistTags, StreamInfo, StreamInfoAv


class AppleMusicSongDownloaderTests(unittest.TestCase):
    def _build_base_downloader(self, tempdir: str) -> AppleMusicBaseDownloader:
        with patch.object(AppleMusicBaseDownloader, "initialize", return_value=None):
            downloader = AppleMusicBaseDownloader(
                output_path=str(Path(tempdir) / "downloads"),
                temp_path=str(Path(tempdir) / "tmp"),
            )
        downloader.cdm = object()
        return downloader

    def test_get_download_item_raises_format_not_available_when_no_stream_info(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            interface = SimpleNamespace(
                get_media_id_of_library_media=MagicMock(return_value="song-123"),
                get_lyrics=AsyncMock(return_value=None),
                apple_music_api=SimpleNamespace(
                    get_webplayback=AsyncMock(return_value={"songList": [{"songId": "song-123"}]})
                ),
                get_tags=AsyncMock(
                    return_value=MediaTags(
                        album="Album",
                        artist="Artist",
                        title="Track Title",
                        track=1,
                    )
                ),
                get_stream_info=AsyncMock(return_value=None),
            )
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=interface,
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            song_metadata = {
                "id": "song-123",
                "type": "songs",
                "attributes": {"playParams": {"id": "song-123"}},
            }

            with self.assertRaisesRegex(
                FormatNotAvailable,
                "Requested format is not available for media ID: song-123",
            ):
                asyncio.run(downloader.get_download_item(song_metadata))

    def test_get_final_path_uses_top_songs_directory_for_artist_top_songs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)

            final_path = base_downloader.get_final_path(
                MediaTags(
                    album="Album",
                    artist="Artist",
                    title="Track Title",
                    title_id=12345,
                    track=1,
                ),
                ".m4a",
                None,
                "artist-top-songs",
            )

            self.assertEqual(
                Path(final_path),
                Path(tempdir) / "downloads" / "Artist" / "Top Songs" / "Track Title [12345].m4a",
            )

    def test_get_final_path_uses_selected_artist_folder_for_artist_top_songs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)

            final_path = base_downloader.get_final_path(
                MediaTags(
                    album="Album",
                    artist="Artist A & Artist B",
                    title="Duet",
                    title_id=42,
                    track=1,
                ),
                ".m4a",
                None,
                "artist-top-songs",
                "Artist A",
            )

            self.assertEqual(
                Path(final_path),
                Path(tempdir) / "downloads" / "Artist A" / "Top Songs" / "Duet [42].m4a",
            )

    def test_get_final_path_preserves_title_id_suffix_when_truncate_is_enabled(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(AppleMusicBaseDownloader, "initialize", return_value=None):
                base_downloader = AppleMusicBaseDownloader(
                    output_path=str(Path(tempdir) / "downloads"),
                    temp_path=str(Path(tempdir) / "tmp"),
                    truncate=20,
                )
            base_downloader.cdm = object()

            final_path = base_downloader.get_final_path(
                MediaTags(
                    artist="Artist",
                    title="Very Long Track Title",
                    title_id=12345,
                ),
                ".m4a",
                None,
                "artist-top-songs",
            )

            self.assertTrue(Path(final_path).name.endswith(" [12345].m4a"))
            self.assertLessEqual(len(Path(final_path).name), 20)

    def test_get_final_path_keeps_full_title_id_suffix_when_truncate_is_tiny(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(AppleMusicBaseDownloader, "initialize", return_value=None):
                base_downloader = AppleMusicBaseDownloader(
                    output_path=str(Path(tempdir) / "downloads"),
                    temp_path=str(Path(tempdir) / "tmp"),
                    truncate=10,
                )
            base_downloader.cdm = object()

            final_path = base_downloader.get_final_path(
                MediaTags(
                    artist="Artist",
                    title="Very Long Track Title",
                    title_id=12345,
                ),
                ".m4a",
                None,
                "artist-top-songs",
            )

            self.assertTrue(Path(final_path).name.endswith(" [12345].m4a"))

    def test_get_final_path_keeps_full_title_id_suffix_when_truncate_is_too_small(self):
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.object(AppleMusicBaseDownloader, "initialize", return_value=None):
                base_downloader = AppleMusicBaseDownloader(
                    output_path=str(Path(tempdir) / "downloads"),
                    temp_path=str(Path(tempdir) / "tmp"),
                    truncate=10,
                )
            base_downloader.cdm = object()

            final_path = base_downloader.get_final_path(
                MediaTags(
                    artist="Artist",
                    title="Very Long Track Title",
                    title_id=123456789,
                ),
                ".m4a",
                None,
                "artist-top-songs",
            )

            self.assertEqual(
                Path(final_path).name,
                "V [123456789].m4a",
            )

    def test_get_download_item_applies_top_songs_paths_during_item_construction(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            interface = SimpleNamespace(
                get_media_id_of_library_media=MagicMock(return_value="song-123"),
                get_lyrics=AsyncMock(return_value=Lyrics(synced="[00:00.00]test", unsynced="test")),
                apple_music_api=SimpleNamespace(
                    get_webplayback=AsyncMock(return_value={"songList": [{"songId": "song-123"}]})
                ),
                get_tags=AsyncMock(
                    return_value=MediaTags(
                        album="Album",
                        artist="Artist A & Artist B",
                        title="Track Title",
                        title_id=12345,
                        track=1,
                    )
                ),
                get_stream_info=AsyncMock(
                    return_value=SimpleNamespace(
                        audio_track=SimpleNamespace(legacy=True, stream_url="https://example.com/stream", fairplay_key="fp", widevine_pssh=None),
                        file_format=SimpleNamespace(value="m4a"),
                    )
                ),
                get_decryption_key_legacy=AsyncMock(return_value=SimpleNamespace(audio_track=SimpleNamespace(key="abc"))),
                get_cover_url_template=MagicMock(return_value="https://example.com/{w}x{h}.jpg"),
                get_cover_url=MagicMock(return_value="https://example.com/cover.jpg"),
                get_cover_file_extension=AsyncMock(return_value=".jpg"),
            )
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=interface,
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            song_metadata = {
                "id": "song-123",
                "type": "songs",
                "attributes": {"name": "Track Title", "playParams": {"id": "song-123"}},
            }

            download_item = asyncio.run(
                downloader.get_download_item(
                    song_metadata,
                    source_context="artist-top-songs",
                    artist_folder_name="Artist A",
                )
            )

            self.assertEqual(
                Path(download_item.final_path),
                Path(tempdir) / "downloads" / "Artist A" / "Top Songs" / "Track Title [12345].m4a",
            )
            self.assertEqual(
                Path(download_item.cover_path),
                Path(tempdir) / "downloads" / "Artist A" / "Top Songs" / "cover-Album.jpg",
            )

    def test_get_download_item_uses_library_webplayback_and_drm_free_stream(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            stream_info = StreamInfoAv(
                media_id="i.library-song",
                audio_track=StreamInfo(
                    stream_url="https://example.com/library.m4a",
                    drm_free=True,
                    legacy=False,
                ),
                file_format=MediaFileFormat.M4A,
            )
            interface = SimpleNamespace(
                get_media_id_of_library_media=MagicMock(return_value="catalog-123"),
                get_lyrics=AsyncMock(return_value=None),
                apple_music_api=SimpleNamespace(
                    get_webplayback=AsyncMock(
                        return_value={"songList": [{"songId": "i.library-song"}]}
                    )
                ),
                get_tags=AsyncMock(
                    return_value=MediaTags(
                        album="Library",
                        artist="Artist",
                        title="Track Title",
                        track=1,
                    )
                ),
                get_stream_info=AsyncMock(return_value=stream_info),
                get_cover_url_template=MagicMock(return_value="https://example.com/{w}x{h}.jpg"),
                get_cover_url=MagicMock(return_value="https://example.com/cover.jpg"),
                get_cover_file_extension=AsyncMock(return_value=".jpg"),
            )
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=interface,
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            song_metadata = {
                "id": "i.library-song",
                "type": "library-songs",
                "attributes": {"name": "Track Title", "playParams": {"isLibrary": True}},
            }

            download_item = asyncio.run(downloader.get_download_item(song_metadata))

            interface.apple_music_api.get_webplayback.assert_awaited_once_with(
                "i.library-song",
                is_library=True,
            )
            self.assertTrue(download_item.stream_info.audio_track.drm_free)
            self.assertIsNone(download_item.decryption_key)
            self.assertTrue(interface.get_stream_info.await_args.kwargs["is_library"])

    def test_get_final_path_keeps_existing_album_structure_outside_top_songs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)

            final_path = base_downloader.get_final_path(
                MediaTags(
                    album="Album",
                    album_artist="Artist",
                    artist="Artist",
                    title="Track Title",
                    track=1,
                ),
                ".m4a",
                None,
            )

            self.assertEqual(
                Path(final_path),
                Path(tempdir) / "downloads" / "Artist" / "Album" / "01 Track Title.m4a",
            )

    def test_get_cover_path_uses_per_song_cover_for_artist_top_songs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=SimpleNamespace(),
                codec_priority=[SongCodec.AAC_LEGACY],
            )

            cover_path = downloader.get_cover_path(
                str(Path(tempdir) / "downloads" / "Artist" / "Top Songs" / "Track Title [12345].m4a"),
                ".jpg",
                "artist-top-songs",
            )

            self.assertEqual(
                Path(cover_path),
                Path(tempdir) / "downloads" / "Artist" / "Top Songs" / "cover-Unknown Album.jpg",
            )

    def test_get_cover_path_keeps_shared_cover_outside_top_songs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=SimpleNamespace(),
                codec_priority=[SongCodec.AAC_LEGACY],
            )

            cover_path = downloader.get_cover_path(
                str(Path(tempdir) / "downloads" / "Artist" / "Album" / "01 Track Title.m4a"),
                ".jpg",
            )

            self.assertEqual(
                Path(cover_path),
                Path(tempdir) / "downloads" / "Artist" / "Album" / "cover-Unknown Album.jpg",
            )

    def test_single_download_item_captures_format_not_available_error(self):
        song_metadata = {
            "id": "song-123",
            "type": "songs",
            "attributes": {"name": "Track Title", "playParams": {"id": "song-123"}},
        }
        base_downloader = SimpleNamespace(is_media_streamable=MagicMock(return_value=True))
        song_downloader = SimpleNamespace(
            get_download_item=AsyncMock(side_effect=FormatNotAvailable("song-123"))
        )
        downloader = AppleMusicDownloader(
            interface=MagicMock(),
            base_downloader=base_downloader,
            song_downloader=song_downloader,
            music_video_downloader=None,
            uploaded_video_downloader=None,
        )

        result = asyncio.run(downloader.get_single_download_item_no_filter(song_metadata))

        self.assertIsInstance(result.error, FormatNotAvailable)
        self.assertEqual(
            str(result.error),
            "Requested format is not available for media ID: song-123",
        )

    def test_artist_top_songs_items_are_marked_with_source_context(self):
        downloader = AppleMusicDownloader(
            interface=MagicMock(),
            base_downloader=SimpleNamespace(),
            song_downloader=SimpleNamespace(),
            music_video_downloader=None,
            uploaded_video_downloader=None,
        )
        download_item = DownloadItem(media_metadata={"id": "song-123", "type": "song"})

        with patch.object(
            downloader,
            "get_single_download_item",
            new=AsyncMock(side_effect=lambda *args, **kwargs: DownloadItem(
                media_metadata=download_item.media_metadata,
                source_context=kwargs.get("source_context"),
                artist_folder_name=kwargs.get("artist_folder_name"),
            )),
        ):
            result = asyncio.run(
                downloader.get_artist_songs_download_items(
                    [{"id": "song-123", "type": "songs", "attributes": {"name": "Track Title"}}],
                    select_all=True,
                    artist_folder_name="Artist A",
                )
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source_context, "artist-top-songs")
        self.assertEqual(result[0].artist_folder_name, "Artist A")

    def test_get_artist_download_items_returns_empty_when_auto_selected_view_is_missing(self):
        downloader = AppleMusicDownloader(
            interface=SimpleNamespace(
                apple_music_api=SimpleNamespace(
                    extend_api_data=AsyncMock(),
                )
            ),
            base_downloader=SimpleNamespace(),
            song_downloader=SimpleNamespace(),
            music_video_downloader=None,
            uploaded_video_downloader=None,
            artist_auto_select=MagicMock(path_key=("views", "top-songs")),
        )
        artist_metadata = {
            "attributes": {"name": "Artist A"},
            "views": {},
        }

        result = asyncio.run(downloader.get_artist_download_items(artist_metadata))

        self.assertEqual(result, [])
        downloader.interface.apple_music_api.extend_api_data.assert_not_called()

    def test_library_song_url_uses_library_song_api(self):
        song_metadata = {
            "id": "i.library-song",
            "type": "library-songs",
            "attributes": {"name": "Library Track", "playParams": {"isLibrary": True}},
        }
        interface = SimpleNamespace(
            apple_music_api=SimpleNamespace(
                get_library_song=AsyncMock(return_value={"data": [song_metadata]}),
                get_song=AsyncMock(),
            )
        )
        base_downloader = SimpleNamespace(is_media_streamable=MagicMock(return_value=True))
        song_downloader = SimpleNamespace(
            get_download_item=AsyncMock(return_value=DownloadItem(media_metadata=song_metadata))
        )
        downloader = AppleMusicDownloader(
            interface=interface,
            base_downloader=base_downloader,
            song_downloader=song_downloader,
            music_video_downloader=None,
            uploaded_video_downloader=None,
        )
        url_info = downloader.get_url_info(
            "https://music.apple.com/library/songs/i.librarySong123"
        )

        result = asyncio.run(downloader.get_download_queue(url_info))

        self.assertEqual(len(result), 1)
        interface.apple_music_api.get_library_song.assert_awaited_once_with(
            "i.librarySong123"
        )
        interface.apple_music_api.get_song.assert_not_called()

    def test_library_music_video_url_uses_library_music_video_api(self):
        music_video_metadata = {
            "id": "i.library-video",
            "type": "library-music-videos",
            "attributes": {"name": "Library Video", "playParams": {"isLibrary": True}},
        }
        interface = SimpleNamespace(
            apple_music_api=SimpleNamespace(
                get_library_music_video=AsyncMock(return_value={"data": [music_video_metadata]}),
                get_music_video=AsyncMock(),
            )
        )
        base_downloader = SimpleNamespace(is_media_streamable=MagicMock(return_value=True))
        music_video_downloader = SimpleNamespace(
            get_download_item=AsyncMock(
                return_value=DownloadItem(media_metadata=music_video_metadata)
            )
        )
        downloader = AppleMusicDownloader(
            interface=interface,
            base_downloader=base_downloader,
            song_downloader=None,
            music_video_downloader=music_video_downloader,
            uploaded_video_downloader=None,
        )
        url_info = downloader.get_url_info(
            "https://music.apple.com/library/music-videos/i.libraryVideo123"
        )

        result = asyncio.run(downloader.get_download_queue(url_info))

        self.assertEqual(len(result), 1)
        interface.apple_music_api.get_library_music_video.assert_awaited_once_with(
            "i.libraryVideo123"
        )
        interface.apple_music_api.get_music_video.assert_not_called()

    def test_download_failure_does_not_write_sidecars_or_playlist_file(self):
        interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
        base_downloader = SimpleNamespace(
            overwrite=False,
            save_cover=True,
            save_playlist=True,
            write_cover_image=MagicMock(),
            update_playlist_file=MagicMock(),
            move_to_final_path=MagicMock(),
            cleanup_temp=MagicMock(),
        )
        song_downloader = SimpleNamespace(
            synced_lyrics_only=False,
            no_synced_lyrics=False,
            write_synced_lyrics=MagicMock(),
        )
        downloader = AppleMusicDownloader(
            interface=interface,
            base_downloader=base_downloader,
            song_downloader=song_downloader,
            music_video_downloader=None,
            uploaded_video_downloader=None,
        )
        download_item = DownloadItem(
            media_metadata={"id": "song-123", "type": "song"},
            lyrics=Lyrics(synced="[00:00.00]test"),
            playlist_tags=PlaylistTags(
                playlist_artist="Foo",
                playlist_id=1,
                playlist_title="Bar",
                playlist_track=2,
            ),
            random_uuid="abc123",
            final_path="/tmp/Track.m4a",
            cover_path="/tmp/Cover.jpg",
            cover_url="https://example.com/cover.jpg",
            synced_lyrics_path="/tmp/Track.lrc",
            playlist_file_path="/tmp/Playlist.m3u8",
        )

        with patch.object(downloader, "_download", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                asyncio.run(downloader.download(download_item))

        base_downloader.write_cover_image.assert_not_called()
        song_downloader.write_synced_lyrics.assert_not_called()
        base_downloader.update_playlist_file.assert_not_called()
        base_downloader.cleanup_temp.assert_called_once_with("abc123")

    def test_successful_download_writes_sidecars_and_playlist_after_media_exists(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            final_path = temp_path / "Track.m4a"
            staged_path = temp_path / "Track.staged.m4a"
            staged_path.write_text("audio", encoding="utf-8")
            cover_path = temp_path / "Cover.jpg"
            lyrics_path = temp_path / "Track.lrc"
            playlist_path = temp_path / "Playlist.m3u8"

            interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
            base_downloader = SimpleNamespace(
                overwrite=False,
                save_cover=True,
                save_playlist=True,
                write_cover_image=MagicMock(),
                update_playlist_file=MagicMock(),
                cleanup_temp=MagicMock(),
            )

            def move_to_final_path(source, target):
                Path(target).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
                Path(source).unlink()

            base_downloader.move_to_final_path = MagicMock(side_effect=move_to_final_path)
            song_downloader = SimpleNamespace(
                synced_lyrics_only=False,
                no_synced_lyrics=False,
                write_synced_lyrics=MagicMock(),
            )
            downloader = AppleMusicDownloader(
                interface=interface,
                base_downloader=base_downloader,
                song_downloader=song_downloader,
                music_video_downloader=None,
                uploaded_video_downloader=None,
            )
            download_item = DownloadItem(
                media_metadata={"id": "song-123", "type": "song"},
                lyrics=Lyrics(synced="[00:00.00]test"),
                playlist_tags=PlaylistTags(
                    playlist_artist="Foo",
                    playlist_id=1,
                    playlist_title="Bar",
                    playlist_track=2,
                ),
                random_uuid="abc123",
                staged_path=str(staged_path),
                final_path=str(final_path),
                cover_path=str(cover_path),
                cover_url="https://example.com/cover.jpg",
                synced_lyrics_path=str(lyrics_path),
                playlist_file_path=str(playlist_path),
            )

            with patch.object(downloader, "_download", new=AsyncMock(return_value=None)):
                asyncio.run(downloader.download(download_item))

            self.assertTrue(final_path.exists())
            base_downloader.move_to_final_path.assert_called_once()
            base_downloader.write_cover_image.assert_called_once_with(b"cover", str(cover_path))
            song_downloader.write_synced_lyrics.assert_called_once_with(
                "[00:00.00]test",
                str(lyrics_path),
            )
            base_downloader.update_playlist_file.assert_called_once_with(
                str(playlist_path),
                str(final_path),
                2,
            )

    def test_existing_media_skip_still_updates_playlist_and_sidecars(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            final_path = temp_path / "Track.m4a"
            final_path.write_text("audio", encoding="utf-8")
            cover_path = temp_path / "Cover.jpg"
            lyrics_path = temp_path / "Track.lrc"
            playlist_path = temp_path / "Playlist.m3u8"

            interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
            base_downloader = SimpleNamespace(
                overwrite=False,
                save_cover=True,
                save_playlist=True,
                write_cover_image=MagicMock(),
                update_playlist_file=MagicMock(),
                move_to_final_path=MagicMock(),
                cleanup_temp=MagicMock(),
            )
            song_downloader = SimpleNamespace(
                synced_lyrics_only=False,
                no_synced_lyrics=False,
                write_synced_lyrics=MagicMock(),
            )
            downloader = AppleMusicDownloader(
                interface=interface,
                base_downloader=base_downloader,
                song_downloader=song_downloader,
                music_video_downloader=None,
                uploaded_video_downloader=None,
            )
            download_item = DownloadItem(
                media_metadata={"id": "song-123", "type": "song"},
                lyrics=Lyrics(synced="[00:00.00]test"),
                playlist_tags=PlaylistTags(
                    playlist_artist="Foo",
                    playlist_id=1,
                    playlist_title="Bar",
                    playlist_track=2,
                ),
                random_uuid="abc123",
                final_path=str(final_path),
                cover_path=str(cover_path),
                cover_url="https://example.com/cover.jpg",
                synced_lyrics_path=str(lyrics_path),
                playlist_file_path=str(playlist_path),
            )

            with self.assertRaises(MediaFileExists):
                asyncio.run(downloader.download(download_item))

            base_downloader.write_cover_image.assert_called_once_with(b"cover", str(cover_path))
            song_downloader.write_synced_lyrics.assert_called_once_with(
                "[00:00.00]test",
                str(lyrics_path),
            )
            base_downloader.update_playlist_file.assert_called_once_with(
                str(playlist_path),
                str(final_path),
                2,
            )

    def test_sidecar_failure_does_not_fail_successful_media_download(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            final_path = temp_path / "Track.m4a"
            staged_path = temp_path / "Track.staged.m4a"
            staged_path.write_text("audio", encoding="utf-8")
            cover_path = temp_path / "Cover.jpg"
            lyrics_path = temp_path / "Track.lrc"
            playlist_path = temp_path / "Playlist.m3u8"

            interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
            base_downloader = SimpleNamespace(
                overwrite=False,
                save_cover=True,
                save_playlist=True,
                update_playlist_file=MagicMock(),
                cleanup_temp=MagicMock(),
            )

            def move_to_final_path(source, target):
                Path(target).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
                Path(source).unlink()

            base_downloader.move_to_final_path = MagicMock(side_effect=move_to_final_path)
            base_downloader.write_cover_image = MagicMock(side_effect=RuntimeError("cover boom"))
            song_downloader = SimpleNamespace(
                synced_lyrics_only=False,
                no_synced_lyrics=False,
                write_synced_lyrics=MagicMock(),
            )
            downloader = AppleMusicDownloader(
                interface=interface,
                base_downloader=base_downloader,
                song_downloader=song_downloader,
                music_video_downloader=None,
                uploaded_video_downloader=None,
            )
            download_item = DownloadItem(
                media_metadata={"id": "song-123", "type": "song", "attributes": {"name": "Track 1"}},
                lyrics=Lyrics(synced="[00:00.00]test"),
                playlist_tags=PlaylistTags(
                    playlist_artist="Foo",
                    playlist_id=1,
                    playlist_title="Bar",
                    playlist_track=2,
                ),
                random_uuid="abc123",
                staged_path=str(staged_path),
                final_path=str(final_path),
                cover_path=str(cover_path),
                cover_url="https://example.com/cover.jpg",
                synced_lyrics_path=str(lyrics_path),
                playlist_file_path=str(playlist_path),
            )

            with (
                patch.object(downloader, "_download", new=AsyncMock(return_value=None)),
                self.assertLogs("gamdl.downloader", level="WARNING") as logs,
            ):
                result = asyncio.run(downloader.download(download_item))

            self.assertIs(result, download_item)
            self.assertTrue(final_path.exists())
            song_downloader.write_synced_lyrics.assert_called_once_with(
                "[00:00.00]test",
                str(lyrics_path),
            )
            base_downloader.update_playlist_file.assert_called_once_with(
                str(playlist_path),
                str(final_path),
                2,
            )
            self.assertTrue(any("cover" in line and "Track 1" in line for line in logs.output))

    def test_sidecar_failure_does_not_override_media_file_exists_skip(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            final_path = temp_path / "Track.m4a"
            final_path.write_text("audio", encoding="utf-8")
            cover_path = temp_path / "Cover.jpg"
            lyrics_path = temp_path / "Track.lrc"
            playlist_path = temp_path / "Playlist.m3u8"

            interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
            base_downloader = SimpleNamespace(
                overwrite=False,
                save_cover=True,
                save_playlist=True,
                update_playlist_file=MagicMock(),
                move_to_final_path=MagicMock(),
                cleanup_temp=MagicMock(),
            )
            base_downloader.write_cover_image = MagicMock(side_effect=RuntimeError("cover boom"))
            song_downloader = SimpleNamespace(
                synced_lyrics_only=False,
                no_synced_lyrics=False,
                write_synced_lyrics=MagicMock(),
            )
            downloader = AppleMusicDownloader(
                interface=interface,
                base_downloader=base_downloader,
                song_downloader=song_downloader,
                music_video_downloader=None,
                uploaded_video_downloader=None,
            )
            download_item = DownloadItem(
                media_metadata={"id": "song-123", "type": "song", "attributes": {"name": "Track 1"}},
                lyrics=Lyrics(synced="[00:00.00]test"),
                playlist_tags=PlaylistTags(
                    playlist_artist="Foo",
                    playlist_id=1,
                    playlist_title="Bar",
                    playlist_track=2,
                ),
                random_uuid="abc123",
                final_path=str(final_path),
                cover_path=str(cover_path),
                cover_url="https://example.com/cover.jpg",
                synced_lyrics_path=str(lyrics_path),
                playlist_file_path=str(playlist_path),
            )

            with self.assertLogs("gamdl.downloader", level="WARNING") as logs:
                with self.assertRaises(MediaFileExists):
                    asyncio.run(downloader.download(download_item))

            song_downloader.write_synced_lyrics.assert_called_once_with(
                "[00:00.00]test",
                str(lyrics_path),
            )
            base_downloader.update_playlist_file.assert_called_once_with(
                str(playlist_path),
                str(final_path),
                2,
            )
            self.assertTrue(any("cover" in line and "Track 1" in line for line in logs.output))

    def test_drm_free_song_download_skips_decryption_stage(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            interface = SimpleNamespace(get_cover_bytes=AsyncMock(return_value=b"cover"))
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=interface,
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            downloader.download_stream = AsyncMock()
            downloader.stage = AsyncMock()
            downloader.apply_tags = AsyncMock()

            download_item = DownloadItem(
                media_metadata={"id": "i.library-song", "type": "library-songs"},
                random_uuid="abc123",
                staged_path=str(Path(tempdir) / "staged.m4a"),
                stream_info=StreamInfoAv(
                    audio_track=StreamInfo(
                        stream_url="https://example.com/library.m4a",
                        drm_free=True,
                        legacy=False,
                    )
                ),
                media_tags=MediaTags(title="Track Title"),
                cover_url="https://example.com/cover.jpg",
            )

            asyncio.run(downloader.download(download_item))

            downloader.download_stream.assert_awaited_once_with(
                "https://example.com/library.m4a",
                str(Path(tempdir) / "staged.m4a"),
            )
            downloader.stage.assert_not_awaited()
            downloader.apply_tags.assert_awaited_once()

    def test_stage_forwards_cenc_and_single_content_key_to_hex_decrypt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=SimpleNamespace(),
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            downloader.use_wrapper = False
            downloader.decrypt_amdecrypt_hex = AsyncMock()
            decryption_key = SimpleNamespace(audio_track=SimpleNamespace(key="00" * 16))

            asyncio.run(
                downloader.stage(
                    "/tmp/encrypted.m4a",
                    "/tmp/staged.m4a",
                    decryption_key,
                    False,
                    "song-123",
                    "skd://track",
                    use_cenc=True,
                    use_single_content_key=True,
                )
            )

            downloader.decrypt_amdecrypt_hex.assert_awaited_once_with(
                "/tmp/encrypted.m4a",
                "/tmp/staged.m4a",
                "00" * 16,
                False,
                use_cenc=True,
                use_single_content_key=True,
            )

    def test_stage_forwards_single_content_key_to_wrapper_decrypt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            base_downloader = self._build_base_downloader(tempdir)
            downloader = AppleMusicSongDownloader(
                base_downloader=base_downloader,
                interface=SimpleNamespace(),
                codec_priority=[SongCodec.AAC_LEGACY],
            )
            downloader.use_wrapper = True
            downloader.decrypt_amdecrypt = AsyncMock()

            asyncio.run(
                downloader.stage(
                    "/tmp/encrypted.m4a",
                    "/tmp/staged.m4a",
                    None,
                    False,
                    "song-123",
                    "skd://track",
                    use_single_content_key=True,
                )
            )

            downloader.decrypt_amdecrypt.assert_awaited_once_with(
                "/tmp/encrypted.m4a",
                "/tmp/staged.m4a",
                "song-123",
                "skd://track",
                use_single_content_key=True,
            )


if __name__ == "__main__":
    unittest.main()
