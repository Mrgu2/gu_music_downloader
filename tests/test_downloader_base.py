import unittest
import inspect
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamdl.network import NetworkConfig
from gamdl.interface.types import MediaTags, PlaylistTags
from gamdl.downloader.downloader_base import AppleMusicBaseDownloader


class AppleMusicBaseDownloaderTests(unittest.TestCase):
    def _base_downloader_stub(self, output_path: str = "/tmp/out") -> AppleMusicBaseDownloader:
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.output_path = output_path
        downloader.album_folder_template = "{album_artist}/{album}"
        downloader.compilation_folder_template = "Compilations/{album}"
        downloader.no_album_folder_template = "{artist}/Unknown Album"
        downloader.single_disc_file_template = "{track:02d} {title}"
        downloader.multi_disc_file_template = "{disc}-{track:02d} {title}"
        downloader.no_album_file_template = "{title}"
        downloader.playlist_file_template = "Playlists/{playlist_artist}/{playlist_title}"
        downloader.truncate = None
        return downloader

    def test_flat_paths_cover_album_compilation_and_artist_top_songs(self):
        downloader = self._base_downloader_stub()
        downloader.preserve_folder_structure = False
        for context in (None, "artist-top-songs"):
            for compilation in (False, True):
                tags = MediaTags(artist="Singer", title="Song", title_id=123,
                                 album="Album", track=1, compilation=compilation, disc_total=2)
                self.assertEqual(
                    downloader.get_final_path(tags, ".m4a", None,
                                              source_context=context,
                                              artist_folder_name="Singer"),
                    "/tmp/out/[01]Song-Album-Singer.m4a",
                )

    def test_playlist_filename_uses_playlist_position_without_changing_album_tag(self):
        downloader = self._base_downloader_stub()
        tags = MediaTags(artist="Singer", album_artist="Singer", title="Song",
                         album="Album", track=3, disc_total=1)
        playlist = PlaylistTags(playlist_track=12)
        for preserve, expected in (
            (False, "/tmp/out/[12]Song-Album-Singer.m4a"),
            (True, "/tmp/out/Singer/Album/12 Song.m4a"),
        ):
            downloader.preserve_folder_structure = preserve
            self.assertEqual(downloader.get_final_path(tags, ".m4a", playlist), expected)
            self.assertEqual(tags.track, 3)
        downloader.preserve_folder_structure = False
        playlist.playlist_track = 105
        self.assertIn("[105]", downloader.get_final_path(tags, ".m4a", playlist))

    def test_flat_paths_distinguish_albums_and_sanitize_slashes(self):
        downloader = self._base_downloader_stub()
        downloader.preserve_folder_structure = False
        paths = [Path(downloader.get_final_path(
            MediaTags(artist="A/B", title="Same/Title", album=album),
            ".m4a", None)) for album in ("Album One", "Album Two")]
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(path.parent == Path("/tmp/out") for path in paths))

    def test_output_path_default_is_machine_neutral(self):
        self.assertEqual(
            AppleMusicBaseDownloader.__init__.__defaults__[0],
            "./Apple Music",
        )

    def test_save_cover_default_is_enabled(self):
        self.assertTrue(AppleMusicBaseDownloader.__init__.__defaults__[4])

    def test_cover_size_default_uses_max_available(self):
        self.assertIsNone(
            inspect.signature(AppleMusicBaseDownloader.__init__)
            .parameters["cover_size"]
            .default
        )

    def test_sanitize_string_replaces_reserved_path_segments(self):
        downloader = self._base_downloader_stub()

        self.assertEqual(downloader.sanitize_string(".."), "_")
        self.assertEqual(downloader.sanitize_string("."), "_")
        self.assertEqual(downloader.sanitize_string("   "), "_")

    def test_get_final_path_cannot_escape_output_path_with_reserved_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "downloads"
            downloader = self._base_downloader_stub(str(output_path))
            final_path = Path(
                downloader.get_final_path(
                    MediaTags(
                        album_artist="..",
                        album=".",
                        title="Safe Song",
                        track=1,
                    ),
                    ".m4a",
                    None,
                )
            )

            self.assertTrue(
                final_path.resolve().is_relative_to(output_path.resolve())
            )
            self.assertNotIn("..", final_path.parts)

    def test_get_playlist_file_path_cannot_escape_output_path_with_reserved_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "downloads"
            downloader = self._base_downloader_stub(str(output_path))
            playlist_path = Path(
                downloader.get_playlist_file_path(
                    PlaylistTags(
                        playlist_artist="..",
                        playlist_title=".",
                        playlist_id="pl.test",
                        playlist_track=1,
                    )
                )
            )

            self.assertTrue(
                playlist_path.resolve().is_relative_to(output_path.resolve())
            )
            self.assertNotIn("..", playlist_path.parts)

    def test_wrapper_m3u8_ip_uses_matching_port_offset(self):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.wrapper_decrypt_ip = "127.0.0.1:10020"
        self.assertEqual(downloader.get_wrapper_m3u8_ip(), "127.0.0.1:20020")

    def test_wrapper_m3u8_ip_tracks_custom_decrypt_port(self):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.wrapper_decrypt_ip = "127.0.0.1:10022"
        self.assertEqual(downloader.get_wrapper_m3u8_ip(), "127.0.0.1:20022")

    def test_wrapper_m3u8_ip_defaults_host_for_port_only_input(self):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.wrapper_decrypt_ip = "10022"
        self.assertEqual(downloader.get_wrapper_m3u8_ip(), "127.0.0.1:20022")

    @patch("gamdl.downloader.downloader_base.HlsFD")
    @patch("gamdl.downloader.downloader_base.YoutubeDL")
    def test_download_ytdlp_passes_empty_proxy_in_direct_mode(self, ytdlp_cls, hls_cls):
        hls_cls.return_value.download.return_value = (True, None)
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.network_config = NetworkConfig(mode="direct", proxy_url="")
        downloader.silent = False

        downloader._download_ytdlp("https://example.com/stream.m3u8", "/tmp/out.m4a")

        options = ytdlp_cls.call_args.args[0]
        self.assertEqual(options["proxy"], "")

    @patch("gamdl.downloader.downloader_base.HlsFD")
    @patch("gamdl.downloader.downloader_base.YoutubeDL")
    def test_download_ytdlp_passes_custom_proxy(self, ytdlp_cls, hls_cls):
        hls_cls.return_value.download.return_value = (True, None)
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.network_config = NetworkConfig(mode="custom", proxy_url="http://127.0.0.1:7890")
        downloader.silent = True

        downloader._download_ytdlp("https://example.com/stream.m3u8", "/tmp/out.m4a")

        options = ytdlp_cls.call_args.args[0]
        self.assertEqual(options["proxy"], "http://127.0.0.1:7890")

    @patch("gamdl.downloader.downloader_base.HlsFD")
    @patch("gamdl.downloader.downloader_base.HttpFD")
    @patch("gamdl.downloader.downloader_base.YoutubeDL")
    def test_download_ytdlp_uses_hls_downloader_for_m3u8(self, ytdlp_cls, http_cls, hls_cls):
        hls_cls.return_value.download.return_value = (True, None)
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.network_config = NetworkConfig(mode="direct", proxy_url="")
        downloader.silent = False

        downloader._download_ytdlp("https://example.com/master.m3u8?token=1", "/tmp/out.m4a")

        hls_cls.return_value.download.assert_called_once_with(
            "/tmp/out.m4a",
            {
                "url": "https://example.com/master.m3u8?token=1",
                "ext": "mp4",
                "protocol": "m3u8",
            },
        )
        http_cls.return_value.download.assert_not_called()

    @patch("gamdl.downloader.downloader_base.HlsFD")
    @patch("gamdl.downloader.downloader_base.HttpFD")
    @patch("gamdl.downloader.downloader_base.YoutubeDL")
    def test_download_ytdlp_uses_http_downloader_for_non_m3u8(self, ytdlp_cls, http_cls, hls_cls):
        http_cls.return_value.download.return_value = (True, None)
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.network_config = NetworkConfig(mode="direct", proxy_url="")
        downloader.silent = False

        downloader._download_ytdlp("https://example.com/audio.m4a", "/tmp/out.m4a")

        http_cls.return_value.download.assert_called_once_with(
            "/tmp/out.m4a",
            {
                "url": "https://example.com/audio.m4a",
            },
        )
        hls_cls.return_value.download.assert_not_called()

    @patch("gamdl.downloader.downloader_base.HlsFD")
    @patch("gamdl.downloader.downloader_base.YoutubeDL")
    def test_download_ytdlp_raises_when_hls_downloader_fails(self, ytdlp_cls, hls_cls):
        hls_cls.return_value.download.return_value = (False, None)
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.network_config = NetworkConfig(mode="direct", proxy_url="")
        downloader.silent = False

        with self.assertRaisesRegex(RuntimeError, "yt-dlp HLS download failed"):
            downloader._download_ytdlp("https://example.com/master.m3u8", "/tmp/out.m4a")

    @patch("gamdl.downloader.downloader_base.async_subprocess", new_callable=AsyncMock)
    def test_download_nm3u8dlre_passes_network_config(self, async_subprocess_mock):
        downloader = AppleMusicBaseDownloader.__new__(AppleMusicBaseDownloader)
        downloader.full_nm3u8dlre_path = "N_m3u8DL-RE"
        downloader.full_ffmpeg_path = "ffmpeg"
        downloader.silent = True
        downloader.network_config = NetworkConfig(mode="custom", proxy_url="http://127.0.0.1:7890")

        import asyncio

        asyncio.run(
            downloader.download_nm3u8dlre(
                "https://example.com/stream.m3u8",
                "/tmp/test/out.m4a",
            )
        )

        self.assertEqual(
            async_subprocess_mock.call_args.kwargs["network_config"],
            downloader.network_config,
        )
