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

        # In wrapper mode, use wrapper-lite /lyrics endpoint directly.
        # It can return word-timed syllable lyrics with translations/romanization
        # that the AMP relationships API may not expose.
        if self.base.use_wrapper:
            wrapper_lyrics = await self._get_lyrics_from_wrapper(
                self.base.parse_catalog_media_id(song_metadata)
            )
            if wrapper_lyrics is not None:
                log.debug("success_via_wrapper")
                return wrapper_lyrics
            log.debug("wrapper_lyrics_unavailable_falling_back")

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

    async def _get_lyrics_from_wrapper(self, adam_id: str) -> "Lyrics | None":
        """Fetch TTML lyrics from wrapper-lite /lyrics endpoint.

        wrapper-lite returns the raw TTML string in data.lyrics.  Supports
        word-timed syllable lyrics (syllable=1, default) and line-timed lyrics
        (syllable=0).  Falls back to None on any error so the caller can try
        the AMP relationships path.
        """
        # Request word-timed syllable lyrics (syllable=1) for TTML/SRT formats.
        # For LRC we still request syllable=1; wrapper-lite bundles translations
        # and romanization in both modes, and our _get_lyrics() TTML parser handles
        # both.  syllable=0 only matters if the caller specifically wants
        # wrapper-converted line-timed output without the extra metadata.
        syllable_param = "1"
        url = (
            f"{self.base.wrapper_url.rstrip('/')}/lyrics"
            f"?adamId={urllib.parse.quote(adam_id)}"
            f"&syllable={syllable_param}"
        )
        try:
            response = await self.base.get_response(url, valid_responses=[200, 404])
            if response.status_code == 404:
                return None
            data = response.json()
        except Exception:
            return None

        if data.get("code") != 0:
            return None

        ttml = data.get("data", {}).get("lyrics")
        if not ttml:
            return None

        try:
            return self._get_lyrics(ttml)
        except Exception:
            return None

    @staticmethod
    def _get_p_text(p: ElementTree.Element) -> str | None:
        """Extract display text from a <p> element.

        Line-timed TTML: text sits directly in p.text.
        Syllable-timed TTML (wrapper-lite syllable=1): text is split across
        <span> children with no separator between them — must join with a
        space and then strip spurious spaces before punctuation.
        """
        NS = "{http://www.w3.org/ns/ttml}"
        spans = [s for s in p if s.tag == f"{NS}span"]
        if spans:
            parts = [s.text or "" for s in spans if (s.text or "").strip()]
            if parts:
                # Join with space; remove space inserted before punctuation
                joined = " ".join(parts)
                joined = re.sub(r" ([,\.!?;:\)\]…\'\"」』])", r"\1", joined)
                joined = re.sub(r"([\(\[「『\'\"]) ", r"\1", joined)
                return joined.strip() or None
        # Line-timed mode: text sits directly in p.text
        text = (p.text or "").strip()
        return text if text else None

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
                text = self._get_p_text(p)
                if text is not None:
                    stanza.append(text)

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
        text = self._get_p_text(element) or ""

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
        text = self._get_p_text(element) or ""

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
    ) -> MediaTags:
        log = logger.bind(action="get_song_tags")

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
            genre_id=int(webplayback_metadata["genreId"]) or None,
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

        log.debug("success", tags=tags)

        return tags

    async def get_tags_from_amp(
        self,
        song_metadata: dict,
        lyrics: str | None = None,
    ) -> "MediaTags":
        """
        Build MediaTags from AMP catalog + iTunes Lookup API.

        Sources (all public — no music_user_token needed):
          - AMP song attributes  : name, sortName, sortArtistName, composerName,
                                   trackNumber, discNumber, releaseDate, contentRating,
                                   isCompilation, genreNames, durationInMillis
          - AMP album relationship: albumName, sortName, artistName, copyright,
                                    trackCount, discCount
          - iTunes Lookup (entity=album): artistId, genreId, primaryGenreName,
                                          trackExplicitness, discCount, trackCount,
                                          collectionId, gapless, comments
        Together these exactly match the fields available from Apple webPlayback.
        """
        log = logger.bind(action="get_song_tags_from_amp")

        attr = song_metadata["attributes"]
        song_id = song_metadata["id"]

        # ── AMP album relationship ─────────────────────────────────────────────
        album_data = None
        try:
            album_data = song_metadata["relationships"]["albums"]["data"][0]
        except (KeyError, IndexError):
            pass
        album_attr = album_data["attributes"] if album_data else {}
        album_id_str = album_data["id"] if album_data else None

        # ── iTunes Lookup (concurrent with anything above) ────────────────────
        # entity=album returns [song_record, album_record] giving us
        # artistId, genreId, compilation, gapless, comments, discCount, etc.
        lookup_results = []
        try:
            lookup_data = await self.base.itunes_api.get_lookup_result(
                song_id, entity="album"
            )
            lookup_results = lookup_data.get("results", [])
        except Exception:
            pass  # degrade gracefully — AMP data is still usable

        lk_song  = lookup_results[0] if len(lookup_results) > 0 else {}
        lk_album = lookup_results[1] if len(lookup_results) > 1 else {}

        def _int(v, default: int = 0) -> int:
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        # ── Integer IDs ───────────────────────────────────────────────────────
        title_id  = _int(song_id)
        album_id  = _int(lk_song.get("collectionId") or lk_album.get("collectionId") or album_id_str)
        artist_id = _int(lk_song.get("artistId")) or None

        # genre_id: use None (omit tag) rather than 0 when unavailable.
        # Hardcoding 0 writes an invalid geID atom that confuses players.
        _raw_genre_id = lk_song.get("primaryGenreId") or lk_album.get("primaryGenreId")
        genre_id = _int(_raw_genre_id) if _raw_genre_id else None

        # ── Sort fields (AMP attributes, populated when track is in catalog) ──
        sort_name   = attr.get("sortName")        or attr.get("name", "")
        sort_artist = attr.get("sortArtistName")  or attr.get("artistName", "")
        sort_album  = album_attr.get("sortName")  or album_attr.get("name", "")
        sort_composer = attr.get("sortComposerName")

        # ── Album name / artist name ──────────────────────────────────────────
        # iTunes Lookup censoredName matches what webplayback returns
        album_name   = (lk_song.get("collectionCensoredName")
                        or album_attr.get("name", ""))
        album_artist = (lk_album.get("artistName")
                        or album_attr.get("artistName")
                        or attr.get("artistName", ""))

        # ── Disc / track counts ───────────────────────────────────────────────
        disc       = _int(lk_song.get("discNumber")  or attr.get("discNumber"), 1)
        disc_total = _int(lk_song.get("discCount")   or album_attr.get("discCount"), 1)
        track      = _int(lk_song.get("trackNumber") or attr.get("trackNumber"), 1)
        track_total = _int(lk_song.get("trackCount") or album_attr.get("trackCount"), 1)

        # ── Explicit rating ───────────────────────────────────────────────────
        # iTunes Lookup: trackExplicitness = "explicit" | "cleaned" | "notExplicit"
        explicitness = lk_song.get("trackExplicitness", "")
        if explicitness == "explicit":
            rating = MediaRating.EXPLICIT
        elif explicitness == "cleaned":
            rating = MediaRating.CLEAN
        else:
            # Fallback to AMP contentRating
            cr = attr.get("contentRating", "")
            if cr == "explicit":
                rating = MediaRating.EXPLICIT
            elif cr == "clean":
                rating = MediaRating.CLEAN
            else:
                rating = MediaRating.NONE

        # ── Genre ─────────────────────────────────────────────────────────────
        genre = (lk_song.get("primaryGenreName")
                 or (attr.get("genreNames") or [""])[0]
                 or None)

        # ── Gapless, compilation, comments ───────────────────────────────────
        # These live in iTunes Lookup but not in AMP catalog attributes
        gapless     = bool(lk_song.get("trackTimeMillis") and lk_song.get("trackTimeMillis") != 0
                           and lk_album.get("collectionType") == "Compilation")                       if not lk_song.get("gapless") else bool(lk_song.get("gapless"))
        compilation = bool(lk_song.get("collectionArtistId")
                           or album_attr.get("isCompilation")
                           or lk_album.get("collectionType") == "Compilation")
        comment     = lk_song.get("shortDescription") or lk_song.get("longDescription")

        # ── Copyright ─────────────────────────────────────────────────────────
        copyright_str = (album_attr.get("copyright")
                         or lk_album.get("copyright"))

        # ── Date ─────────────────────────────────────────────────────────────
        if self.use_album_date and album_id:
            date = await self.base.get_media_date(str(album_id))
        else:
            release_date_str = (lk_song.get("releaseDate")
                                or attr.get("releaseDate"))
            date = self.base.parse_date(release_date_str) if release_date_str else None

        # ── Composer ─────────────────────────────────────────────────────────
        composer    = attr.get("composerName") or lk_song.get("composerName")
        composer_id = None  # not in AMP or iTunes Lookup for songs

        # ── xid / isrc ────────────────────────────────────────────────────────
        # xid is only returned by the webplayback API (vendor-specific ID
        # embedded in the FairPlay license flow) and is never present in AMP
        # catalog attributes — omit it entirely to avoid writing an empty tag.
        # ISRC is available in AMP attributes (attr["isrc"]) and is a stable,
        # standard identifier; include it when present.
        isrc = attr.get("isrc") or None

        tags = MediaTags(
            album=album_name,
            album_artist=album_artist,
            album_id=album_id,
            album_sort=sort_album,
            artist=lk_song.get("artistName") or attr.get("artistName", ""),
            artist_id=artist_id,
            artist_sort=sort_artist,
            comment=comment,
            compilation=compilation,
            composer=composer,
            composer_id=composer_id,
            composer_sort=sort_composer,
            copyright=copyright_str,
            date=date,
            disc=disc,
            disc_total=disc_total,
            gapless=gapless,
            genre=genre,
            genre_id=genre_id,
            isrc=isrc,
            lyrics=lyrics if lyrics else None,
            media_type=MediaType.SONG,
            rating=rating,
            storefront=self.base.itunes_api.storefront_id,  # numeric int
            title=lk_song.get("trackCensoredName") or attr.get("name", ""),
            title_id=title_id,
            title_sort=sort_name,
            track=track,
            track_total=track_total,
        )

        log.debug("success", tags=tags)
        return tags


    async def get_stream_info(
        self,
        song_metadata: dict | None = None,
        webplayback: dict | None = None,
    ) -> StreamInfoAv | None:
        for codec in self.codec_priority:
            # In wrapper mode webplayback is None; legacy codecs need the
            # webplayback struct, so fall back to the HLS path instead.
            if codec.is_legacy() and webplayback is not None:
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

        # Try to get DRM keys from SESSION-DATA (fast path: no extra HTTP request).
        # Falls back to fetching the playlist M3U8 when:
        #   - AudioSessionKeyInfo SESSION-DATA is absent (some wrapper-lite M3U8s), or
        #   - stable_variant_id is not present in asset_metadata (key mismatch).
        used_session_key_path = False
        if session_key_metadata:
            asset_metadata = self._get_asset_metadata(m3u8_master_data)
            variant_id = playlist["stream_info"].get("stable_variant_id")
            if asset_metadata and variant_id and variant_id in asset_metadata:
                drm_ids = asset_metadata[variant_id]["AUDIO-SESSION-KEY-IDS"]
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
                used_session_key_path = True

        if not used_session_key_path:
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

        if self.base.use_wrapper:
            # Wrapper mode: build tags from AMP catalog (no webplayback needed).
            # get_webplayback via wrapper-lite only returns an m3u8 URL, not the
            # full Apple struct required by get_tags — so we skip it entirely.
            media.tags = await self.get_tags_from_amp(
                media.media_metadata,
                media.lyrics.unsynced if media.lyrics else None,
            )
            webplayback = None
        else:
            webplayback = await self.base.apple_music_api.get_webplayback(media.media_id)
            media.tags = await self.get_tags(
                webplayback,
                media.lyrics.unsynced if media.lyrics else None,
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

            if (
                not self.base.use_wrapper
                and not media.stream_info.audio_track.widevine_pssh
            ) or (
                self.base.use_wrapper and not media.stream_info.audio_track.fairplay_key
            ):
                raise GamdlInterfaceDecryptionNotAvailableError(media_id=media.media_id)

            if (
                media.stream_info.audio_track.widevine_pssh
                and not self.base.use_wrapper
            ) or media.stream_info.audio_track.legacy:
                media.decryption_key = DecryptionKeyAv(
                    audio_track=await self.base.get_decryption_key(
                        media.stream_info.audio_track.widevine_pssh,
                        media.media_id,
                    )
                )

        media.partial = False

        yield media
