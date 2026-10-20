import asyncio
import base64
import datetime
import io
import json
import logging
import re

from defusedxml import ElementTree, minidom
import m3u8
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from mutagen.mp4 import MP4
from pywidevine import PSSH, Cdm
from pywidevine.license_protocol_pb2 import WidevinePsshData

from ..utils import get_response
from .constants import DRM_DEFAULT_KEY_MAPPING, MP4_FORMAT_CODECS, SONG_CODEC_REGEX_MAP
from .enums import MediaRating, MediaType, SongCodec, SyncedLyricsFormat
from .interface import AppleMusicInterface
from .types import (
    DecryptionKey,
    DecryptionKeyAv,
    Lyrics,
    MediaFileFormat,
    MediaTags,
    StreamInfo,
    StreamInfoAv,
)

logger = logging.getLogger(__name__)


class AppleMusicSongInterface(AppleMusicInterface):
    def __init__(self, interface: AppleMusicInterface):
        self.__dict__.update(interface.__dict__)

    async def get_lyrics(
        self,
        song_metadata: dict,
        synced_lyrics_format: SyncedLyricsFormat,
    ) -> Lyrics | None:
        if not song_metadata["attributes"]["hasLyrics"]:
            return None

        if (
            "relationships" not in song_metadata
            or "lyrics" not in song_metadata["relationships"]
        ):
            song_metadata = (
                await self.apple_music_api.get_song(
                    self.get_media_id_of_library_media(song_metadata)
                )
            )["data"][0]

        if (
            "lyrics" in song_metadata["relationships"]
            and "data" in song_metadata["relationships"]["lyrics"]
            and len(song_metadata["relationships"]["lyrics"]["data"]) > 0
            and "attributes" in song_metadata["relationships"]["lyrics"]["data"][0]
            and song_metadata["relationships"]["lyrics"]["data"][0]["attributes"].get(
                "ttml"
            )
            is not None
        ):
            lyrics = self._get_lyrics(
                song_metadata["relationships"]["lyrics"]["data"][0]["attributes"][
                    "ttml"
                ],
                synced_lyrics_format,
            )
            logging.debug(f"Lyrics: {lyrics}")

            return lyrics

    def _get_lyrics(
        self,
        lyrics_ttml: str,
        synced_lyrics_format: SyncedLyricsFormat,
    ) -> Lyrics:
        lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        unsynced_lyrics = []
        synced_lyrics = []
        index = 1

        for div in lyrics_ttml_et.iter("{http://www.w3.org/ns/ttml}div"):
            stanza = []
            unsynced_lyrics.append(stanza)

            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                if p.text is not None:
                    stanza.append(p.text)

                if p.attrib.get("begin"):
                    if synced_lyrics_format == SyncedLyricsFormat.LRC:
                        synced_lyrics.append(self._get_lyrics_line_lrc(p))

                    if synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(self._get_lyrics_line_srt(index, p))

                    if synced_lyrics_format == SyncedLyricsFormat.TTML:
                        if not synced_lyrics:
                            synced_lyrics.append(
                                minidom.parseString(lyrics_ttml).toprettyxml()
                            )
                        continue

                    index += 1

        return Lyrics(
            synced="\n".join(synced_lyrics + ["\n"]) if synced_lyrics else None,
            unsynced=(
                "\n\n".join(["\n".join(lyric_group) for lyric_group in unsynced_lyrics])
                if unsynced_lyrics
                else None
            ),
        )

    def _parse_ttml_timestamp(
        self,
        timestamp_ttml: str,
    ) -> datetime.datetime:
        mins_secs_ms = re.findall(r"\d+", timestamp_ttml)
        ms, secs, mins = 0, 0, 0

        if len(mins_secs_ms) == 2 and ":" in timestamp_ttml:
            secs, mins = int(mins_secs_ms[-1]), int(mins_secs_ms[-2])

        elif len(mins_secs_ms) == 1:
            ms = int(mins_secs_ms[-1])

        else:
            secs = float(f"{mins_secs_ms[-2]}.{mins_secs_ms[-1]}")
            if len(mins_secs_ms) > 2:
                mins = int(mins_secs_ms[-3])

        return datetime.datetime.fromtimestamp(
            (mins * 60) + secs + (ms / 1000),
            tz=datetime.timezone.utc,
        )

    def _get_lyrics_line_srt(self, index: int, element) -> str:
        timestamp_begin_ttml = element.attrib.get("begin")
        timestamp_end_ttml = element.attrib.get("end")
        text = element.text

        timestamp_begin = self._parse_ttml_timestamp(timestamp_begin_ttml)
        timestamp_end = self._parse_ttml_timestamp(timestamp_end_ttml)

        return (
            f"{index}\n"
            f"{timestamp_begin.strftime('%H:%M:%S,%f')[:-3]} --> "
            f"{timestamp_end.strftime('%H:%M:%S,%f')[:-3]}\n"
            f"{text}\n"
        )

    def _get_lyrics_line_lrc(self, element) -> str:
        timestamp_ttml = element.attrib.get("begin")
        text = element.text

        timestamp = self._parse_ttml_timestamp(timestamp_ttml)
        ms_new = timestamp.strftime("%f")[:-3]

        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=timestamp.microsecond
            )

        return f"[{timestamp.strftime('%M:%S.%f')[:-4]}]{text}"

    async def get_tags(
        self,
        webplayback: dict,
        lyrics: str | None = None,
        use_album_date: bool = False,
    ) -> MediaTags:
        webplayback_metadata = webplayback["songList"][0]["assets"][0]["metadata"]

        tags = MediaTags(
            album=webplayback_metadata["playlistName"],
            album_artist=webplayback_metadata["playlistArtistName"],
            album_id=int(webplayback_metadata["playlistId"]),
            album_sort=webplayback_metadata["sort-album"],
            artist=webplayback_metadata["artistName"],
            artist_id=int(webplayback_metadata["artistId"]),
            artist_sort=webplayback_metadata["sort-artist"],
            comment=webplayback_metadata.get("comments"),
            compilation=webplayback_metadata["compilation"],
            composer=webplayback_metadata.get("composerName"),
            composer_id=(
                int(webplayback_metadata.get("composerId"))
                if webplayback_metadata.get("composerId")
                else None
            ),
            composer_sort=webplayback_metadata.get("sort-composer"),
            copyright=webplayback_metadata.get("copyright"),
            date=(
                await self.get_media_date(webplayback_metadata["playlistId"])
                if use_album_date
                else (
                    self.parse_date(webplayback_metadata["releaseDate"])
                    if webplayback_metadata.get("releaseDate")
                    else None
                )
            ),
            disc=webplayback_metadata["discNumber"],
            disc_total=webplayback_metadata["discCount"],
            gapless=webplayback_metadata["gapless"],
            genre=webplayback_metadata.get("genre"),
            genre_id=int(webplayback_metadata["genreId"]),
            lyrics=lyrics if lyrics else None,
            media_type=MediaType.SONG,
            rating=MediaRating(webplayback_metadata["explicit"]),
            storefront=webplayback_metadata["s"],
            title=webplayback_metadata["itemName"],
            title_id=int(webplayback_metadata["itemId"]),
            title_sort=webplayback_metadata["sort-name"],
            track=webplayback_metadata["trackNumber"],
            track_total=webplayback_metadata["trackCount"],
            xid=webplayback_metadata.get("xid"),
        )
        logger.debug(f"Tags: {tags}")

        return tags

    def _switch_m3u8_master_url_to_default(self, m3u8_master_url: str) -> str:
        return re.sub(
            r"(P\d+)_[^/]+(\.m3u8)",
            r"\1_default\2",
            m3u8_master_url,
        )

    def _get_m3u8_master_url_from_webplayback(
        self,
        webplayback: dict | None,
    ) -> str | None:
        if not webplayback:
            return None

        m3u8_master_url = webplayback.get("songList", [{}])[0].get("hls-playlist-url")
        if not m3u8_master_url:
            return None

        return self._switch_m3u8_master_url_to_default(m3u8_master_url)

    async def _get_m3u8_master_url_from_metadata(
        self,
        song_metadata: dict,
    ) -> str | None:
        attributes = song_metadata.get("attributes", {})
        if attributes.get("playParams", {}).get("isLibrary"):
            return None

        if "extendedAssetUrls" not in attributes:
            song_metadata = (
                await self.apple_music_api.get_song(
                    self.get_media_id_of_library_media(song_metadata),
                )
            )["data"][0]
            attributes = song_metadata.get("attributes", {})

        m3u8_master_url = attributes.get("extendedAssetUrls", {}).get("enhancedHls")
        if not m3u8_master_url:
            return None

        return self._switch_m3u8_master_url_to_default(m3u8_master_url)

    async def get_stream_info(
        self,
        codec: SongCodec,
        song_metadata: dict | None = None,
        webplayback: dict | None = None,
        m3u8_master_url: str | None = None,
        is_library: bool = False,
    ) -> StreamInfoAv | None:
        if is_library:
            return await self._get_library_stream_info(webplayback)

        if codec.is_legacy():
            return await self._get_stream_info_legacy(webplayback, codec)
        else:
            if m3u8_master_url is None:
                m3u8_master_url = (
                    self._get_m3u8_master_url_from_webplayback(webplayback)
                    or await self._get_m3u8_master_url_from_metadata(song_metadata)
                )
            return await self._get_stream_info(
                song_metadata,
                codec,
                m3u8_master_url,
            )

    async def _get_stream_info(
        self,
        song_metadata: dict,
        codec: SongCodec,
        m3u8_master_url: str | None = None,
    ) -> StreamInfoAv | None:
        if m3u8_master_url is None:
            m3u8_master_url = await self._get_m3u8_master_url_from_metadata(
                song_metadata,
            )
        if not m3u8_master_url:
            return None

        m3u8_master_obj = m3u8.loads((await self._get_response(m3u8_master_url)).text)
        m3u8_master_data = m3u8_master_obj.data

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            return None

        stream_info = StreamInfo(legacy=False)
        stream_info.stream_url = (
            f"{m3u8_master_url.rpartition('/')[0]}/{playlist['uri']}"
        )
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(stream_info.codec.startswith(codec) for codec in MP4_FORMAT_CODECS)

        session_key_metadata = self._get_audio_session_key_metadata(m3u8_master_data)

        if session_key_metadata:
            asset_metadata = self._get_asset_metadata(m3u8_master_data)
            variant_id = playlist["stream_info"].get("stable_variant_id")
            audio_group = playlist["stream_info"].get("audio")
            asset = asset_metadata.get(variant_id) if asset_metadata else None
            if asset is None and asset_metadata and audio_group:
                # Apple may index assets separately from STABLE-VARIANT-ID.
                # Only use an unambiguous match for the selected audio group.
                matching_assets = [
                    candidate
                    for candidate in asset_metadata.values()
                    if candidate.get("GROUP-ID") == audio_group
                ]
                if len(matching_assets) == 1:
                    asset = matching_assets[0]
            if asset is None:
                logger.warning(
                    "No unique audio asset metadata for variant %s (audio=%s)",
                    variant_id,
                    audio_group,
                )
                return None
            drm_ids = asset.get("AUDIO-SESSION-KEY-IDS")
            if not drm_ids:
                logger.warning(
                    "No audio session key IDs for variant %s (audio=%s)",
                    variant_id,
                    audio_group,
                )
                return None

            stream_info.widevine_pssh = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
            )
            stream_info.playready_pssh = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "com.microsoft.playready",
            )
            stream_info.fairplay_key = self._get_drm_uri_from_session_key(
                session_key_metadata,
                drm_ids,
                "com.apple.streamingkeydelivery",
            )
        else:
            m3u8_obj = m3u8.loads((await self._get_response(stream_info.stream_url)).text)

            stream_info.widevine_pssh = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
            )
            stream_info.playready_pssh = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "com.microsoft.playready",
            )
            stream_info.fairplay_key = self._get_drm_uri_from_m3u8_keys(
                m3u8_obj,
                "com.apple.streamingkeydelivery",
            )

        stream_info_av = StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )
        logger.debug(f"Stream info: {stream_info_av}")

        return stream_info_av

    def _get_m3u8_metadata(self, m3u8_data: dict, data_id: str) -> dict | None:
        for session_data in m3u8_data.get("session_data", []):
            if session_data["data_id"] == data_id:
                return json.loads(
                    base64.b64decode(session_data["value"]).decode("utf-8")
                )
        return None

    def _get_audio_session_key_metadata(self, m3u8_data: dict) -> dict | None:
        return self._get_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.AudioSessionKeyInfo",
        )

    def _get_asset_metadata(self, m3u8_data: dict) -> dict | None:
        return self._get_m3u8_metadata(
            m3u8_data,
            "com.apple.hls.audioAssetMetadata",
        )

    def _get_playlist_from_codec(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                SONG_CODEC_REGEX_MAP[codec.value], playlist["stream_info"]["audio"]
            )
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    async def _get_playlist_from_user(self, m3u8_data: dict) -> dict | None:
        choices = [
            Choice(
                name=playlist["stream_info"]["audio"],
                value=playlist,
            )
            for playlist in m3u8_data["playlists"]
        ]

        return await inquirer.select(
            message="Select which codec to download:",
            choices=choices,
        ).execute_async()

    def _get_drm_uri_from_session_key(
        self,
        drm_infos: dict,
        drm_ids: list,
        drm_key: str,
    ) -> str | None:
        for drm_id in drm_ids:
            if drm_id != "1" and drm_key in drm_infos.get(drm_id, {}):
                return drm_infos[drm_id][drm_key]["URI"]
        return None

    def _get_drm_uri_from_m3u8_keys(
        self,
        m3u8_obj: m3u8.M3U8,
        drm_key: str,
    ) -> str | None:
        default_uri = DRM_DEFAULT_KEY_MAPPING[drm_key]

        for key in m3u8_obj.keys:
            if key.keyformat == drm_key and key.uri != default_uri:
                return key.uri
        return None

    async def _get_stream_info_legacy(
        self,
        webplayback: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        if not webplayback:
            return None

        flavor = "32:ctrp64" if codec == SongCodec.AAC_HE_LEGACY else "28:ctrp256"

        stream_info = StreamInfo(
            legacy=True,
            use_cenc=True,
            use_single_content_key=True,
        )
        asset = next(
            (
                i
                for i in webplayback["songList"][0].get("assets", [])
                if i["flavor"] == flavor
            ),
            None,
        )
        if not asset:
            return None

        stream_info.stream_url = asset["URL"]

        m3u8_obj = m3u8.loads((await self._get_response(stream_info.stream_url)).text)
        stream_info.widevine_pssh = m3u8_obj.keys[0].uri

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        logger.debug(f"Stream info legacy: {stream_info_av}")

        return stream_info_av

    async def _get_library_stream_info(
        self,
        webplayback: dict | None,
    ) -> StreamInfoAv | None:
        if not webplayback:
            return None

        assets = webplayback.get("songList", [{}])[0].get("assets", [])
        if not assets:
            return None

        stream_info = StreamInfo(
            stream_url=assets[0]["URL"],
            legacy=False,
            drm_free=True,
        )

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        logger.debug(f"Library stream info: {stream_info_av}")

        return stream_info_av

    async def get_decryption_key_legacy(
        self,
        stream_info: StreamInfoAv,
        cdm: Cdm,
    ) -> DecryptionKeyAv:
        stream_info_audio = stream_info.audio_track

        try:
            cdm_session = cdm.open()

            widevine_pssh_data = WidevinePsshData()
            widevine_pssh_data.algorithm = 1
            widevine_pssh_data.key_ids.append(
                base64.b64decode(stream_info_audio.widevine_pssh.split(",")[1])
            )
            pssh_obj = PSSH(widevine_pssh_data.SerializeToString())

            challenge = base64.b64encode(
                await asyncio.to_thread(
                    cdm.get_license_challenge, cdm_session, pssh_obj
                )
            ).decode()
            license_response = await self.apple_music_api.get_license_exchange(
                stream_info.media_id,
                stream_info.audio_track.widevine_pssh,
                challenge,
            )

            await asyncio.to_thread(
                cdm.parse_license, cdm_session, license_response["license"]
            )

            decryption_key = next(
                i for i in cdm.get_keys(cdm_session) if i.type == "CONTENT"
            )
        finally:
            cdm.close(cdm_session)

        decryption_key = DecryptionKeyAv(
            audio_track=DecryptionKey(
                kid=decryption_key.kid.hex,
                key=decryption_key.key.hex(),
            )
        )
        logger.debug(f"Decryption key legacy: {decryption_key}")

        return decryption_key

    async def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
        cdm: Cdm,
    ) -> DecryptionKeyAv:
        return DecryptionKeyAv(
            audio_track=await AppleMusicInterface.get_decryption_key(
                self,
                stream_info.audio_track.widevine_pssh,
                stream_info.media_id,
                cdm,
            )
        )

    async def get_extra_tags(
        self,
        song_metadata: dict,
    ) -> dict:
        previews = song_metadata["attributes"].get("previews", [])
        if not previews:
            return {}

        preview_url = previews[0]["url"]
        preview_response = await self._get_response(preview_url)
        preview_bytes = preview_response.content
        preview_tags = dict(MP4(io.BytesIO(preview_bytes)).tags)

        logger.debug(f"Extra tags: {preview_tags.keys()}")
        return preview_tags
