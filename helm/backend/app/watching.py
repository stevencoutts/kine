"""Now-playing sessions from Plex and Emby, using Settings credentials."""
from __future__ import annotations

import asyncio
import os
import re
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


_STREAM_UUID_RE = re.compile(
    r"/stream/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_HDHR_NUMBER_RE = re.compile(r"/auto/v(\d+)", re.IGNORECASE)


def _channel_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value:
            return None
        if value == int(value):
            return str(int(value))
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text if text.isdigit() else None


def _norm_channel_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _tuner_ref(path: str | None) -> tuple[str | None, str | None]:
    """Channel UUID or HDHomeRun number embedded in a Plex/Emby media path."""
    if not path or not isinstance(path, str):
        return None, None
    uuid_match = _STREAM_UUID_RE.search(path)
    number_match = _HDHR_NUMBER_RE.search(path)
    channel_id = uuid_match.group(1).lower() if uuid_match else None
    number = number_match.group(1) if number_match else None
    return channel_id, number


def _first_tuner(paths: list) -> tuple[str | None, str | None]:
    channel_id = None
    number = None
    for path in paths:
        found_id, found_number = _tuner_ref(path if isinstance(path, str) else None)
        if channel_id is None and found_id:
            channel_id = found_id
        if number is None and found_number:
            number = found_number
    return channel_id, number


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("results", "channels", "streams"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _account_name(stream: dict, accounts: dict[str, str]) -> str | None:
    raw = stream.get("m3u_account")
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        if name:
            return name
        raw = raw.get("id")
    if raw is None:
        raw = stream.get("m3u_account_id")
    if raw is None:
        return None
    return accounts.get(str(raw))


def _plex_live(item: dict) -> bool:
    """Plex live TV sessions are often type=movie/episode and omit the channel."""
    if str(item.get("live") or "").strip().lower() in {"1", "true"}:
        return True
    if str(item.get("key") or "").startswith("/livetv/"):
        return True
    try:
        if int(item.get("librarySectionID")) == -4:
            return True
    except (TypeError, ValueError):
        pass
    for media in _as_list(item.get("Media")):
        if isinstance(media, dict) and str(media.get("origin") or "").lower() == "livetv":
            return True
    return False


def index_live_providers(
    stats: Any,
    streams: Any,
    accounts: Any,
    channels: Any = None,
    programs: Any = None,
) -> list[dict]:
    """Active Dispatcharr channels, each labelled with the M3U account in use."""
    account_names: dict[str, str] = {}
    for row in accounts or []:
        if isinstance(row, dict) and row.get("id") is not None and str(row.get("name") or "").strip():
            account_names[str(row["id"])] = str(row["name"]).strip()
    stream_provider: dict[str, str] = {}
    for stream in _rows(streams):
        sid = stream.get("id")
        if sid is None:
            continue
        name = _account_name(stream, account_names)
        if name:
            stream_provider[str(sid)] = name
    detail_by_id: dict[str, dict] = {}
    for row in _rows(channels):
        cid = row.get("uuid") or row.get("channel_id")
        if cid:
            detail_by_id[str(cid).lower()] = row
    program_by_id: dict[str, str] = {}
    for row in _rows(programs):
        cid = str(row.get("channel_uuid") or row.get("uuid") or "").strip().lower()
        title = str(row.get("title") or "").strip()
        if cid and title:
            program_by_id[cid] = title
    live = stats.get("live") if isinstance(stats, dict) and "live" in stats else stats
    indexed: list[dict] = []
    for channel in _rows(live):
        sid = channel.get("stream_id")
        provider = stream_provider.get(str(sid)) if sid is not None else None
        if not provider:
            continue
        cid = str(channel.get("channel_id") or "").strip().lower() or None
        detail = detail_by_id.get(cid or "", {})
        names: list[str] = []
        for candidate in (
            detail.get("effective_name"),
            channel.get("channel_name"),
            detail.get("name"),
        ):
            text = str(candidate or "").strip()
            if text and text not in names:
                names.append(text)
        number = (
            _channel_number(detail.get("effective_channel_number"))
            or _channel_number(detail.get("channel_number"))
            or _channel_number(channel.get("channel_number"))
        )
        indexed.append({
            "channel_id": cid,
            "name": names[0] if names else None,
            "names": names,
            "number": number,
            "program": program_by_id.get(cid or ""),
            "provider": provider,
        })
    return indexed


def _remember(bucket: dict[str, str | None], key: str | None, provider: str) -> None:
    if not key:
        return
    current = bucket.get(key, provider)
    bucket[key] = provider if current == provider else None


def _remember_row(bucket: dict[str, dict | None], key: str | None, row: dict) -> None:
    if not key:
        return
    if key not in bucket:
        bucket[key] = row
        return
    current = bucket[key]
    if current is not None and current.get("channel_id") != row.get("channel_id"):
        bucket[key] = None


def _label_live_program(session: dict, row: dict) -> None:
    """Plex live sessions name the programme, not the channel."""
    if session.get("channel"):
        return
    name = str(row.get("name") or "").strip()
    number = row.get("number")
    program = str(session.get("program") or "").strip()
    if not name:
        return
    session["channel"] = str(number) if number else name
    if number:
        session["channel_number"] = number
    if program and _norm_channel_name(program) != _norm_channel_name(name):
        session["title"] = f"{name} — {program}"
    else:
        session["title"] = f"{number} — {name}" if number else name


def attach_live_providers(sessions: list[dict], rows: list[dict]) -> list[dict]:
    """Set provider on live sessions. Library plays are left alone."""
    by_id: dict[str, str | None] = {}
    by_number: dict[str, str | None] = {}
    by_name: dict[str, str | None] = {}
    by_program: dict[str, dict | None] = {}
    for row in rows:
        provider = str(row.get("provider") or "").strip()
        if not provider:
            continue
        _remember(by_id, str(row.get("channel_id") or "").strip().lower() or None, provider)
        _remember(by_number, _channel_number(row.get("number")), provider)
        aliases = row.get("names") if isinstance(row.get("names"), list) else [row.get("name")]
        for alias in aliases:
            _remember(by_name, _norm_channel_name(alias) or None, provider)
        _remember_row(by_program, _norm_channel_name(row.get("program")) or None, row)
    for session in sessions:
        # A library file has no tuner id. A live play can still carry one when
        # Plex/Emby label the session as an episode.
        if session.get("kind") != "channel" and not session.get("channel_id"):
            session.pop("provider", None)
            continue
        provider = None
        matched: dict | None = None
        channel_id = str(session.get("channel_id") or "").strip().lower()
        if channel_id:
            provider = by_id.get(channel_id)
        if not provider:
            number = _channel_number(session.get("channel_number")) or _channel_number(session.get("channel"))
            if number:
                provider = by_number.get(number)
        if not provider:
            name = _norm_channel_name(session.get("channel"))
            if name:
                provider = by_name.get(name)
        if not provider:
            head = str(session.get("title") or "").split(" — ")[0]
            name = _norm_channel_name(head)
            if name:
                provider = by_name.get(name)
        if not provider:
            program = _norm_channel_name(session.get("program"))
            if program and program in by_program:
                matched = by_program[program]
                provider = str(matched.get("provider") or "").strip() if matched else None
        if provider:
            session["provider"] = provider
            if matched:
                _label_live_program(session, matched)
        else:
            session.pop("provider", None)
    return sessions


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
# Home playback never needs a 2 Gbps badge. INT32_MAX (2147483647) kbps is a
# live-TV sentinel that otherwise renders as "2147484 Mbps".
_MAX_PLAUSIBLE_BPS = 400_000_000


def _sane_bps(bps: int | None) -> int | None:
    if bps is None or bps <= 0 or bps > _MAX_PLAUSIBLE_BPS:
        return None
    return bps


def _kbps_to_bps(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        kbps = int(float(value))
    except (TypeError, ValueError):
        return None
    if kbps <= 0:
        return None
    return _sane_bps(kbps * 1000)


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
        bps = _kbps_to_bps(session_kbps)
        if bps:
            return bps

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
        program = None
        plex_live = _plex_live(item)
        if plex_live or kind in {"live", "channel"} or channel:
            kind = "channel"
            program = title if plex_live else None
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
        tuner_paths = []
        for media_item in _as_list(item.get("Media")):
            if not isinstance(media_item, dict):
                continue
            for part in _as_list(media_item.get("Part")):
                if isinstance(part, dict):
                    tuner_paths.append(part.get("file") or part.get("key"))
        channel_id, tuner_number = _first_tuner(tuner_paths)
        channel_number = _channel_number(item.get("channelIdentifier")) or tuner_number
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
            "channel_number": channel_number,
            "channel_id": channel_id,
            "program": program,
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

    bitrate_bps = _sane_bps(bitrate_bps)
    if bitrate_bps is None:
        bitrate_bps = _estimate_bitrate_bps_from_resolution({
            "height": height,
            "videoResolution": str(height) if height else "",
        })

    bitrate_label = _bitrate_label(bitrate_bps)

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
        tuner_paths = [now.get("Path")]
        for src in _as_list(now.get("MediaSources")):
            if isinstance(src, dict):
                tuner_paths.append(src.get("Path"))
        channel_id, tuner_number = _first_tuner(tuner_paths)
        channel_number = _channel_number(now.get("ChannelNumber")) or tuner_number
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
            "channel_number": channel_number,
            "channel_id": channel_id,
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


async def _fetch_live_provider_rows() -> list[dict]:
    """Ask Dispatcharr which M3U account is serving each active channel."""
    from . import dispatcharr_sources as sources

    if not sources.enabled() or not sources.configured():
        return []
    try:
        stats = await asyncio.to_thread(sources._request, "GET", "/proxy/stats/")
    except Exception:
        return []
    active = []
    if isinstance(stats, dict) and isinstance(stats.get("live"), dict):
        active = [row for row in _as_list(stats["live"].get("channels")) if isinstance(row, dict)]
    if not active:
        return []
    stream_ids: list[int] = []
    uuids: list[str] = []
    for row in active:
        try:
            stream_ids.append(int(row["stream_id"]))
        except (KeyError, TypeError, ValueError):
            pass
        cid = str(row.get("channel_id") or "").strip()
        if cid:
            uuids.append(cid)

    async def _safe(func, *args, **kwargs):
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception:
            return []

    async def _empty():
        return []

    streams, details, accounts, programs = await asyncio.gather(
        _safe(
            sources._request,
            "POST",
            "/api/channels/streams/by-ids/",
            json={"ids": stream_ids},
        ) if stream_ids else _empty(),
        _safe(
            sources._request,
            "POST",
            "/api/channels/channels/by-uuids/",
            json={"uuids": uuids},
        ) if uuids else _empty(),
        _safe(sources.list_m3u),
        _safe(
            sources._request,
            "POST",
            "/api/epg/current-programs/",
            json={"channel_uuids": uuids},
        ) if uuids else _empty(),
    )
    return index_live_providers(stats, streams, accounts, details, programs)


async def snapshot() -> dict:
    env = config.read()
    plex_task = asyncio.create_task(_plex_sessions(env))
    emby_task = asyncio.create_task(_emby_sessions(env))
    plex_rows, plex_error = await plex_task
    emby_rows, emby_error = await emby_task
    sessions = sort_watching_sessions([*plex_rows, *emby_rows])
    if any(row.get("kind") == "channel" for row in sessions):
        try:
            attach_live_providers(sessions, await _fetch_live_provider_rows())
        except Exception:
            pass
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
