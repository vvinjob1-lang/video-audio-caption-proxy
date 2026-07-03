import base64
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

APP_VERSION = "caption-proxy-v1-public-captions-only"
app = Flask(__name__)
CORS(app)

YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,my;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = int(os.getenv("YOUTUBE_CAPTION_TIMEOUT", "30"))
COOKIE_FILE = Path(__file__).resolve().parent / "cookies.txt"
GENERATED_COOKIE_FILE = Path(os.getenv("YOUTUBE_COOKIES_GENERATED_FILE", "/tmp/youtube_cookies.txt"))


def json_error(message, status_code=500, **extra):
    payload = {"ok": False, "success": False, "error": message, "version": APP_VERSION}
    payload.update(extra)
    return jsonify(payload), status_code


def normalize_youtube_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower().replace("www.", "")
    path = parsed.path.strip("/")
    video_id = None
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"} and path.startswith("shorts/"):
        video_id = path.split("/")[1]
    elif host == "youtu.be" and path:
        video_id = path.split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"} and (path.startswith("embed/") or path.startswith("live/")):
        video_id = path.split("/")[1]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    if video_id:
        video_id = re.sub(r"[^0-9A-Za-z_-]", "", video_id)
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def get_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(normalize_youtube_url(url))
        return parse_qs(parsed.query).get("v", [None])[0]
    except Exception:
        return None


def is_youtube_url(url: str) -> bool:
    try:
        host = urlparse((url or "").strip()).netloc.lower().replace("www.", "")
        return host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "youtube-nocookie.com"}
    except Exception:
        return False


def get_cookie_file() -> Path | None:
    cookie_b64 = os.getenv("YOUTUBE_COOKIES_B64") or os.getenv("YOUTUBE_COOKIES_BASE64")
    cookie_text = os.getenv("YOUTUBE_COOKIES_TXT")
    try:
        if cookie_b64:
            GENERATED_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            decoded = base64.b64decode(cookie_b64).decode("utf-8", errors="replace")
            GENERATED_COOKIE_FILE.write_text(decoded, encoding="utf-8")
            if GENERATED_COOKIE_FILE.stat().st_size > 0:
                return GENERATED_COOKIE_FILE
        if cookie_text:
            GENERATED_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            GENERATED_COOKIE_FILE.write_text(cookie_text, encoding="utf-8")
            if GENERATED_COOKIE_FILE.stat().st_size > 0:
                return GENERATED_COOKIE_FILE
    except Exception as exc:
        print(f"cookie setup warning: {exc}", flush=True)
    if COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 0:
        return COOKIE_FILE
    return None


def get_cookie_header() -> str:
    direct = (os.getenv("YOUTUBE_CAPTION_COOKIE_HEADER") or "").strip()
    if direct:
        return direct
    path = get_cookie_file()
    if not path:
        return ""
    pairs = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                name, value = parts[5], parts[6]
                if name and value:
                    pairs.append(f"{name}={value}")
    except Exception as exc:
        print(f"cookie parse warning: {exc}", flush=True)
    return "; ".join(pairs)


def yt_headers(extra=None):
    headers = dict(YOUTUBE_HEADERS)
    cookie_header = get_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header
    if extra:
        headers.update(extra)
    return headers


def srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(float(seconds or 0) * 1000))
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1000
    millis = milliseconds % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_ts_to_srt(ts: str) -> str:
    ts = (ts or "").strip().replace(",", ".")
    if re.fullmatch(r"\d+(?:\.\d+)?", ts):
        return srt_timestamp(float(ts))
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds_float = float(parts[1])
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds_float = float(parts[2])
        else:
            return "00:00:00,000"
        seconds = int(seconds_float)
        millis = int(round((seconds_float - seconds) * 1000))
        if millis >= 1000:
            seconds += 1
            millis -= 1000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    except Exception:
        return "00:00:00,000"


def clean_caption_line(line: str) -> str:
    line = html.unescape((line or "").strip())
    line = re.sub(r"<\d{1,2}:\d{2}:\d{2}[.,]\d{3}>", "", line)
    line = re.sub(r"<[^>]+>", "", line)
    line = re.sub(r"\{[^}]*\}", "", line)
    line = line.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", line).strip()


def vtt_to_srt(vtt_text: str) -> str:
    text = (vtt_text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    blocks = []
    i = 0
    cue_number = 1
    time_re = re.compile(r"((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})")
    while i < len(lines):
        line = (lines[i] or "").strip()
        upper = line.upper()
        if not line or upper == "WEBVTT" or upper.startswith("X-TIMESTAMP"):
            i += 1
            continue
        if upper.startswith(("NOTE", "STYLE", "REGION")):
            i += 1
            while i < len(lines) and (lines[i] or "").strip():
                i += 1
            continue
        match = time_re.search(line)
        if not match and i + 1 < len(lines):
            next_line = (lines[i + 1] or "").strip()
            match = time_re.search(next_line)
            if match:
                i += 1
        if not match:
            i += 1
            continue
        start = caption_ts_to_srt(match.group(1))
        end = caption_ts_to_srt(match.group(2))
        i += 1
        text_lines = []
        seen = set()
        while i < len(lines) and (lines[i] or "").strip():
            cleaned = clean_caption_line(lines[i])
            if cleaned and cleaned.casefold() not in seen:
                seen.add(cleaned.casefold())
                text_lines.append(cleaned)
            i += 1
        cue_text = re.sub(r"\s+", " ", " ".join(text_lines)).strip()
        if cue_text:
            blocks.append(f"{cue_number}\n{start} --> {end}\n{cue_text}\n")
            cue_number += 1
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def json3_to_srt(json_text: str) -> str:
    try:
        data = json.loads((json_text or "").strip())
    except Exception:
        return ""
    blocks = []
    for event in data.get("events") or []:
        text = clean_caption_line("".join(seg.get("utf8") or "" for seg in event.get("segs") or []))
        if not text:
            continue
        start_ms = int(event.get("tStartMs") or 0)
        dur_ms = int(event.get("dDurationMs") or 3000)
        blocks.append(f"{len(blocks)+1}\n{srt_timestamp(start_ms/1000)} --> {srt_timestamp((start_ms + max(dur_ms, 500))/1000)}\n{text}\n")
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def xml_caption_to_srt(xml_text: str) -> str:
    raw = (xml_text or "").strip()
    if not raw:
        return ""
    try:
        raw2 = re.sub(r"xmlns(:\w+)?=\"[^\"]+\"", "", raw)
        root = ET.fromstring(raw2)
    except Exception:
        return ""
    blocks = []
    # YouTube srv XML: <transcript><text start="..." dur="...">...
    for node in root.findall(".//text"):
        try:
            start = float(node.attrib.get("start", "0") or 0)
            dur = float(node.attrib.get("dur", "3") or 3)
            text = clean_caption_line("".join(node.itertext()))
            if text:
                blocks.append(f"{len(blocks)+1}\n{srt_timestamp(start)} --> {srt_timestamp(start+dur)}\n{text}\n")
        except Exception:
            continue
    if blocks:
        return "\n".join(blocks).strip() + "\n"
    # TTML: <p begin="..." end="...">...
    for node in root.iter():
        if node.tag.split("}")[-1].lower() != "p":
            continue
        start = node.attrib.get("begin") or node.attrib.get("start") or "00:00:00.000"
        end = node.attrib.get("end") or start
        text = clean_caption_line(" ".join(node.itertext()))
        if text:
            blocks.append(f"{len(blocks)+1}\n{caption_ts_to_srt(start)} --> {caption_ts_to_srt(end)}\n{text}\n")
    return "\n".join(blocks).strip() + "\n" if blocks else ""


def normalize_caption_to_srt(caption_text: str, ext: str | None = None) -> str:
    raw = (caption_text or "").strip()
    ext_low = (ext or "").lower()
    if not raw:
        return ""
    if ext_low == "srt" or ("-->" in raw and not raw.lstrip("\ufeff").upper().startswith("WEBVTT")):
        return raw.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if ext_low == "vtt" or raw.lstrip("\ufeff").upper().startswith("WEBVTT"):
        return vtt_to_srt(raw)
    if ext_low == "json3" or raw.startswith("{"):
        return json3_to_srt(raw)
    if ext_low in {"srv1", "srv2", "srv3", "xml", "ttml"} or raw.lstrip().startswith("<"):
        return xml_caption_to_srt(raw)
    return ""


def caption_lang_candidates(requested_language=None):
    env_value = os.getenv("YOUTUBE_CAPTION_LANGUAGES", "en,en-US,en-GB,en.*,my,und,auto,*")
    candidates = []
    if requested_language and str(requested_language).lower() not in {"auto", "detect"}:
        candidates.append(str(requested_language).strip())
    for item in env_value.split(","):
        value = item.strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates if "*" in candidates else [*candidates, "*"]


def lang_matches(key, candidate):
    key_low = (key or "").lower()
    cand = (candidate or "").lower()
    if cand in {"*", "auto", "all"}:
        return True
    if cand.endswith(".*"):
        base = cand[:-2]
        return key_low == base or key_low.startswith(base + "-")
    return key_low == cand


def pick_track(tracks, requested_language=None):
    if not tracks:
        return None, None
    manual = [t for t in tracks if (t.get("kind") or "") != "asr"]
    auto = [t for t in tracks if (t.get("kind") or "") == "asr"]
    candidates = caption_lang_candidates(requested_language)
    for source_name, pool in [("youtube_caption_tracks", manual), ("youtube_auto_caption", auto), ("youtube_caption_tracks", tracks)]:
        for candidate in candidates:
            for track in pool:
                lang = track.get("languageCode") or track.get("lang") or track.get("language") or ""
                if lang_matches(lang, candidate):
                    return source_name, track
    return "youtube_caption_tracks", tracks[0]


def set_query_param(url: str, **params) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def extract_json_after_marker(text: str, marker: str):
    pos = text.find(marker)
    if pos < 0:
        return None
    start = text.find("{", pos)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except Exception:
                    return None
    return None


def fetch_watch_page(video_id):
    watch_urls = [
        f"https://www.youtube.com/watch?v={video_id}&hl=en&persist_hl=1&bpctr=9999999999&has_verified=1",
        f"https://m.youtube.com/watch?v={video_id}&hl=en&persist_hl=1&bpctr=9999999999&has_verified=1",
    ]
    errors = []
    for watch_url in watch_urls:
        try:
            resp = requests.get(watch_url, headers=yt_headers(), timeout=TIMEOUT)
            if resp.status_code < 400 and resp.text:
                return resp.text, watch_url, errors
            errors.append({"watch_url": watch_url, "status_code": resp.status_code, "chars": len(resp.text or "")})
        except Exception as exc:
            errors.append({"watch_url": watch_url, "error": str(exc)})
    return "", "", errors


def fetch_caption_from_tracks(tracks, requested_language=None):
    source_name, track = pick_track(tracks, requested_language)
    if not track:
        return None
    base_url = track.get("baseUrl") or track.get("url") or ""
    if not base_url:
        return None
    for fmt in ["vtt", "json3", "srv3", "ttml"]:
        try:
            caption_url = set_query_param(base_url, fmt=fmt)
            resp = requests.get(caption_url, headers=yt_headers(), timeout=TIMEOUT)
            if resp.status_code >= 400 or not resp.text.strip():
                continue
            srt_text = normalize_caption_to_srt(resp.text, fmt)
            if srt_text.strip():
                return srt_text, {
                    "source": source_name,
                    "subtitle_source": source_name if track.get("kind") != "asr" else "youtube_auto_caption",
                    "language": track.get("languageCode") or track.get("lang") or "",
                    "format": fmt,
                }
        except Exception:
            continue
    return None


def get_watch_page_captions(url, requested_language=None):
    video_id = get_video_id(url)
    if not video_id:
        return None
    html_text, watch_url, errors = fetch_watch_page(video_id)
    if not html_text:
        return None
    player_response = extract_json_after_marker(html_text, "ytInitialPlayerResponse")
    if not player_response:
        return None
    tracks = (((player_response.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or [])
    if not tracks:
        return None
    result = fetch_caption_from_tracks(tracks, requested_language)
    if result:
        srt_text, meta = result
        manual_languages = sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") != "asr"})
        auto_languages = sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") == "asr"})
        meta.update({
            "video_id": video_id,
            "source_url": normalize_youtube_url(url),
            "title": (player_response.get("videoDetails") or {}).get("title") or "YouTube captions",
            "manual_languages": manual_languages,
            "auto_languages": auto_languages,
            "watch_url": watch_url,
        })
        return srt_text, meta
    return None


def extract_innertube_context(html_text):
    api_key = None
    client_version = None
    visitor_data = None
    m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html_text)
    if m:
        api_key = m.group(1)
    m = re.search(r'"clientVersion"\s*:\s*"([^"]+)"', html_text)
    if m:
        client_version = m.group(1)
    m = re.search(r'"visitorData"\s*:\s*"([^"]+)"', html_text)
    if m:
        visitor_data = m.group(1)
    return api_key, client_version, visitor_data


def get_innertube_captions(url, requested_language=None):
    video_id = get_video_id(url)
    if not video_id:
        return None
    html_text, watch_url, watch_errors = fetch_watch_page(video_id)
    api_key, client_version, visitor_data = extract_innertube_context(html_text or "")
    api_key = api_key or os.getenv("YOUTUBE_INNERTUBE_API_KEY")
    client_version = client_version or os.getenv("YOUTUBE_INNERTUBE_CLIENT_VERSION", "2.20240726.00.00")
    if not api_key:
        return None
    clients = [
        {"clientName": "WEB", "clientVersion": client_version},
        {"clientName": "MWEB", "clientVersion": client_version},
        {"clientName": "WEB_EMBEDDED_PLAYER", "clientVersion": client_version},
        {"clientName": "ANDROID", "clientVersion": "19.09.37", "androidSdkVersion": 30},
    ]
    for client in clients:
        try:
            body = {
                "context": {"client": {**client, **({"visitorData": visitor_data} if visitor_data else {})}},
                "videoId": video_id,
                "contentCheckOk": True,
                "racyCheckOk": True,
            }
            resp = requests.post(
                f"https://www.youtube.com/youtubei/v1/player?key={api_key}",
                headers=yt_headers({"Content-Type": "application/json", "Origin": "https://www.youtube.com", "Referer": watch_url or f"https://www.youtube.com/watch?v={video_id}"}),
                json=body,
                timeout=TIMEOUT,
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            tracks = (((data.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks") or [])
            if not tracks:
                continue
            result = fetch_caption_from_tracks(tracks, requested_language)
            if result:
                srt_text, meta = result
                meta.update({
                    "source": "innertube_caption_tracks",
                    "subtitle_source": meta.get("subtitle_source") or "innertube_caption_tracks",
                    "video_id": video_id,
                    "source_url": normalize_youtube_url(url),
                    "title": (data.get("videoDetails") or {}).get("title") or "YouTube captions",
                    "manual_languages": sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") != "asr"}),
                    "auto_languages": sorted({t.get("languageCode") for t in tracks if t.get("languageCode") and t.get("kind") == "asr"}),
                    "client": client.get("clientName"),
                })
                return srt_text, meta
        except Exception:
            continue
    return None


def get_direct_timedtext(url, requested_language=None):
    video_id = get_video_id(url)
    if not video_id:
        return None
    tracks = []
    for host in ["https://www.youtube.com/api/timedtext", "https://video.google.com/timedtext"]:
        try:
            resp = requests.get(f"{host}?{urlencode({'type':'list','v':video_id})}", headers=yt_headers(), timeout=TIMEOUT)
            if resp.status_code >= 400:
                continue
            root = ET.fromstring(resp.text or "")
            for tr in root.findall(".//track"):
                lang = tr.attrib.get("lang_code") or tr.attrib.get("lang") or ""
                if lang:
                    tracks.append({"languageCode": lang, "kind": tr.attrib.get("kind") or "", "name": tr.attrib.get("name") or ""})
        except Exception:
            continue
    source_name, track = pick_track(tracks, requested_language)
    if not track:
        return None
    for fmt in ["vtt", "json3", "srv3", "ttml"]:
        params = {"v": video_id, "lang": track["languageCode"], "fmt": fmt}
        if track.get("kind"):
            params["kind"] = track["kind"]
        if track.get("name"):
            params["name"] = track["name"]
        for host in ["https://www.youtube.com/api/timedtext", "https://video.google.com/timedtext"]:
            try:
                resp = requests.get(f"{host}?{urlencode(params)}", headers=yt_headers(), timeout=TIMEOUT)
                if resp.status_code >= 400 or not resp.text.strip():
                    continue
                srt_text = normalize_caption_to_srt(resp.text, fmt)
                if srt_text.strip():
                    return srt_text, {
                        "source": "youtube_direct_timedtext" if track.get("kind") != "asr" else "youtube_direct_auto_caption",
                        "subtitle_source": "youtube_direct_timedtext" if track.get("kind") != "asr" else "youtube_direct_auto_caption",
                        "language": track["languageCode"],
                        "format": fmt,
                        "video_id": video_id,
                        "source_url": normalize_youtube_url(url),
                        "manual_languages": sorted({t["languageCode"] for t in tracks if t.get("kind") != "asr"}),
                        "auto_languages": sorted({t["languageCode"] for t in tracks if t.get("kind") == "asr"}),
                    }
            except Exception:
                continue
    return None


def get_transcript_api(url, requested_language=None):
    video_id = get_video_id(url)
    if not video_id:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        langs = [x for x in caption_lang_candidates(requested_language) if x not in {"*", "all", "auto", "und"}]
        transcript = None
        try:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id, languages=langs or ["en"])
        except Exception:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=langs or ["en"])
            except Exception:
                transcript = None
        if not transcript:
            return None
        blocks = []
        for item in transcript:
            # Supports dict and object styles across package versions.
            text = clean_caption_line(item.get("text", "") if isinstance(item, dict) else getattr(item, "text", ""))
            start = float(item.get("start", 0) if isinstance(item, dict) else getattr(item, "start", 0))
            duration = float(item.get("duration", 3) if isinstance(item, dict) else getattr(item, "duration", 3))
            if text:
                blocks.append(f"{len(blocks)+1}\n{srt_timestamp(start)} --> {srt_timestamp(start+max(duration,0.5))}\n{text}\n")
        srt_text = "\n".join(blocks).strip() + "\n" if blocks else ""
        if srt_text.strip():
            return srt_text, {
                "source": "youtube_transcript_api",
                "subtitle_source": "youtube_transcript_api",
                "language": requested_language or "auto",
                "format": "srt",
                "video_id": video_id,
                "source_url": normalize_youtube_url(url),
            }
    except Exception as exc:
        print(f"transcript api failed: {exc}", flush=True)
    return None


def get_caption_srt(url, requested_language=None):
    methods = [
        ("innertube_caption_tracks", get_innertube_captions),
        ("youtube_transcript_api", get_transcript_api),
        ("watch_page_caption_tracks", get_watch_page_captions),
        ("direct_timedtext", get_direct_timedtext),
    ]
    errors = []
    for name, fn in methods:
        try:
            result = fn(url, requested_language)
            if result and result[0].strip():
                srt_text, meta = result
                meta.setdefault("method", name)
                meta.setdefault("no_media_download", True)
                meta.setdefault("caption_first", True)
                return srt_text, meta, errors
            errors.append({"method": name, "error": "no caption text returned"})
        except Exception as exc:
            errors.append({"method": name, "error": str(exc)})
    return None, None, errors


@app.get("/")
def index():
    return jsonify({
        "ok": True,
        "success": True,
        "service": "caption-proxy",
        "version": APP_VERSION,
        "endpoints": ["POST /extract", "POST /caption", "POST /debug"],
        "public_captions_only": True,
        "no_media_download": True,
        "cookie_header_applied": bool(get_cookie_header()),
        "cookie_header_bytes": len(get_cookie_header() or ""),
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "success": True, "version": APP_VERSION})


@app.post("/extract")
@app.post("/caption")
def extract():
    try:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or ""
        language = payload.get("language") or "auto"
        if not url:
            return json_error("Missing url", 400)
        if not is_youtube_url(url):
            return json_error("Only YouTube URLs are supported by this caption proxy", 400)
        srt_text, meta, errors = get_caption_srt(url, language)
        if srt_text and srt_text.strip():
            return jsonify({
                "ok": True,
                "success": True,
                "srt_text": srt_text,
                "language": meta.get("language") or language,
                "format": "srt",
                "title": meta.get("title") or "YouTube captions",
                "video_id": meta.get("video_id") or get_video_id(url),
                "subtitle_source": meta.get("subtitle_source") or meta.get("source") or "caption_proxy",
                "source": meta.get("source") or "caption_proxy",
                "manual_languages": meta.get("manual_languages") or [],
                "auto_languages": meta.get("auto_languages") or [],
                "no_media_download": True,
                "caption_first": True,
                "version": APP_VERSION,
            })
        return json_error("No public captions returned from proxy host", 404, needs_upload=True, errors=errors)
    except Exception as exc:
        return json_error(str(exc), 500)


@app.post("/debug")
def debug():
    payload = request.get_json(silent=True) or {}
    url = payload.get("url") or ""
    language = payload.get("language") or "auto"
    normalized = normalize_youtube_url(url) if url else ""
    video_id = get_video_id(url) if url else None
    debug_data = {
        "ok": True,
        "success": True,
        "version": APP_VERSION,
        "url": url,
        "normalized_url": normalized,
        "video_id": video_id,
        "cookie_header_applied": bool(get_cookie_header()),
        "cookie_header_bytes": len(get_cookie_header() or ""),
        "methods": [],
    }
    for name, fn in [
        ("innertube_caption_tracks", get_innertube_captions),
        ("youtube_transcript_api", get_transcript_api),
        ("watch_page_caption_tracks", get_watch_page_captions),
        ("direct_timedtext", get_direct_timedtext),
    ]:
        item = {"method": name, "success": False}
        try:
            result = fn(normalized, language)
            if result and result[0].strip():
                srt_text, meta = result
                item.update({"success": True, "chars": len(srt_text), "sample": srt_text[:500], "meta": meta})
            else:
                item["error"] = "no caption text returned"
        except Exception as exc:
            item["error"] = str(exc)
        debug_data["methods"].append(item)
    debug_data["any_success"] = any(x.get("success") for x in debug_data["methods"])
    return jsonify(debug_data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
