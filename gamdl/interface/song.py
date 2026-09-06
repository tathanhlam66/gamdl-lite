import asyncio
import base64
import datetime
import json
import re
import urllib.parse
from typing import AsyncGenerator, Callable
from xml.dom import minidom
from xml.etree import ElementTree

import m3u8
import structlog

from .base import AppleMusicBaseInterface
from .constants import DRM_DEFAULT_KEY_MAPPING, MP4_FORMAT_CODECS, SONG_CODEC_REGEX_MAP
from .enums import MediaRating, MediaType, SongCodec, SyncedLyricsFormat
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    AppleMusicMedia,
    DecryptionKeyAv,
    Lyrics,
    MediaFileFormat,
    MediaTags,
    StreamInfo,
    StreamInfoAv,
)

logger = structlog.get_logger(__name__)


class AppleMusicSongInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        codec_priority: list[SongCodec] = [SongCodec.AAC_LEGACY],
        use_album_date: bool = False,
        skip_stream_info: bool = False,
        ask_codec_function: Callable[[list[dict]], dict | None] | None = None,
    ):
        self.base = base
        self.synced_lyrics_format = synced_lyrics_format
        self.codec_priority = codec_priority
        self.use_album_date = use_album_date
        self.skip_stream_info = skip_stream_info
        self.ask_codec_function = ask_codec_function

    async def _get_lyrics_from_catalog(
        self, song_metadata: dict
    ) -> "Lyrics | None":
        """Fetch lyrics via the Apple Music catalog API (requires media-user-token).

        This is the same logic used by the cookies-only path, extracted so that
        the wrapper + cookies hybrid mode can also benefit from it.
        Returns a ``Lyrics`` object on success, ``None`` otherwise.
        """
        if (
            "relationships" not in song_metadata
            or "lyrics" not in song_metadata["relationships"]
        ):
            song_metadata = (
                await self.base.apple_music_api.get_song(
                    self.base.parse_catalog_media_id(song_metadata)
                )
            )["data"][0]

        if (
            "lyrics" in song_metadata.get("relationships", {})
            and "data" in song_metadata["relationships"]["lyrics"]
            and len(song_metadata["relationships"]["lyrics"]["data"]) > 0
            and "attributes"
            in song_metadata["relationships"]["lyrics"]["data"][0]
            and song_metadata["relationships"]["lyrics"]["data"][0][
                "attributes"
            ].get("ttml")
            is not None
        ):
            return self._get_lyrics(
                song_metadata["relationships"]["lyrics"]["data"][0]["attributes"][
                    "ttml"
                ]
            )
        return None

    async def _get_lyrics_ttml_from_wrapper(self, song_id: str) -> str | None:
        """Fetch TTML lyrics string from wrapper-lite /lyrics endpoint.

        wrapper-lite exposes GET /lyrics?adamId=<id>[&syllable=1]
        and returns {"code":0,"data":{"lyrics":"<ttml>"}}.
        Falls back to non-syllable lyrics if syllable fetch fails.

        Reuses the existing ``_wrapper_client`` from the API object so that
        connection pooling is shared with all other wrapper calls.
        """
        client = self.base.apple_music_api._wrapper_client
        wrapper_base = self.base.wrapper_url.rstrip("/")
        for syllable in (1, 0):
            url = f"{wrapper_base}/lyrics?adamId={song_id}&syllable={syllable}"
            try:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
            except Exception:
                continue

            if data.get("code") != 0:
                continue

            ttml = data.get("data", {}).get("lyrics") or ""
            if ttml.strip():
                return ttml

        return None

    async def get_lyrics(
        self,
        song_metadata: dict,
    ) -> Lyrics | None:
        log = logger.bind(
            action="get_lyrics",
            song_id=self.base.parse_catalog_media_id(song_metadata),
        )

        if not song_metadata["attributes"]["hasLyrics"]:
            log.debug("no_lyrics")
            return None

        has_cookie = bool(self.base.apple_music_api.media_user_token)

        # --- wrapper-lite path ---
        if self.base.use_wrapper:
            if has_cookie:
                # Cookies available → try catalog API first (richer TTML / syllable data)
                catalog_lyrics = await self._get_lyrics_from_catalog(song_metadata)
                if catalog_lyrics:
                    log.debug("success_wrapper_catalog")
                    return catalog_lyrics
                log.debug("catalog_lyrics_empty_falling_back_to_wrapper_endpoint")

            # No cookies (or catalog returned nothing) → wrapper /lyrics endpoint
            song_id = self.base.parse_catalog_media_id(song_metadata)
            ttml = await self._get_lyrics_ttml_from_wrapper(song_id)
            if ttml:
                lyrics = self._get_lyrics(ttml)
                log.debug("success_wrapper", lyrics=lyrics)
                return lyrics
            log.debug("no_lyrics_data_wrapper")
            return None

        # --- cookies-only path (unchanged) ---
        if (
            "relationships" not in song_metadata
            or "lyrics" not in song_metadata["relationships"]
        ):
            song_metadata = (
                await self.base.apple_music_api.get_song(
                    self.base.parse_catalog_media_id(song_metadata)
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
            )

            log.debug("success", lyrics=lyrics)

            return lyrics
        else:
            log.debug("no_lyrics_data")

    def _get_lyrics(
        self,
        lyrics_ttml: str,
    ) -> Lyrics:
        lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        unsynced_lyrics = []
        synced_lyrics = []
        index = 1

        for div in lyrics_ttml_et.iter("{http://www.w3.org/ns/ttml}div"):
            stanza = []
            unsynced_lyrics.append(stanza)

            for p in div.iter("{http://www.w3.org/ns/ttml}p"):
                # Collect text from both line-timed (<p>text</p>) and
                # word-timed (<p><span>word</span>…</p>) TTML structures.
                # p.text alone misses span children produced by wrapper-lite's
                # syllable endpoint — itertext() covers both cases.
                line_text = " ".join("".join(p.itertext()).split())
                if line_text:
                    stanza.append(line_text)

                if p.attrib.get("begin"):
                    if self.synced_lyrics_format == SyncedLyricsFormat.LRC:
                        synced_lyrics.append(self._get_lyrics_line_lrc(p))

                    if self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(self._get_lyrics_line_srt(index, p))

                    if self.synced_lyrics_format == SyncedLyricsFormat.TTML:
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

    def _get_lyrics_line_srt(self, index: int, element: ElementTree.Element) -> str:
        timestamp_begin_ttml = element.attrib.get("begin")
        timestamp_end_ttml = element.attrib.get("end")
        text = " ".join("".join(element.itertext()).split())

        timestamp_begin = self._parse_ttml_timestamp(timestamp_begin_ttml)
        timestamp_end = self._parse_ttml_timestamp(timestamp_end_ttml)

        return (
            f"{index}\n"
            f"{timestamp_begin.strftime('%H:%M:%S,%f')[:-3]} --> "
            f"{timestamp_end.strftime('%H:%M:%S,%f')[:-3]}\n"
            f"{text}\n"
        )

    def _get_lyrics_line_lrc(self, element: ElementTree.Element) -> str:
        timestamp_ttml = element.attrib.get("begin")
        text = " ".join("".join(element.itertext()).split())

        timestamp = self._parse_ttml_timestamp(timestamp_ttml)
        ms_new = timestamp.strftime("%f")[:-3]

        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=timestamp.microsecond
            )

        return f"[{timestamp.strftime('%M:%S.%f')[:-4]}]{text}"

    @staticmethod
    def _build_xid_from_catalog(song_metadata: dict) -> str | None:
        """Return the ISRC from catalog attributes as the xid atom value.

        When cookies are unavailable the full ``Vendor:isrc:ISRC`` string that
        webplayback normally supplies cannot be reconstructed reliably (the
        vendor prefix is not exposed by the catalog API).  Storing the bare
        ISRC is lossless — it preserves the internationally unique track
        identifier without inventing a vendor prefix that could be wrong.
        Returns ``None`` when no ISRC is present so the atom is omitted
        entirely rather than written with a placeholder value.
        """
        return song_metadata.get("attributes", {}).get("isrc") or None

    async def get_tags(
        self,
        webplayback: dict,
        lyrics: str | None = None,
        song_metadata: dict | None = None,
    ) -> MediaTags:
        log = logger.bind(action="get_song_tags")

        webplayback_metadata = webplayback["songList"][0]["assets"][0]["metadata"]

        # --- composer_id (cmID atom) ---
        # Webplayback from wrapper-lite may omit composerId.
        # Fall back to the catalog API song_metadata when present.
        composer_id_raw = webplayback_metadata.get("composerId")
        if not composer_id_raw and song_metadata:
            composer_id_raw = song_metadata.get("attributes", {}).get("composerId")
        composer_id = int(composer_id_raw) if composer_id_raw else None

        # --- xid atom (Vendor:isrc:ISRC) ---
        # Webplayback from wrapper-lite never carries xid.
        # Reconstruct it from the catalog ISRC + label heuristic.
        xid = webplayback_metadata.get("xid")
        if not xid and song_metadata:
            xid = self._build_xid_from_catalog(song_metadata)

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
            composer_id=composer_id,
            composer_sort=webplayback_metadata.get("sort-composer"),
            copyright=webplayback_metadata.get("copyright"),
            date=(
                await self.base.get_media_date(webplayback_metadata["playlistId"])
                if self.use_album_date
                else (
                    self.base.parse_date(webplayback_metadata["releaseDate"])
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
            xid=xid,
        )

        log.debug("success", tags=tags)

        return tags

    async def get_stream_info(
        self,
        song_metadata: dict | None = None,
        webplayback: dict | None = None,
    ) -> StreamInfoAv | None:
        for codec in self.codec_priority:
            if codec.is_legacy():
                return await self._get_stream_info_legacy(webplayback, codec)
            else:
                return await self._get_stream_info(song_metadata, codec)

    async def get_wrapper_m3u8(self, adam_id: str) -> str | None:
        """Fetch the M3U8 URL from wrapper-lite's HTTP /m3u8 endpoint."""
        url = (
            f"{self.base.wrapper_url.rstrip('/')}/m3u8"
            f"?adamId={urllib.parse.quote(adam_id)}"
        )
        try:
            response = await self.base.get_response(url)
            data = response.json()
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        return data.get("data", {}).get("m3u8") or None

    async def _get_stream_info(
        self,
        song_metadata: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info")

        if "extendedAssetUrls" not in song_metadata["attributes"]:
            song_metadata = (
                await self.base.apple_music_api.get_song(
                    self.base.parse_catalog_media_id(song_metadata),
                )
            )["data"][0]

        m3u8_master_url = (
            await self.get_wrapper_m3u8(self.base.parse_catalog_media_id(song_metadata))
            if self.base.use_wrapper
            else song_metadata["attributes"]["extendedAssetUrls"].get("enhancedHls")
        )
        if not m3u8_master_url:
            return None

        m3u8_master_obj = m3u8.loads(
            (await self.base.get_response(m3u8_master_url)).text
        )
        m3u8_master_data = m3u8_master_obj.data

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = StreamInfo(legacy=False)
        stream_info.stream_url = (
            f"{m3u8_master_url.rpartition('/')[0]}/{playlist['uri']}"
        )
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(stream_info.codec.startswith(codec) for codec in MP4_FORMAT_CODECS)

        session_key_metadata = self._get_audio_session_key_metadata(m3u8_master_data)

        # Resolve DRM URIs from master-playlist session data when available.
        # Falls back to fetching the segment-level M3U8 when:
        #   • session_key_metadata is absent (legacy / AAC streams), OR
        #   • asset_metadata does not contain the variant_id reported by the
        #     playlist (can happen with wrapper-lite where the M3U8 is rebuilt
        #     and stable-variant-id values may not match the asset map keys).
        drm_resolved = False
        if session_key_metadata:
            asset_metadata = self._get_asset_metadata(m3u8_master_data)
            variant_id = playlist["stream_info"].get("stable_variant_id", "")
            variant_entry = (asset_metadata or {}).get(variant_id)
            if variant_entry is not None:
                drm_ids = variant_entry["AUDIO-SESSION-KEY-IDS"]
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
                drm_resolved = True

        if not drm_resolved:
            m3u8_obj = m3u8.loads(
                (await self.base.get_response(stream_info.stream_url)).text
            )

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

        log.debug("success", stream_info=stream_info_av)

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
        if self.ask_codec_function:
            playlist = self.ask_codec_function(
                [playlist for playlist in m3u8_data["playlists"]]
            )
            if asyncio.iscoroutine(playlist):
                playlist = await playlist

            return playlist

        return None

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
    ) -> StreamInfoAv:
        log = logger.bind(action="get_legacy_song_stream_info")

        flavor = "32:ctrp64" if codec == SongCodec.AAC_HE_LEGACY else "28:ctrp256"

        stream_info = StreamInfo(legacy=True)
        stream_info.stream_url = next(
            i for i in webplayback["songList"][0]["assets"] if i["flavor"] == flavor
        )["URL"]

        m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = m3u8_obj.keys[0].uri

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        if not media.media_metadata:
            media.media_metadata = (
                await self.base.apple_music_api.get_song(media.media_id)
            )["data"][0]

        media.media_id = self.base.parse_catalog_media_id(media.media_metadata)

        yield media

        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(
                media_id=media.media_id,
            )

        if media.playlist_metadata:
            media.playlist_tags = self.base.get_playlist_tags(
                media.playlist_metadata,
                media.index,
            )

        media.cover = await self.base.get_cover(media.media_metadata)

        media.lyrics = await self.get_lyrics(media.media_metadata)

        webplayback = await self.base.apple_music_api.get_webplayback(media.media_id)

        media.tags = await self.get_tags(
            webplayback,
            media.lyrics.unsynced if media.lyrics else None,
            song_metadata=media.media_metadata,
        )

        if not self.skip_stream_info:
            media.stream_info = await self.get_stream_info(
                media.media_metadata,
                webplayback,
            )
            if not media.stream_info:
                raise GamdlInterfaceFormatNotAvailableError(
                    media_id=media.media_id,
                    codec=self.codec_priority,
                )

            track = media.stream_info.audio_track
            has_fairplay = bool(track.fairplay_key)
            has_widevine = bool(track.widevine_pssh)

            # Determine decryption path:
            #   wrapper + fairplay  → FairPlay via wrapper /license   (ALAC/Atmos)
            #   wrapper + widevine only → direct Apple license (cookies required)
            #                            e.g. aac-legacy stream has no FairPlay key
            #   no wrapper          → direct Apple license via Widevine (or legacy)
            if self.base.use_wrapper and has_fairplay:
                # Wrapper handles FairPlay decryption — skip Widevine entirely
                pass
            elif has_widevine or track.legacy:
                # Direct Apple license exchange (bypass wrapper even when configured)
                media.decryption_key = DecryptionKeyAv(
                    audio_track=await self.base.get_decryption_key(
                        track.widevine_pssh,
                        media.media_id,
                        use_wrapper=False,
                    )
                )
            else:
                raise GamdlInterfaceDecryptionNotAvailableError(media_id=media.media_id)

        media.partial = False

        yield media
