"""Now-playing sessions from Plex and Emby, using Settings credentials."""
from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import config


def media_base(host: str, port: str | int, use_ssl: bool) -> str:
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{host}:{port}"


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def progress_pct(position: int | float | None, duration: int | float | None) -> int | None:
    if not duration or duration <= 0 or position is None:
        return None
    return max(0, min(100, round(100 * float(position) / float(duration))))


def sort_watching_sessions(sessions: list[dict]) -> list[dict]:
    """Oldest / longest-running first.

    Emby and Plex reshuffle session order on every poll; without a stable sort
    the Watching cards jump around. Prefer higher playback position (been
    watching longer), then a stable session id / user / title tie-break.
    """
    return sorted(
        sessions,
        key=lambda s: (
            -(s.get("position_ms") or 0),
            str(s.get("session_id") or ""),
            str(s.get("user") or ""),
            str(s.get("title") or ""),
        ),
    )


def format_duration_ms(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    try:
        total = max(0, int(round(float(ms) / 1000.0)))
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_duration_ticks(ticks: int | float | None) -> str | None:
    """Emby/Jellyfin ticks are 100ns units (10_000_000 per second)."""
    if ticks is None:
        return None
    try:
        ms = float(ticks) / 10_000.0
    except (TypeError, ValueError):
        return None
    return format_duration_ms(ms)


def _season_episode(season: Any, episode: Any, title: str) -> str:
    try:
        s, e = int(season), int(episode)
    except (TypeError, ValueError):
        return title
    return f"S{s:02d}E{e:02d} {title}".strip()


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _basename(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    cleaned = path.strip().rstrip("/\\")
    if not cleaned:
        return None
    return os.path.basename(cleaned) or cleaned


def art_proxy_path(server: str, path: str | None) -> str | None:
    """Build a same-origin art URL for a Plex relative thumb path."""
    if server != "plex" or not path or not isinstance(path, str):
        return None
    cleaned = path.strip()
    if not cleaned.startswith("/") or cleaned.startswith("//") or ".." in cleaned:
        return None
    return f"/api/watching/art/plex?path={quote(cleaned, safe='')}"


def emby_art_proxy_url(
    item_id: str | None,
    tag: str | None = None,
    *,
    image: str = "Primary",
) -> str | None:
    if not item_id or not isinstance(item_id, str):
        return None
    cleaned = item_id.strip()
    if not cleaned or "/" in cleaned or ".." in cleaned:
        return None
    kind = (image or "Primary").strip()
    if kind not in {"Primary", "Logo", "Thumb", "Backdrop"}:
        kind = "Primary"
    query: dict[str, str] = {"item_id": cleaned, "image": kind}
    if tag and isinstance(tag, str) and tag.strip():
        query["tag"] = tag.strip()
    return f"/api/watching/art/emby?{urlencode(query)}"


def _format_fields(
    *,
    resolution: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    bitrate_label: str | None = None,
) -> dict[str, Any]:
    formats = [x for x in (resolution, video_codec, audio_codec, bitrate_label) if x]
    return {
        "resolution": resolution,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "formats": formats,
        "quality": " · ".join(formats) if formats else None,
    }


# Common Plex client quality caps (kbps). On LAN, Session.bandwidth is often one
# of these allotments rather than a measured delivery rate.
_PLEX_BANDWIDTH_CAPS_KBPS = frozenset({
    64, 96, 128, 192, 256, 320, 384, 448, 512, 720, 768,
    1000, 1500, 2000, 3000, 4000, 6000, 8000, 10000, 12000, 15000,
    20000, 25000, 30000, 40000, 60000, 100000, 200000, 300000, 400000,
})


def _kbps_to_bps(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        kbps = int(float(value))
    except (TypeError, ValueError):
        return None
    if kbps <= 0:
        return None
    return kbps * 1000


def _bitrate_label(bps: int | None) -> str | None:
    if not bps:
        return None
    return f"{round(bps / 1_000_000)} Mbps"


def _plex_looks_like_bandwidth_cap(kbps: int) -> bool:
    return kbps in _PLEX_BANDWIDTH_CAPS_KBPS


def _estimate_bitrate_bps_from_resolution(media: dict) -> int | None:
    """Rough delivery estimate when Plex omits file/session bitrate (live/LAN)."""
    height = None
    raw_h = media.get("height")
    if raw_h is not None:
        try:
            height = int(float(raw_h))
        except (TypeError, ValueError):
            height = None
    if height is None:
        res = str(media.get("videoResolution") or "").strip().lower()
        digits = "".join(ch for ch in res if ch.isdigit())
        if digits:
            try:
                height = int(digits)
            except ValueError:
                height = None
        elif "4k" in res or "uhd" in res:
            height = 2160
    if height is None or height <= 0:
        return None
    if height >= 2160:
        return 20_000_000
    if height >= 1080:
        return 8_000_000
    if height >= 720:
        return 5_000_000
    return 2_000_000


def _plex_video_stream_bitrate_bps(parts: list) -> int | None:
    for part in parts:
        if not isinstance(part, dict):
            continue
        for stream in _as_list(part.get("Stream")):
            if not isinstance(stream, dict):
                continue
            if stream.get("streamType") == 1:
                bps = _kbps_to_bps(stream.get("bitrate"))
                if bps:
                    return bps
    return None


def _plex_resolve_bitrate_bps(item: dict, media: dict, parts: list) -> int | None:
    """Prefer file bitrate, then measured Session.bandwidth, else resolution estimate."""
    bps = _kbps_to_bps(media.get("bitrate"))
    if bps:
        return bps
    bps = _plex_video_stream_bitrate_bps(parts)
    if bps:
        return bps

    session = item.get("Session") if isinstance(item.get("Session"), dict) else {}
    session_kbps = None
    raw = session.get("bandwidth")
    if raw is not None and raw != "":
        try:
            session_kbps = int(float(raw))
        except (TypeError, ValueError):
            session_kbps = None
    if session_kbps and session_kbps > 0 and not _plex_looks_like_bandwidth_cap(session_kbps):
        return session_kbps * 1000

    return _estimate_bitrate_bps_from_resolution(media)


def _plex_media_bits(item: dict) -> dict[str, Any]:
    """Return source/stream plus structured format fields."""
    empty = {
        "source": None,
        "stream": None,
        "bitrate_bps": None,
        **_format_fields(),
    }
    medias = _as_list(item.get("Media"))
    if not medias:
        return empty
    media = medias[0] if isinstance(medias[0], dict) else {}
    parts = _as_list(media.get("Part"))
    part = parts[0] if parts and isinstance(parts[0], dict) else {}
    source = _basename(part.get("file")) or (part.get("file") or "").strip() or None
    resolution = None
    res = media.get("videoResolution")
    if res:
        resolution = f"{res}p" if str(res).isdigit() else str(res)
    elif media.get("height"):
        resolution = f"{media['height']}p"
    video_codec = str(media["videoCodec"]).upper() if media.get("videoCodec") else None
    audio_codec = str(media["audioCodec"]).upper() if media.get("audioCodec") else None
    bitrate_bps = _plex_resolve_bitrate_bps(item, media, parts)
    bitrate_label = _bitrate_label(bitrate_bps)
    if item.get("TranscodeSession"):
        stream = "transcode"
    elif any(
        isinstance(s, dict) and s.get("streamType") == 1 and s.get("decision") == "transcode"
        for p in parts if isinstance(p, dict)
        for s in _as_list(p.get("Stream"))
    ):
        stream = "transcode"
    else:
        stream = "direct"
    return {
        "source": source,
        "stream": stream,
        "bitrate_bps": bitrate_bps,
        **_format_fields(
            resolution=resolution,
            video_codec=video_codec,
            audio_codec=audio_codec,
            bitrate_label=bitrate_label,
        ),
    }


def _plex_art_url(item: dict, kind: str) -> str | None:
    thumb = None
    if kind == "episode":
        thumb = (item.get("grandparentThumb") or item.get("thumb") or "").strip() or None
    else:
        thumb = (item.get("thumb") or item.get("parentThumb") or "").strip() or None
    return art_proxy_path("plex", thumb)


def parse_plex_sessions(payload: dict) -> list[dict]:
    container = payload.get("MediaContainer") or {}
    rows: list[dict] = []
    for item in _as_list(container.get("Metadata")):
        if not isinstance(item, dict):
            continue
        kind = (item.get("type") or "").lower()
        title = (item.get("title") or "Untitled").strip()
        year = item.get("year")
        show = (item.get("grandparentTitle") or "").strip() or None
        season = item.get("parentIndex")
        episode = item.get("index")
        channel = (
            (item.get("channelCallSign") or item.get("channelTitle") or item.get("channelIdentifier") or "")
            .strip()
            or None
        )
        if kind in {"live", "channel"} or channel:
            kind = "channel"
            display = channel or title
            if title and channel and title != channel:
                display = f"{channel} — {title}"
        elif kind == "episode":
            ep = _season_episode(season, episode, title)
            display = f"{show} — {ep}" if show else ep
        elif year:
            display = f"{title} ({year})"
        else:
            display = title

        position = item.get("viewOffset")
        duration = item.get("duration")
        remaining = None
        if position is not None and duration is not None:
            try:
                remaining = max(0, float(duration) - float(position))
            except (TypeError, ValueError):
                remaining = None

        user = ((item.get("User") or {}).get("title") or "").strip()
        player = item.get("Player") or {}
        media = _plex_media_bits(item)
        source = media["source"]
        if kind == "channel" and not source:
            source = channel or (item.get("channelIdentifier") or None)

        rows.append({
            "server": "plex",
            "session_id": str(item.get("sessionKey") or item.get("ratingKey") or "") or None,
            "kind": kind or "unknown",
            "title": display,
            "show": show,
            "episode_title": title if kind == "episode" else None,
            "season": season,
            "episode": episode,
            "year": year,
            "channel": channel,
            "user": user or "unknown",
            "player": (player.get("title") or player.get("product") or "").strip() or "Plex",
            "product": (player.get("product") or "").strip() or None,
            "platform": (player.get("platform") or "").strip() or None,
            "state": (player.get("state") or "playing").lower(),
            "progress": progress_pct(position, duration),
            "position_ms": int(position) if isinstance(position, (int, float)) else None,
            "duration_ms": int(duration) if isinstance(duration, (int, float)) else None,
            "remaining_ms": int(remaining) if isinstance(remaining, (int, float)) else None,
            "position_label": format_duration_ms(position),
            "duration_label": format_duration_ms(duration),
            "remaining_label": format_duration_ms(remaining),
            "library": (item.get("librarySectionTitle") or "").strip() or None,
            "source": source,
            "quality": media["quality"],
            "resolution": media["resolution"],
            "video_codec": media["video_codec"],
            "audio_codec": media["audio_codec"],
            "formats": media["formats"],
            "stream": media["stream"],
            "bitrate_bps": media.get("bitrate_bps"),
            "art_url": _plex_art_url(item, kind or "unknown"),
        })
    return rows


def _emby_media_bits(now: dict, session: dict) -> dict[str, Any]:
    sources = _as_list(now.get("MediaSources"))
    source = None
    bitrate_bps = None
    if sources and isinstance(sources[0], dict):
        src = sources[0]
        source = _basename(src.get("Path")) or (src.get("Path") or src.get("Name") or "").strip() or None
        for key in ("Bitrate", "bitrate"):
            if src.get(key):
                try:
                    bitrate_bps = int(float(src[key]))
                    break
                except (TypeError, ValueError):
                    pass
    if not source:
        source = _basename(now.get("Path")) or (now.get("Path") or "").strip() or None
    if bitrate_bps is None:
        for key in ("Bitrate", "bitrate"):
            if now.get(key):
                try:
                    bitrate_bps = int(float(now[key]))
                    break
                except (TypeError, ValueError):
                    pass
    transcode = session.get("TranscodingInfo") or {}
    if isinstance(transcode, dict) and transcode.get("Bitrate"):
        try:
            # Transcode bitrate is the actual delivery rate when remuxing/transcoding.
            bitrate_bps = int(float(transcode["Bitrate"]))
        except (TypeError, ValueError):
            pass

    resolution = None
    width = now.get("Width")
    height = now.get("Height")
    if height:
        resolution = f"{height}p"
    elif width and height:
        resolution = f"{width}x{height}"
    video_codec = None
    audio_codec = None
    streams = _as_list(now.get("MediaStreams"))
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if stream.get("Type") == "Video" and stream.get("Codec"):
            video_codec = str(stream["Codec"]).upper()
            break
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if stream.get("Type") == "Audio" and stream.get("Codec"):
            audio_codec = str(stream["Codec"]).upper()
            break

    bitrate_label = None
    if bitrate_bps:
        bitrate_label = f"{round(bitrate_bps / 1_000_000)} Mbps"

    if isinstance(transcode, dict) and (transcode.get("IsVideoDirect") is False or transcode.get("VideoCodec")):
        stream = "transcode"
    else:
        play = session.get("PlayState") or {}
        method = (play.get("PlayMethod") or "").lower()
        if "transcode" in method:
            stream = "transcode"
        else:
            stream = "direct"
    return {
        "source": source,
        "stream": stream,
        "bitrate_bps": bitrate_bps,
        **_format_fields(
            resolution=resolution,
            video_codec=video_codec,
            audio_codec=audio_codec,
            bitrate_label=bitrate_label,
        ),
    }


def _emby_art_url(now: dict, kind: str) -> str | None:
    if kind == "episode":
        series_id = (now.get("SeriesId") or "").strip()
        series_tag = (now.get("SeriesPrimaryImageTag") or "").strip() or None
        if series_id:
            return emby_art_proxy_url(series_id, series_tag, image="Primary")
    if kind == "channel":
        channel_id = (now.get("ChannelId") or now.get("Id") or "").strip()
        tags = now.get("ImageTags") if isinstance(now.get("ImageTags"), dict) else {}
        logo_tag = (tags.get("Logo") or "").strip() or None
        primary = (tags.get("Primary") or "").strip() or None
        if logo_tag:
            return emby_art_proxy_url(channel_id, logo_tag, image="Logo")
        return emby_art_proxy_url(channel_id, primary, image="Primary")
    item_id = (now.get("Id") or "").strip()
    tags = now.get("ImageTags") if isinstance(now.get("ImageTags"), dict) else {}
    tag = (tags.get("Primary") or "").strip() or None
    return emby_art_proxy_url(item_id, tag, image="Primary")


def parse_emby_sessions(payload: list | dict) -> list[dict]:
    sessions = payload if isinstance(payload, list) else payload.get("Items") or []
    rows: list[dict] = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        now = item.get("NowPlayingItem")
        if not isinstance(now, dict):
            continue
        kind_raw = (now.get("Type") or "").lower()
        title = (now.get("Name") or "Untitled").strip()
        year = now.get("ProductionYear")
        show = (now.get("SeriesName") or "").strip() or None
        season = now.get("ParentIndexNumber")
        episode = now.get("IndexNumber")
        channel = (
            (now.get("ChannelName") or now.get("ChannelNumber") or item.get("NowPlayingChannelName") or "")
            .strip()
            or None
        )
        if kind_raw in {"tvchannel", "livetvprogram", "channel"} or (
            channel and kind_raw in {"", "program"}
        ):
            kind = "channel"
            display = channel or title
            if title and channel and title != channel:
                display = f"{channel} — {title}"
        elif kind_raw == "episode":
            kind = "episode"
            ep = _season_episode(season, episode, title)
            display = f"{show} — {ep}" if show else ep
        else:
            kind = kind_raw or "movie"
            display = f"{title} ({year})" if year else title

        play = item.get("PlayState") or {}
        paused = bool(play.get("IsPaused"))
        runtime = now.get("RunTimeTicks")
        position = play.get("PositionTicks")
        remaining = None
        if position is not None and runtime is not None:
            try:
                remaining = max(0, float(runtime) - float(position))
            except (TypeError, ValueError):
                remaining = None

        # Convert ticks → ms for consistent frontend fields.
        position_ms = int(float(position) / 10_000.0) if isinstance(position, (int, float)) else None
        duration_ms = int(float(runtime) / 10_000.0) if isinstance(runtime, (int, float)) else None
        remaining_ms = int(float(remaining) / 10_000.0) if isinstance(remaining, (int, float)) else None

        media = _emby_media_bits(now, item)
        source = media["source"]
        if kind == "channel" and not source:
            source = channel

        rows.append({
            "server": "emby",
            "session_id": str(item.get("Id") or "") or None,
            "kind": kind,
            "title": display,
            "show": show,
            "episode_title": title if kind == "episode" else None,
            "season": season,
            "episode": episode,
            "year": year,
            "channel": channel,
            "user": (item.get("UserName") or "unknown").strip(),
            "player": (item.get("DeviceName") or item.get("Client") or "Emby").strip(),
            "product": (item.get("Client") or "").strip() or None,
            "platform": (item.get("ApplicationVersion") or "").strip() or None,
            "state": "paused" if paused else "playing",
            "progress": progress_pct(position, runtime),
            "position_ms": position_ms,
            "duration_ms": duration_ms,
            "remaining_ms": remaining_ms,
            "position_label": format_duration_ticks(position),
            "duration_label": format_duration_ticks(runtime),
            "remaining_label": format_duration_ticks(remaining),
            "library": (now.get("AlbumArtist") or now.get("CollectionType") or "").strip() or None,
            "source": source,
            "quality": media["quality"],
            "resolution": media["resolution"],
            "video_codec": media["video_codec"],
            "audio_codec": media["audio_codec"],
            "formats": media["formats"],
            "stream": media["stream"],
            "bitrate_bps": media.get("bitrate_bps"),
            "art_url": _emby_art_url(now, kind),
        })
    return rows


def _configured(env: dict, host_key: str, secret_key: str) -> bool:
    return bool(env.get(host_key, "").strip() and env.get(secret_key, "").strip())


async def _get_json(url: str, headers: dict, params: dict | None = None) -> Any:
    async with httpx.AsyncClient(timeout=6.0, verify=False, follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


async def _get_bytes(url: str, headers: dict, params: dict | None = None) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=8.0, verify=False, follow_redirects=True) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        return response.content, content_type


def _plex_base(env: dict) -> str | None:
    host = env.get("PLEX_HOST", "").strip()
    token = env.get("PLEX_TOKEN", "").strip()
    if not host or not token:
        return None
    port = env.get("PLEX_PORT", "").strip() or "32400"
    ssl = _env_bool(env.get("PLEX_USE_SSL", ""))
    return media_base(host, port, ssl)


def _emby_bases(env: dict) -> list[str]:
    token = env.get("EMBY_API_KEY", "").strip()
    host = env.get("EMBY_HOST", "").strip()
    candidates: list[str] = []
    if host and token:
        port = env.get("EMBY_PORT", "").strip() or (
            "443" if _env_bool(env.get("EMBY_USE_SSL", "")) else "8096"
        )
        ssl = _env_bool(env.get("EMBY_USE_SSL", ""))
        candidates.append(media_base(host, port, ssl))
    if "emby" in config.profiles() and token:
        internal = "http://emby:8096"
        if internal not in candidates:
            candidates.append(internal)
    return candidates


async def fetch_art(
    server: str,
    *,
    path: str | None = None,
    item_id: str | None = None,
    tag: str | None = None,
    image: str | None = None,
) -> tuple[bytes, str]:
    """Fetch poster/logo bytes from Plex or Emby using Settings credentials."""
    env = config.read()
    try:
        if server == "plex":
            if art_proxy_path("plex", path) is None:
                raise ValueError("invalid plex art path")
            base = _plex_base(env)
            token = env.get("PLEX_TOKEN", "").strip()
            if not base or not token:
                raise LookupError("plex not configured")
            # Photo transcoder returns a display-sized JPEG; raw /thumb is multi-MB.
            return await _get_bytes(
                f"{base}/photo/:/transcode",
                {"X-Plex-Token": token, "Accept": "image/jpeg"},
                {
                    "width": 420,
                    "height": 560,
                    "minSize": 1,
                    "upscale": 1,
                    "url": path,
                },
            )
        if server == "emby":
            if emby_art_proxy_url(item_id, tag, image=image or "Primary") is None:
                raise ValueError("invalid emby art item")
            token = env.get("EMBY_API_KEY", "").strip()
            bases = _emby_bases(env)
            if not token or not bases:
                raise LookupError("emby not configured")
            wanted = (image or "Primary").strip() or "Primary"
            if wanted not in {"Primary", "Logo", "Thumb", "Backdrop"}:
                wanted = "Primary"
            # Channels often only have Logo or only Primary — try both.
            image_types = [wanted]
            if wanted == "Logo":
                image_types.append("Primary")
            elif wanted == "Primary":
                pass
            last_error: Exception | None = None
            for base in bases:
                for image_type in image_types:
                    params: dict[str, Any] = {
                        "maxHeight": 420,
                        "maxWidth": 420,
                        "quality": 92,
                    }
                    if tag and tag.strip() and image_type == wanted:
                        params["tag"] = tag.strip()
                    try:
                        return await _get_bytes(
                            f"{base}/Items/{item_id.strip()}/Images/{image_type}",
                            {"X-Emby-Token": token},
                            params,
                        )
                    except httpx.HTTPError as exc:
                        last_error = exc
            raise LookupError(str(last_error) if last_error else "emby art unavailable")
        raise ValueError("unknown art server")
    except httpx.HTTPError as exc:
        raise LookupError(str(exc)) from exc


async def _plex_sessions(env: dict) -> tuple[list[dict], str | None]:
    host = env.get("PLEX_HOST", "").strip()
    token = env.get("PLEX_TOKEN", "").strip()
    if not host or not token:
        return [], None
    port = env.get("PLEX_PORT", "").strip() or "32400"
    ssl = _env_bool(env.get("PLEX_USE_SSL", ""))
    url = f"{media_base(host, port, ssl)}/status/sessions"
    try:
        data = await _get_json(
            url,
            {"X-Plex-Token": token, "Accept": "application/json"},
        )
        return parse_plex_sessions(data if isinstance(data, dict) else {}), None
    except httpx.HTTPError as exc:
        return [], str(exc)


async def _emby_sessions(env: dict) -> tuple[list[dict], str | None]:
    token = env.get("EMBY_API_KEY", "").strip()
    host = env.get("EMBY_HOST", "").strip()
    candidates: list[str] = []
    if host and token:
        port = env.get("EMBY_PORT", "").strip() or (
            "443" if _env_bool(env.get("EMBY_USE_SSL", "")) else "8096"
        )
        ssl = _env_bool(env.get("EMBY_USE_SSL", ""))
        candidates.append(media_base(host, port, ssl))
    if "emby" in config.profiles() and token:
        internal = "http://emby:8096"
        if internal not in candidates:
            candidates.append(internal)
    if not token or not candidates:
        return [], None
    last_error: str | None = None
    for base in candidates:
        try:
            data = await _get_json(
                f"{base}/Sessions",
                {"X-Emby-Token": token},
                {"ActiveWithinSeconds": 300},
            )
            return parse_emby_sessions(data), None
        except httpx.HTTPError as exc:
            last_error = str(exc)
    return [], last_error


async def snapshot() -> dict:
    env = config.read()
    plex_task = asyncio.create_task(_plex_sessions(env))
    emby_task = asyncio.create_task(_emby_sessions(env))
    plex_rows, plex_error = await plex_task
    emby_rows, emby_error = await emby_task
    sessions = sort_watching_sessions([*plex_rows, *emby_rows])
    return {
        "ok": True,
        "configured": {
            "plex": _configured(env, "PLEX_HOST", "PLEX_TOKEN"),
            "emby": bool(env.get("EMBY_API_KEY", "").strip()) and (
                _configured(env, "EMBY_HOST", "EMBY_API_KEY") or "emby" in config.profiles()
            ),
        },
        "count": len(sessions),
        "sessions": sessions,
        "errors": {k: v for k, v in (("plex", plex_error), ("emby", emby_error)) if v},
    }
