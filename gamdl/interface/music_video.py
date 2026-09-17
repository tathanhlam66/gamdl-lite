import asyncio
import urllib.parse
from typing import AsyncGenerator, Callable

import m3u8
import structlog

from .base import AppleMusicBaseInterface
from .constants import MP4_FORMAT_CODECS
from .enums import MediaRating, MediaType, MusicVideoCodec, MusicVideoResolution
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    AppleMusicMedia,
    DecryptionKey,
    DecryptionKeyAv,
    MediaFileFormat,
    MediaTags,
    StreamInfo,
    StreamInfoAv,
)

logger = structlog.get_logger(__name__)


class AppleMusicMusicVideoInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        resolution: MusicVideoResolution = MusicVideoResolution.R1080P,
        codec_priority: list[MusicVideoCodec] = [
            MusicVideoCodec.H264,
            MusicVideoCodec.H265,
        ],
        ask_video_codec_function: (
            Callable[[list[m3u8.Playlist]], dict | None] | None
        ) = None,
        ask_audio_codec_function: Callable[[list[dict]], dict | None] | None = None,
    ):
        self.base = base
        self.resolution = resolution
        self.codec_priority = codec_priority
        self.ask_video_codec_function = ask_video_codec_function
        self.ask_audio_codec_function = ask_audio_codec_function

    async def get_itunes_page_metadata(
        self,
        music_video_metadata: dict,
    ) -> dict:
        url_media_id = self.base.parse_media_id_from_url(music_video_metadata)
        itunes_page = await self.base.itunes_api.get_itunes_page(
            "music-video",
            url_media_id,
        )
        return itunes_page["storePlatformData"]["product-dv"]["results"][url_media_id]

    def _get_m3u8_master_url_from_webplayback(self, webplayback: dict) -> str:
        m3u8_master_url = webplayback["hls-playlist-url"]
        return m3u8_master_url

    def _get_m3u8_master_url_from_itunes_page_metadata(
        self,
        itunes_page_metadata: dict,
    ) -> str | None:
        log = logger.bind(action="get_m3u8_master_url_from_itunes_page_metadata")

        stream_url = itunes_page_metadata["offers"][0]["assets"][0].get("hlsUrl")
        if not stream_url:
            return None

        url_parts = urllib.parse.urlparse(stream_url)
        query = urllib.parse.parse_qs(url_parts.query, keep_blank_values=True)
        query.update({"aec": "HD", "dsid": "1"})

        m3u8_master_url = url_parts._replace(
            query=urllib.parse.urlencode(query, doseq=True)
        ).geturl()

        m3u8_master_url = m3u8_master_url.replace(
            "play-edge.itunes.apple.com",
            "play.itunes.apple.com",
        ).replace(
            "MZPlayLocal.woa",
            "MZPlay.woa",
        )

        log.debug("success", m3u8_master_url=m3u8_master_url)

        return m3u8_master_url

    async def get_tags(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
    ) -> MediaTags:
        """Build MediaTags for a music video.

        AMP is the authoritative source for all fields it provides.
        iTunes Lookup (entity=musicVideo) only supplements fields AMP cannot provide:
          - artistId                 — not exposed in AMP attributes
          - comment                  — AMP editorialNotes primary, Lookup shortDescription fallback
        iTunes Page (product-dv) provides:
          - copyright, collectionId, genreId
        """
        log = logger.bind(
            action="get_music_video_tags",
            media_id=self.base.parse_catalog_media_id(metadata),
        )

        attr = metadata.get("attributes", {})
        url_media_id = self.base.parse_media_id_from_url(metadata)

        # --- AMP album relationship (sort-album, isCompilation) ----------------
        amp_album_attr: dict = {}
        try:
            amp_album_attr = (
                metadata["relationships"]["albums"]["data"][0]["attributes"]
            )
        except (KeyError, IndexError, TypeError):
            pass

        # --- iTunes Lookup — use entity=musicVideo so results[1] is the
        #     collection record (album/EP) that contains this MV, not a random
        #     album. Falls back to entity=album if musicVideo returns nothing. ---
        lookup_results = []
        try:
            lookup_raw = await self.base.itunes_api.get_lookup_result(
                url_media_id, entity="musicVideo"
            )
            lookup_results = lookup_raw.get("results", [])
        except Exception:
            pass

        if not lookup_results:
            try:
                lookup_raw = await self.base.itunes_api.get_lookup_result(
                    url_media_id, entity="album"
                )
                lookup_results = lookup_raw.get("results", [])
            except Exception:
                pass

        lk_mv    = lookup_results[0] if len(lookup_results) > 0 else {}
        lk_album = lookup_results[1] if len(lookup_results) > 1 else {}

        # --- Rating: AMP contentRating is primary ----------------------------
        cr = attr.get("contentRating", "")
        if cr == "explicit":
            rating = MediaRating.EXPLICIT
        elif cr == "clean":
            rating = MediaRating.CLEAN
        else:
            rating = MediaRating.NONE

        # --- Genre: AMP genreNames is primary --------------------------------
        genre = (attr.get("genreNames") or [""])[0] or None
        try:
            genre_id = int(itunes_page_metadata["genres"][0]["genreId"])
        except (KeyError, IndexError, TypeError, ValueError):
            genre_id = None

        # --- Sort fields (AMP only) -------------------------------------------
        title_sort  = attr.get("sortName")       or attr.get("name", "")
        artist_sort = attr.get("sortArtistName") or attr.get("artistName", "")
        album_sort  = amp_album_attr.get("sortName") or amp_album_attr.get("name")

        # --- Comment: prefer AMP editorialNotes, fall back to iTunes short desc -
        editorial = attr.get("editorialNotes") or {}
        comment = (
            editorial.get("short")
            or editorial.get("standard")
            or lk_mv.get("shortDescription")
            or lk_mv.get("longDescription")
        ) or None

        # --- Composer (AMP attributes) ----------------------------------------
        composer = attr.get("composerName") or None

        # --- ISRC (AMP standard field) ----------------------------------------
        isrc = attr.get("isrc") or None

        # --- Copyright (iTunes Page primary, AMP album fallback) --------------
        copyright_str = (
            itunes_page_metadata.get("copyright")
            or amp_album_attr.get("copyright")
        )

        # artistId is only available from iTunes Lookup
        artist_id = int(lk_mv["artistId"]) if lk_mv.get("artistId") else None

        tags = MediaTags(
            artist=attr.get("artistName", ""),
            artist_id=artist_id,
            artist_sort=artist_sort,
            comment=comment,
            composer=composer,
            copyright=copyright_str,
            date=self.base.parse_date(attr.get("releaseDate")),
            genre=genre,
            genre_id=genre_id,
            isrc=isrc,
            media_type=MediaType.MUSIC_VIDEO,
            rating=rating,
            storefront=self.base.itunes_api.storefront_id,
            title=attr.get("name", ""),
            title_id=int(metadata["id"]),
            title_sort=title_sort,
        )

        # --- Album / collection tags (present only when MV belongs to one) ----
        collection_id = itunes_page_metadata.get("collectionId")
        if collection_id:
            album_amp = await self.base.get_album_cached(str(collection_id))
            album_amp_attr: dict = album_amp["attributes"] if album_amp else {}

            # AMP cached album is authoritative for all album fields
            tags.album        = album_amp_attr.get("name") or amp_album_attr.get("name")
            tags.album_artist = album_amp_attr.get("artistName") or amp_album_attr.get("artistName")
            tags.album_id     = int(collection_id)
            tags.album_sort   = album_amp_attr.get("sortName") or album_sort
            tags.compilation  = album_amp_attr.get("isCompilation", False)
            tags.disc         = attr.get("discNumber") or 1
            tags.disc_total   = album_amp_attr.get("discCount") or 1
            tags.track        = attr.get("trackNumber") or 1
            tags.track_total  = album_amp_attr.get("trackCount") or 1

        log.debug("success", tags=tags)

        return tags

    async def get_stream_info(
        self,
        metadata: dict,
        itunes_page_metadata: dict,
    ) -> StreamInfoAv | None:
        log = logger.bind(
            action="get_music_video_stream_info",
            media_id=self.base.parse_catalog_media_id(metadata),
        )

        url_media_id = self.base.parse_media_id_from_url(metadata)
        m3u8_master_url = None

        if url_media_id == metadata["id"]:
            m3u8_master_url = self._get_m3u8_master_url_from_itunes_page_metadata(
                itunes_page_metadata,
            )

        if not m3u8_master_url:
            webplayback_response = await self.base.apple_music_api.get_webplayback(
                metadata["id"]
            )
            m3u8_master_url = self._get_m3u8_master_url_from_webplayback(
                webplayback_response["songList"][0],
            )

        master_text = (await self.base.get_response(m3u8_master_url)).text
        playlist_master_m3u8_obj = m3u8.loads(master_text)
        playlist_master_m3u8_obj._original_text = master_text
        playlist_master_m3u8_obj.base_uri = m3u8_master_url.rpartition("/")[0]
        stream_info_video = await self._get_stream_info_video(playlist_master_m3u8_obj)
        stream_info_audio = await self._get_stream_info_audio(
            playlist_master_m3u8_obj.data,
        )
        if not stream_info_video or not stream_info_audio:
            return None

        use_mp4 = any(
            stream_info_video.codec.startswith(codec) for codec in MP4_FORMAT_CODECS
        ) or any(
            stream_info_audio.codec.startswith(codec) for codec in MP4_FORMAT_CODECS
        )
        if use_mp4:
            file_format = MediaFileFormat.MP4
        else:
            file_format = MediaFileFormat.M4V

        stream_info = StreamInfoAv(
            media_id=self.base.parse_catalog_media_id(metadata),
            video_track=stream_info_video,
            audio_track=stream_info_audio,
            file_format=file_format,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    def _get_video_playlist_from_resolution(
        self,
        video_playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist | None:
        playlist_results = []
        for codec_index, codec in enumerate(self.codec_priority):
            for playlist in video_playlists:
                if playlist.stream_info.codecs.startswith(codec.fourcc()):
                    playlist_results.append((codec_index, playlist))

        if not playlist_results:
            return None

        def sort_key(
            item: tuple[int, m3u8.Playlist],
        ) -> tuple[bool, int, int, int, int]:
            codec_index, playlist = item
            playlist_resolution = playlist.stream_info.resolution[-1]
            bandwidth = playlist.stream_info.bandwidth
            exceeds_resolution = playlist_resolution > int(self.resolution)
            resolution_difference = abs(playlist_resolution - int(self.resolution))

            return (
                exceeds_resolution,
                resolution_difference,
                codec_index,
                -playlist_resolution,
                -bandwidth,
            )

        playlist_results.sort(key=sort_key)
        return playlist_results[0][1]

    def _get_best_stereo_audio_playlist(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        audio_playlist = next(
            (
                media
                for media in playlist_master_data["media"]
                if media["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        return audio_playlist

    async def _get_video_playlist_from_user(
        self,
        video_playlists: list[m3u8.Playlist],
    ) -> m3u8.Playlist | None:
        if self.ask_video_codec_function:
            video_playlist = self.ask_video_codec_function(video_playlists)
            if asyncio.iscoroutine(video_playlist):
                video_playlist = await video_playlist

            return video_playlist

        return None

    async def _get_audio_playlist_from_user(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        if self.ask_audio_codec_function:
            audio_playlist = self.ask_audio_codec_function(
                [
                    playlist
                    for playlist in playlist_master_data["media"]
                    if playlist.get("uri")
                ]
            )
            if asyncio.iscoroutine(audio_playlist):
                audio_playlist = await audio_playlist

            return audio_playlist

        return None

    def _get_key_by_format(
        self,
        m3u8_obj: m3u8.M3U8,
        key_format: str,
    ) -> str | None:
        match = next(
            (key for key in m3u8_obj.keys if key.keyformat == key_format),
            None,
        )
        return match.uri if match is not None else None

    def _get_widevine_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )

    def _get_playready_pssh(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.microsoft.playready",
        )

    def _get_fairplay_key(self, m3u8_obj: m3u8.M3U8) -> str:
        return self._get_key_by_format(
            m3u8_obj,
            "com.apple.streamingkeydelivery",
        )

    @staticmethod
    def _playlist_uses_playready(master_m3u8_text: str, variant_uri: str) -> bool:
        """Return True if the chosen variant's ALLOWED-CPC contains 'SL3000'.

        Apple uses SL3000 (Security Level 3000) exclusively for 4K PlayReady
        streams. This is the same detection strategy used by AMD.
        """
        attrs: dict | None = None
        for raw_line in master_m3u8_text.splitlines():
            line = raw_line.strip()
            if line.startswith("#EXT-X-STREAM-INF:"):
                attr_str = line[len("#EXT-X-STREAM-INF:"):]
                attrs = {
                    k: v.strip('"')
                    for part in attr_str.split(",")
                    if "=" in part
                    for k, v in [part.split("=", 1)]
                }
                continue
            if attrs is None or not line or line.startswith("#"):
                continue
            if line == variant_uri:
                return "SL3000" in attrs.get("ALLOWED-CPC", "").upper()
            attrs = None
        return False

    async def _get_stream_info_video(
        self,
        playlist_master_m3u8_obj: m3u8.M3U8,
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in self.codec_priority:
            playlist = self._get_video_playlist_from_resolution(
                playlist_master_m3u8_obj.playlists,
            )
        else:
            playlist = await self._get_video_playlist_from_user(
                playlist_master_m3u8_obj.playlists
            )

        if not playlist:
            return None

        stream_info.stream_url = playlist.uri
        stream_info.codec = playlist.stream_info.codecs
        stream_info.width, stream_info.height = playlist.stream_info.resolution

        # Detect PlayReady before fetching the media playlist
        master_text = getattr(playlist_master_m3u8_obj, "_original_text", "")
        stream_info.use_playready = self._playlist_uses_playready(
            master_text, playlist.uri
        )

        playlist_m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self._get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self._get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self._get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def _get_stream_info_audio(
        self,
        playlist_master_data: dict,
    ) -> StreamInfo | None:
        stream_info = StreamInfo()

        if MusicVideoCodec.ASK not in self.codec_priority:
            playlist = self._get_best_stereo_audio_playlist(playlist_master_data)
        else:
            playlist = await self._get_audio_playlist_from_user(playlist_master_data)

        if not playlist:
            return None

        stream_info.stream_url = playlist["uri"]
        stream_info.codec = playlist["group_id"]

        playlist_m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )
        stream_info.widevine_pssh = self._get_widevine_pssh(playlist_m3u8_obj)
        stream_info.fairplay_key = self._get_fairplay_key(playlist_m3u8_obj)
        stream_info.playready_pssh = self._get_playready_pssh(playlist_m3u8_obj)

        return stream_info

    async def get_decryption_key(
        self,
        stream_info: StreamInfoAv,
    ) -> DecryptionKeyAv:
        # Apple Music 4K MV streams always include both Widevine and PlayReady
        # PSSH in the media playlist. However only PlayReady licenses are
        # granted for HVC1 4K content — Widevine requests return status=-1021.
        # Presence of playready_pssh on the video track is the definitive
        # signal; use_playready (SL3000 flag) is kept as a secondary indicator.
        video_uses_pr = bool(stream_info.video_track.playready_pssh)

        logger.debug(
            "playready_detection",
            use_playready_flag=stream_info.video_track.use_playready,
            has_widevine_pssh=bool(stream_info.video_track.widevine_pssh),
            has_playready_pssh=bool(stream_info.video_track.playready_pssh),
            video_uses_pr=video_uses_pr,
        )

        if video_uses_pr:
            logger.debug(
                "get_decryption_key_playready",
                media_id=stream_info.media_id,
            )
            decryption_key_video = await self._get_playready_key_subprocess(
                stream_info.media_id,
                stream_info.video_track.playready_pssh,
            )
        else:
            decryption_key_video = await self.base.get_decryption_key(
                stream_info.video_track.widevine_pssh,
                stream_info.media_id,
            )

        decryption_key_audio = await self.base.get_decryption_key(
            stream_info.audio_track.widevine_pssh,
            stream_info.media_id,
        )

        return DecryptionKeyAv(
            video_track=decryption_key_video,
            audio_track=decryption_key_audio,
        )

    async def _get_playready_key_subprocess(
        self,
        adam_id: str,
        playready_pssh_uri: str,
    ) -> DecryptionKey:
        """Obtain a PlayReady content key via the prkey binary.

        prkey uses puppyready (Go) to generate a PlayReady challenge,
        exchanges it with wrapper-lite at /license?drm-type=pr,
        and prints kid:key (hex) to stdout.

        Build prkey from the prkey-standalone source directory:
            GOOS=android GOARCH=arm64 go build -o ~/prkey .
            cp ~/prkey $PREFIX/bin/prkey
        """
        import shutil

        prkey_path = shutil.which("prkey")
        if not prkey_path:
            raise RuntimeError(
                "prkey binary not found in PATH. "
                "Build from prkey-standalone/ with: "
                "GOOS=android GOARCH=arm64 go build -o prkey . "
                "then copy to $PREFIX/bin/"
            )

        proc = await asyncio.create_subprocess_exec(
            prkey_path,
            "-adam-id",    adam_id,
            "-pssh",       playready_pssh_uri,
            "-lite-server", self.base.wrapper_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"prkey failed (exit {proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

        output = stdout.decode().strip()
        if ":" not in output:
            raise RuntimeError(f"prkey unexpected output: {output!r}")

        kid, key = output.split(":", 1)
        return DecryptionKey(kid=kid, key=key)

    @staticmethod
    def _has_albums_relationship(media_metadata: dict) -> bool:
        """Return True only when the albums relationship is fully embedded.

        A track stub coming from get_album() has no 'relationships' key at all
        (or has one that lacks 'albums'), so get_tags() would receive an empty
        amp_album_attr and lose album sort / compilation / copyright fields.
        """
        try:
            data = media_metadata["relationships"]["albums"]["data"]
            return bool(data) and "attributes" in data[0]
        except (KeyError, IndexError, TypeError):
            return False

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        # Fetch full music video object (with include=albums) when:
        #   - no metadata yet (music-video URL with no pre-fetch), OR
        #   - metadata is a shallow track stub from get_album() that lacks the
        #     albums relationship needed by get_tags()
        if not media.media_metadata or not self._has_albums_relationship(
            media.media_metadata
        ):
            media.media_metadata = (
                await self.base.apple_music_api.get_music_video(media.media_id)
            )["data"][0]

        media.media_id = self.base.parse_catalog_media_id(media.media_metadata)

        yield media

        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(media.media_id)

        if media.playlist_metadata:
            media.playlist_tags = self.base.get_playlist_tags(
                media.playlist_metadata,
                media.index,
            )

        itunes_page_metadata = await self.get_itunes_page_metadata(media.media_metadata)
        media.tags = await self.get_tags(
            media.media_metadata,
            itunes_page_metadata,
        )

        # Resolve stream_info first to get actual video resolution for the cover URL.
        media.stream_info = await self.get_stream_info(
            media.media_metadata,
            itunes_page_metadata,
        )
        if not media.stream_info:
            raise GamdlInterfaceFormatNotAvailableError(
                media.media_id,
                self.codec_priority,
            )

        if (
            not media.stream_info.video_track.widevine_pssh
            or not media.stream_info.audio_track.widevine_pssh
        ):
            raise GamdlInterfaceDecryptionNotAvailableError(media.media_id)

        media.decryption_key = await self.get_decryption_key(media.stream_info)

        media.cover = await self.base.get_cover_mv(
            media.media_metadata,
            width=media.stream_info.video_track.width,
            height=media.stream_info.video_track.height,
        )

        media.partial = False

        yield media
