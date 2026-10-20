import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gamdl.interface import AppleMusicSongInterface, SongCodec
from gamdl.interface.enums import MediaFileFormat


class SongAssetMetadataTests(unittest.TestCase):
    AUDIO_GROUP = "audio-alac-stereo-48000-24"
    FAIRPLAY = "com.apple.streamingkeydelivery"
    WIDEVINE = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
    PLAYREADY = "com.microsoft.playready"

    def _asset(self, group=None, key_ids=None):
        return {
            "GROUP-ID": group or self.AUDIO_GROUP,
            "AUDIO-SESSION-KEY-IDS": ["2"] if key_ids is None else key_ids,
        }

    def _stream(self, assets, variant="dktu2l6sm", audio_group=AUDIO_GROUP):
        # Parse real HLS syntax so the reproduction includes m3u8's field mapping.
        key_metadata = {
            "2": {
                self.FAIRPLAY: {"URI": "skd://correct-audio-group"},
                self.WIDEVINE: {"URI": "data:text/plain;base64,correct-widevine"},
                self.PLAYREADY: {"URI": "data:text/plain;base64,correct-playready"},
            },
            "3": {self.FAIRPLAY: {"URI": "skd://different-audio-group"}},
        }
        lines = ["#EXTM3U"]
        for data_id, value in (
            ("com.apple.hls.AudioSessionKeyInfo", key_metadata),
            ("com.apple.hls.audioAssetMetadata", assets),
        ):
            if value is not None:
                encoded = base64.b64encode(json.dumps(value).encode()).decode()
                lines.append(f'#EXT-X-SESSION-DATA:DATA-ID="{data_id}",VALUE="{encoded}"')
        attributes = [
            "BANDWIDTH=2000000",
            "AVERAGE-BANDWIDTH=1800000",
            'CODECS="alac"',
        ]
        if audio_group is not None:
            attributes.append(f'AUDIO="{audio_group}"')
        if variant is not None:
            attributes.append(f'STABLE-VARIANT-ID="{variant}"')
        lines.extend(["#EXT-X-STREAM-INF:" + ",".join(attributes), "audio.m3u8"])

        interface = AppleMusicSongInterface.__new__(AppleMusicSongInterface)
        interface._get_response = AsyncMock(
            return_value=SimpleNamespace(text="\n".join(lines) + "\n")
        )
        codec = SongCodec.ALAC
        if audio_group is None:
            # ASK allows testing a selected playlist with no AUDIO attribute.
            codec = SongCodec.ASK

            async def select_playlist(data):
                return data["playlists"][0]

            interface._get_playlist_from_user = select_playlist
        result = asyncio.run(
            interface._get_stream_info(
                {}, codec, "https://example.com/album/master.m3u8"
            )
        )
        interface._get_response.assert_awaited_once_with(
            "https://example.com/album/master.m3u8"
        )
        return result

    def _assert_correct_stream(self, result):
        self.assertIsNotNone(result)
        self.assertEqual(result.audio_track.codec, "alac")
        self.assertEqual(result.file_format, MediaFileFormat.M4A)
        self.assertEqual(
            result.audio_track.stream_url, "https://example.com/album/audio.m3u8"
        )
        self.assertEqual(result.audio_track.fairplay_key, "skd://correct-audio-group")
        self.assertEqual(
            result.audio_track.widevine_pssh,
            "data:text/plain;base64,correct-widevine",
        )
        self.assertEqual(
            result.audio_track.playready_pssh,
            "data:text/plain;base64,correct-playready",
        )

    def test_exact_variant_match_takes_priority_over_audio_group(self):
        self._assert_correct_stream(
            self._stream(
                {
                    "other": self._asset(key_ids=["3"]),
                    "dktu2l6sm": self._asset(),
                }
            )
        )

    def test_mismatched_variant_uses_matching_audio_group(self):
        for variant in ("dktu2l6sm", "bxfzm078c"):
            with self.subTest(variant=variant):
                self._assert_correct_stream(
                    self._stream(
                        {
                            "unrelated": self._asset("audio-stereo-256", ["3"]),
                            "different-asset-id": self._asset(),
                        },
                        variant=variant,
                    )
                )

    def test_missing_variant_id_uses_matching_audio_group(self):
        self._assert_correct_stream(
            self._stream({"different-asset-id": self._asset()}, variant=None)
        )

    def test_missing_or_unmatched_assets_returns_no_stream(self):
        for assets in (None, {}, {"other": self._asset("audio-stereo-256", ["3"])}):
            with self.subTest(assets=assets):
                with self.assertLogs("gamdl.interface.interface_song", "WARNING") as logs:
                    self.assertIsNone(self._stream(assets))
                self.assertIn("dktu2l6sm", "\n".join(logs.output))
                self.assertIn(self.AUDIO_GROUP, "\n".join(logs.output))

    def test_missing_audio_group_does_not_match_missing_group_metadata(self):
        with self.assertLogs("gamdl.interface.interface_song", "WARNING"):
            self.assertIsNone(
                self._stream(
                    {"other": {"AUDIO-SESSION-KEY-IDS": ["2"]}},
                    audio_group=None,
                )
            )

    def test_ambiguous_audio_group_does_not_pick_an_arbitrary_key(self):
        with self.assertLogs("gamdl.interface.interface_song", "WARNING"):
            self.assertIsNone(
                self._stream(
                    {"first": self._asset(key_ids=["3"]), "second": self._asset()}
                )
            )

    def test_missing_or_empty_key_ids_returns_no_stream(self):
        for asset in ({"GROUP-ID": self.AUDIO_GROUP}, self._asset(key_ids=[])):
            with self.subTest(asset=asset):
                with self.assertLogs("gamdl.interface.interface_song", "WARNING"):
                    self.assertIsNone(self._stream({"dktu2l6sm": asset}))


if __name__ == "__main__":
    unittest.main()
