#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask app to download HLS/DASH from API JSON (no DRM).
- Audio/subtitle language selection (prefer or all).
- HLS: uses master playlist if audio/subtitle groups exist; otherwise picks best variant.
- Auto filename: Serie-t<season>-e-<episode>-<title>.<ext>.
- Writes common TV/movie metadata tags into the output file when possible.
"""

import json
import os
import re
import shutil
import sqlite3
import queue
import threading
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, request, render_template, abort, jsonify
from urllib.parse import urljoin
import urllib.request
import urllib.error
import urllib.parse

app = Flask(__name__)
APP_ROOT = Path(__file__).resolve().parent
USERSCRIPT_TEMPLATE = APP_ROOT / 'tampermonkey_capture.user.js'

# Allow disabling best-variant selection by env (debug help)
DISABLE_VARIANT = os.getenv('DISABLE_VARIANT') == '1'
LOGS_DIR = Path('logs')
CAPTURE_STATE_FILE = LOGS_DIR / 'capture_state.json'
MEDIA_ROOT_SERIES = Path(os.getenv('MEDIA_ROOT_SERIES', os.getenv('MEDIA_ROOT', '/media/series')))
MEDIA_ROOT_MOVIES = Path(os.getenv('MEDIA_ROOT_MOVIES', os.getenv('MEDIA_ROOT', '/media/movies')))
MEDIA_ROOT_CRISTAO = Path(os.getenv('MEDIA_ROOT_CRISTAO', os.getenv('MEDIA_ROOT', '/media/cristaos')))
SAVE_TO_LIBRARY = os.getenv('SAVE_TO_LIBRARY', '1') != '0'
# Keep capture persistence only; download should happen explicitly from the UI button.
AUTO_DOWNLOAD_ON_CAPTURE = False

DOWNLOAD_QUEUE: "queue.Queue[str]" = queue.Queue()
DOWNLOAD_JOBS: dict[str, dict] = {}
DOWNLOAD_JOBS_LOCK = threading.Lock()
DOWNLOAD_WORKER_STARTED = False
DOWNLOAD_DB_FILE = LOGS_DIR / 'download_jobs.sqlite3'


# -------------------- Helpers --------------------

def resolve_ffmpeg_bin() -> Optional[str]:
    """Prefer env -> system ffmpeg -> imageio-ffmpeg (last resort)."""
    env_bin = os.getenv('FFMPEG_BIN')
    if env_bin:
        print(f'[debug] FFMPEG_BIN from env: {env_bin}')
        return env_bin
    sys_bin = shutil.which('ffmpeg')
    print(f'[debug] system ffmpeg: {sys_bin}')
    if sys_bin:
        return sys_bin
    try:
        import imageio_ffmpeg
        fallback = imageio_ffmpeg.get_ffmpeg_exe()
        print(f'[debug] imageio-ffmpeg path: {fallback}')
        return fallback
    except Exception as e:
        print(f'[debug] imageio-ffmpeg error: {e}')
        return None

FFMPEG_BIN = resolve_ffmpeg_bin()
print(f'[debug] FFMPEG_BIN resolved: {FFMPEG_BIN}')

FFPROBE_BIN = shutil.which('ffprobe')
if not FFPROBE_BIN and FFMPEG_BIN:
    candidate = str(Path(FFMPEG_BIN).with_name('ffprobe'))
    if os.path.exists(candidate):
        FFPROBE_BIN = candidate
print(f'[debug] FFPROBE_BIN resolved: {FFPROBE_BIN}')

def mask_value(val: Optional[str]) -> Optional[str]:
    """Mask sensitive values for debug output."""
    if not val: return val
    s = str(val)
    return ('*'*len(s)) if len(s)<=8 else s[:4]+'...'+s[-4:]

def slug(text: str) -> str:
    """Safe segment for filenames; replace spaces with '-'."""
    if not text: return ''
    text = text.replace('/', ' ').replace('\\', ' ')
    text = re.sub(r'[^A-Za-z0-9\s\.\-_]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.replace(' ', '-')

def build_manifest_url(resp: dict) -> str:
    """Compose absolute manifest URL from base_uri + manifest_uri when needed."""
    def first_non_empty_text(*keys: str) -> str:
        for key in keys:
            value = resp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ''

    mu = first_non_empty_text(
        'manifest_uri', 'manifestUrl', 'manifest_url',
        'playback_uri', 'playbackUri', 'playback_url', 'playbackUrl',
        'stream_uri', 'streamUri', 'stream_url', 'streamUrl',
        'hls_url', 'hlsUrl', 'url'
    )
    print(f'[debug] manifest candidate in JSON: {mu}')
    if mu.startswith('http://') or mu.startswith('https://'):
        return mu
    if mu:
        cdns = resp.get('cdns', {}).get('cdn', [])
        if cdns:
            chosen = next((c for c in cdns if c.get('priority',0)==0), cdns[0])
            base = (chosen.get('base_uri') or '').strip()
            if base:
                absolute = urljoin(base.rstrip('/')+'/', mu.lstrip('/'))
                print(f'[debug] combined manifest URL: {absolute}')
                return absolute
        return mu
    cdns = resp.get('cdns', {}).get('cdn', [])
    if not cdns:
        raise ValueError('No CDN info (response.cdns.cdn[])')
    chosen = next((c for c in cdns if c.get('priority',0)==0), cdns[0])
    base = (chosen.get('base_uri') or '').strip()
    if not base:
        raise ValueError('base_uri missing in CDN entry')
    absolute = urljoin(base.rstrip('/')+'/', mu.lstrip('/'))
    print(f'[debug] combined manifest URL: {absolute}')
    return absolute

def headers_list_to_dict(header_lines: List[str], ua: str) -> dict:
    """Turn ['Key: Value'] into headers dict (always set User-Agent)."""
    h = {'User-Agent': ua}
    for line in header_lines or []:
        if ':' in line:
            k,v = line.split(':',1); h[k.strip()] = v.strip()
    return h

def headers_list_to_ffmpeg_arg(header_lines: List[str], cookie: Optional[str]) -> Optional[str]:
    """Build CRLF-separated header string for ffmpeg -headers (must end with CRLF)."""
    lines = list(header_lines or [])
    if cookie: lines.append(f'Cookie: {cookie}')
    return '\r\n'.join(lines)+'\r\n' if lines else None

def fetch_text(url: str, headers: dict, timeout: int = 10) -> Optional[str]:
    """GET text (UTF-8)."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode('utf-8','ignore')
            print(f'[debug] fetched {len(txt)} chars from {url}')
            return txt
    except Exception as e:
        print(f'[debug] fetch_text error: {e}')
        return None

def parse_json_object(raw: str) -> Optional[dict]:
    """Parse a JSON object or a payload with a top-level `response` object."""
    raw = (raw or '').strip()
    if not raw:
        return None
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get('response'), dict):
        return data['response']
    return data if isinstance(data, dict) else None

def resolve_hls_best_variant(manifest_url: str, header_lines: List[str], ua: str) -> str:
    """If master, pick highest BANDWIDTH media playlist; else return manifest."""
    text = fetch_text(manifest_url, headers_list_to_dict(header_lines, ua), timeout=10)
    if not text or '#EXT-X-STREAM-INF' not in text: return manifest_url
    variants, last_bw = [], 0
    for line in text.splitlines():
        if line.startswith('#EXT-X-STREAM-INF'):
            m = re.search(r'BANDWIDTH=(\d+)', line); last_bw = int(m.group(1)) if m else 0
        elif line and not line.startswith('#'):
            variants.append((last_bw, urllib.parse.urljoin(manifest_url, line.strip())))
    variants.sort(key=lambda x:x[0], reverse=True)
    return variants[0][1] if variants else manifest_url

def has_hls_audio_group(manifest_url: str, header_lines: List[str], ua: str) -> bool:
    """True if master declares an external AUDIO group."""
    text = fetch_text(manifest_url, headers_list_to_dict(header_lines, ua), timeout=10)
    return bool(text and '#EXT-X-MEDIA:TYPE=AUDIO' in text)

def has_hls_subtitle_group(manifest_url: str, header_lines: List[str], ua: str) -> bool:
    """True if master declares an external SUBTITLES group."""
    text = fetch_text(manifest_url, headers_list_to_dict(header_lines, ua), timeout=10)
    return bool(text and '#EXT-X-MEDIA:TYPE=SUBTITLES' in text)

# ---- Auto filename helpers ----
def get_nested(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur

def first_text(resp: dict, candidates: List[object]) -> Optional[str]:
    """Return the first non-empty text found among the candidate keys/paths."""
    for candidate in candidates:
        value = get_nested(resp, *candidate) if isinstance(candidate, tuple) else resp.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None

def first_int(resp: dict, candidates: List[object]) -> Optional[int]:
    """Return the first integer-like value found among the candidate keys/paths."""
    for candidate in candidates:
        value = get_nested(resp, *candidate) if isinstance(candidate, tuple) else resp.get(candidate)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            value = value.strip()
            if value.isdigit():
                return int(value)
    return None

def extract_media_metadata(resp: dict) -> dict:
    """Best-effort normalization of show/movie metadata from the JSON payload."""
    series_title = first_text(resp, [
        'series', 'series_title', 'seriesTitle', 'show', 'show_title', 'showTitle',
        'program', 'program_title', 'programTitle', 'asset_series', 'collection',
        'collection_title', ('program', 'seriesTitle'), ('metadata', 'seriesTitle'),
        ('meta', 'series'), ('show', 'title')
    ])
    episode_title = first_text(resp, [
        'episode_title', 'episodeTitle', 'episode_name', 'episodeName', 'asset_title',
        'title', 'name', ('program', 'title'), ('metadata', 'title'), ('meta', 'title')
    ])
    season_number = first_int(resp, [
        'season', 'season_number', 'seasonNumber', 'seasonNum', 'seasonno',
        ('program', 'season'), ('metadata', 'season'), ('meta', 'season')
    ])
    episode_number = first_int(resp, [
        'episode', 'episode_number', 'episodeNumber', 'episodeNum', 'ep', 'number',
        ('program', 'episode'), ('metadata', 'episode'), ('meta', 'episode')
    ])
    year = first_int(resp, [
        'year', 'release_year', 'releaseYear', 'original_year',
        ('program', 'year'), ('metadata', 'year'), ('meta', 'year')
    ])
    description = first_text(resp, [
        'overview', 'plot', 'synopsis', 'description', 'summary',
        ('program', 'description'), ('metadata', 'description'), ('meta', 'description')
    ])
    tmdb_id = first_text(resp, [
        'tmdb_id', 'tmdbId', ('ids', 'tmdb'), ('external_ids', 'tmdb'),
        ('metadata', 'tmdb_id')
    ])
    imdb_id = first_text(resp, [
        'imdb_id', 'imdbId', ('ids', 'imdb'), ('external_ids', 'imdb'),
        ('metadata', 'imdb_id')
    ])
    tvdb_id = first_text(resp, [
        'tvdb_id', 'tvdbId', ('ids', 'tvdb'), ('external_ids', 'tvdb'),
        ('metadata', 'tvdb_id')
    ])
    return {
        'series_title': series_title,
        'episode_title': episode_title,
        'season_number': season_number,
        'episode_number': episode_number,
        'year': year,
        'description': description,
        'tmdb_id': tmdb_id,
        'imdb_id': imdb_id,
        'tvdb_id': tvdb_id,
    }

def extract_video_details_metadata(details: dict) -> dict:
    """Normalize the alternate video-details JSON into the same metadata shape."""
    if not isinstance(details, dict):
        return {}
    vods = details.get('Vods') or []
    first_vod = vods[0] if vods and isinstance(vods[0], dict) else {}
    catalog = first_vod.get('CatalogInfo') if isinstance(first_vod.get('CatalogInfo'), dict) else {}
    play_actions = first_vod.get('PlayActions') or []
    first_action = play_actions[0] if play_actions and isinstance(play_actions[0], dict) else {}
    video_profile = first_action.get('VideoProfile') if isinstance(first_action.get('VideoProfile'), dict) else {}

    series_title = first_text(details, ['SeriesTitle', 'SeriesName']) or first_text(catalog, ['SeriesTitle', 'SeriesName', 'Name'])
    episode_title = first_text(catalog, ['EpisodeName', 'EpisodeTitle', 'Name'])
    season_number = first_int(catalog, ['SeasonNumber', 'Season', 'SeasonNo'])
    episode_number = first_int(catalog, ['EpisodeNumber', 'Episode', 'EpisodeNo'])
    description = first_text(catalog, ['Description'])

    ratings = catalog.get('Ratings') if isinstance(catalog.get('Ratings'), list) else []
    tags = catalog.get('Tags') if isinstance(catalog.get('Tags'), list) else []
    supported_images = catalog.get('SupportedImages') if isinstance(catalog.get('SupportedImages'), list) else []
    audio_tags = video_profile.get('AudioTags') if isinstance(video_profile.get('AudioTags'), dict) else {}

    return {
        'series_title': series_title,
        'episode_title': episode_title,
        'season_number': season_number,
        'episode_number': episode_number,
        'description': description,
        'program_id': details.get('ProgramId'),
        'vod_id': first_vod.get('Id'),
        'show_type': catalog.get('ShowType'),
        'series_id': catalog.get('SeriesId'),
        'runtime_seconds': catalog.get('RuntimeSeconds'),
        'locale': catalog.get('Locale'),
        'is_adult': catalog.get('IsAdult'),
        'is_third_party_content': catalog.get('IsThirdPartyContent'),
        'has_content_advisory': catalog.get('HasContentAdvisory'),
        'available_utc': catalog.get('AvailableUtc'),
        'image_bucket_id': catalog.get('ImageBucketId'),
        'supported_images': supported_images,
        'ratings': ratings,
        'tags': tags,
        'playback_uri': first_text(video_profile, ['PlaybackUri']),
        'playback_origin': first_text(video_profile, ['PlaybackOrigin']),
        'video_profile_id': first_text(video_profile, ['Id']),
        'video_quality_level': first_text(video_profile, ['QualityLevel']),
        'video_client_type': first_text(video_profile, ['ClientType']),
        'video_encoding': first_text(video_profile, ['Encoding']),
        'audio_tags': audio_tags,
    }

def infer_series_metadata(resp: dict) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    """Heuristics: (series_title, season, episode, episode_title) from JSON."""
    meta = extract_media_metadata(resp)
    return (
        meta.get('series_title'),
        meta.get('season_number'),
        meta.get('episode_number'),
        meta.get('episode_title'),
    )

def build_auto_output_name(resp: dict, ext: str, overrides: dict, category: str) -> str:
    """
    Serie-t<season>-e-<episode>-<title>.<ext>
    If season/episode provided via form, they are enforced in the name.
    """
    o_series = (overrides.get('series_title') or '').strip()
    o_season = overrides.get('season_number')
    o_episode = overrides.get('episode_number')
    o_title = (overrides.get('episode_title') or '').strip()

    s_title, s_season, s_episode, s_ep_title = infer_series_metadata(resp)

    series = o_series or s_title or o_title or s_ep_title or 'video'
    category = (category or 'series').strip().lower()

    if category == 'series':
        season = int(o_season) if (o_season not in (None,'')) else (int(s_season) if s_season is not None else None)
        episode = int(o_episode) if (o_episode not in (None,'')) else (int(s_episode) if s_episode is not None else None)
        title = o_title or (s_ep_title or '')

        name = slug(series)
        if season is not None:  name += f'-t{season}'
        if episode is not None: name += f'-e-{episode}'
        if title:               name += f'-{slug(title)}'
    else:
        name = slug(series)
        if o_title and slug(o_title) != slug(series):
            name += f'-{slug(o_title)}'
    return f'{name}.{ext}'

def build_ffmpeg_metadata_args(resp: dict, overrides: dict, final_name: str, extra_details: Optional[dict] = None) -> List[str]:
    """
    Build global metadata tags that are useful for Jellyfin/Sonarr/Radarr-style
    workflows without making the download flow more complex.
    """
    meta = extract_media_metadata(resp)
    normalize_tag = lambda value: re.sub(r'\s+', ' ', str(value)).strip()
    series_title = normalize_tag(overrides.get('series_title') or meta.get('series_title') or '')
    episode_title = normalize_tag(overrides.get('episode_title') or meta.get('episode_title') or '')
    season_number = overrides.get('season_number')
    episode_number = overrides.get('episode_number')
    if season_number in (None, ''):
        season_number = meta.get('season_number')
    else:
        season_number = int(season_number)
    if episode_number in (None, ''):
        episode_number = meta.get('episode_number')
    else:
        episode_number = int(episode_number)

    tags: dict[str, str] = {}
    title_tag = episode_title or series_title or Path(final_name).stem
    if title_tag:
        tags['title'] = title_tag
    if series_title:
        tags['show'] = series_title
    if season_number is not None:
        tags['season_number'] = str(season_number)
    if episode_number is not None:
        tags['episode_id'] = str(episode_number)
        tags['episode_sort'] = str(episode_number)
    if meta.get('year') is not None:
        tags['date'] = str(meta['year'])

    comment_bits = []
    if meta.get('description'):
        comment_bits.append(normalize_tag(meta['description']))
    if meta.get('tmdb_id'):
        comment_bits.append(f"tmdb={normalize_tag(meta['tmdb_id'])}")
    if meta.get('imdb_id'):
        comment_bits.append(f"imdb={normalize_tag(meta['imdb_id'])}")
    if meta.get('tvdb_id'):
        comment_bits.append(f"tvdb={normalize_tag(meta['tvdb_id'])}")
    if extra_details:
        try:
            comment_bits.append(json.dumps(extra_details, ensure_ascii=False, separators=(',', ':')))
        except Exception:
            pass
    if comment_bits:
        tags['comment'] = ' | '.join(comment_bits)

    args = ['-map_metadata', '-1']
    for key, value in tags.items():
        args += ['-metadata', f'{key}={value}']
    return args

def resolve_media_root(category: str) -> Path:
    """Pick the base library path for the selected content category."""
    category = (category or 'series').strip().lower()
    if category == 'movies':
        return MEDIA_ROOT_MOVIES
    if category == 'christao':
        return MEDIA_ROOT_CRISTAO
    return MEDIA_ROOT_SERIES

def build_library_output_path(resp: dict, overrides: dict, final_name: str, category: str) -> Path:
    """Build a Sonarr-style path for series or a flat library path for other categories."""
    meta = extract_media_metadata(resp)
    series_title = (overrides.get('series_title') or meta.get('series_title') or 'video').strip()
    series_dir = slug(series_title) or 'video'
    root = resolve_media_root(category)
    if (category or 'series').strip().lower() == 'series':
        season_value = overrides.get('season_number')
        if season_value in (None, ''):
            season_value = meta.get('season_number')
        if season_value in (None, ''):
            season_num = 0
        else:
            season_num = int(season_value)
        return root / series_dir / f'Season {season_num:02d}' / final_name
    return root / series_dir / final_name


def ensure_logs_dir() -> Path:
    """Create the logs directory used by FFmpeg reports."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR

def ensure_download_jobs_db() -> None:
    """Create the SQLite table used to persist queued jobs."""
    ensure_logs_dir()
    with sqlite3.connect(DOWNLOAD_DB_FILE) as conn:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS download_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                form_data TEXT NOT NULL,
                result TEXT,
                error TEXT,
                progress TEXT
            )
            '''
        )
        conn.commit()

def db_row_to_job(row: sqlite3.Row | None) -> Optional[dict]:
    """Convert a SQLite row into the in-memory job shape."""
    if row is None:
        return None
    try:
        form_data = json.loads(row['form_data']) if row['form_data'] else {}
    except Exception:
        form_data = {}
    try:
        result = json.loads(row['result']) if row['result'] else None
    except Exception:
        result = None
    try:
        progress = json.loads(row['progress']) if row['progress'] else {}
    except Exception:
        progress = {}
    return {
        'job_id': row['job_id'],
        'status': row['status'],
        'created_at': row['created_at'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
        'form_data': form_data,
        'result': result,
        'error': row['error'],
        'progress': progress,
    }

def db_get_job(job_id: str) -> Optional[dict]:
    """Load a job from SQLite if it is no longer in memory."""
    ensure_download_jobs_db()
    with sqlite3.connect(DOWNLOAD_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM download_jobs WHERE job_id = ?', (job_id,)).fetchone()
    return db_row_to_job(row)

def db_upsert_job(job: dict) -> None:
    """Persist the current job snapshot to SQLite."""
    if not job:
        return
    ensure_download_jobs_db()
    payload = (
        job.get('job_id'),
        job.get('status'),
        job.get('created_at'),
        job.get('started_at'),
        job.get('finished_at'),
        json.dumps(job.get('form_data') or {}, ensure_ascii=False),
        json.dumps(job.get('result'), ensure_ascii=False) if job.get('result') is not None else None,
        job.get('error'),
        json.dumps(job.get('progress') or {}, ensure_ascii=False),
    )
    with sqlite3.connect(DOWNLOAD_DB_FILE) as conn:
        conn.execute(
            '''
            INSERT INTO download_jobs (
                job_id, status, created_at, started_at, finished_at,
                form_data, result, error, progress
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                form_data=excluded.form_data,
                result=excluded.result,
                error=excluded.error,
                progress=excluded.progress
            ''',
            payload,
        )
        conn.commit()

def load_capture_state() -> dict:
    """Load the persisted capture state."""
    if not CAPTURE_STATE_FILE.exists():
        return {'latest': None, 'manifest': None, 'details': None, 'expected_key': '', 'auto_downloaded_for': ''}
    try:
        data = json.loads(CAPTURE_STATE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {'latest': None, 'manifest': None, 'details': None, 'expected_key': '', 'auto_downloaded_for': ''}
        data.setdefault('latest', None)
        data.setdefault('manifest', None)
        data.setdefault('details', None)
        data.setdefault('expected_key', '')
        data.setdefault('auto_downloaded_for', '')
        return data
    except Exception as e:
        print(f'[debug] failed to read capture state: {e}')
        return {'latest': None, 'manifest': None, 'details': None, 'expected_key': '', 'auto_downloaded_for': ''}

def write_capture_state(state: dict) -> None:
    """Persist the capture state."""
    ensure_logs_dir()
    CAPTURE_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

def normalize_capture_type(value: str) -> str:
    """Map capture labels from the userscript to the backend storage model."""
    normalized = (value or '').strip().lower()
    if normalized in ('roll', 'manifest', 'playback', 'play-options-manifest'):
        return 'manifest'
    if normalized in ('details', 'play-options', 'video-details'):
        return 'details'
    return normalized or 'generic'

def derive_capture_key_from_payload(payload: object) -> str:
    """Derive a stable content key from manifest/play-options payloads."""
    if not isinstance(payload, dict):
        return ''
    root = payload.get('response') if isinstance(payload.get('response'), dict) else payload
    if not isinstance(root, dict):
        return ''

    manifest_uri = root.get('manifest_uri')
    if isinstance(manifest_uri, str) and manifest_uri.strip():
      base = manifest_uri.strip().split('?', 1)[0].split('#', 1)[0]
      base = base.rsplit('/', 1)[0]
      m = re.search(r'^(?:mno|minno)_(.+?)(?:_(?:Jitp|JITP)_[^/]+)?$', base, re.IGNORECASE)
      if m:
          return m.group(1)
      return base

    program_id = root.get('ProgramId')
    if isinstance(program_id, str) and program_id.strip():
        m = re.search(r'^(?:mno|minno)_(.+?)_minno_episode$', program_id.strip(), re.IGNORECASE)
        if m:
            return m.group(1)
        return program_id.strip()

    vods = root.get('Vods')
    if isinstance(vods, list) and vods:
        first_vod = vods[0] if isinstance(vods[0], dict) else {}
        catalog = first_vod.get('CatalogInfo') if isinstance(first_vod.get('CatalogInfo'), dict) else {}
        for key in ('SeriesId', 'EpisodeId', 'ProgramId'):
            value = catalog.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        vod_id = first_vod.get('Id')
        if isinstance(vod_id, str) and vod_id.strip():
            return vod_id.strip()

    return ''

def store_capture_payload(
    payload: object,
    source_url: str = '',
    capture_type: str = 'generic',
    capture_session: str = '',
    capture_key: str = '',
) -> dict:
    """Persist the latest captured JSON payload for the UI to poll."""
    ensure_logs_dir()
    record = {
        'capture_id': int(time.time() * 1000),
        'received_at': time.time(),
        'source_url': source_url,
        'capture_type': capture_type,
        'capture_session': capture_session,
        'capture_key': capture_key,
        'payload': payload,
    }
    state = load_capture_state()
    state['latest'] = record
    if capture_type in ('manifest', 'details'):
        state[capture_type] = record
    write_capture_state(state)
    return record

def combine_capture_bundle_key(manifest_key: str, details_key: str) -> str:
    """Build a stable key for a manifest/details pair."""
    manifest_key = str(manifest_key or '').strip().lower()
    details_key = str(details_key or '').strip().lower()
    return f'{manifest_key}::{details_key}' if manifest_key and details_key else ''

def load_capture_payload() -> Optional[dict]:
    """Load the latest captured JSON payload, if present."""
    return load_capture_state().get('latest')

def looks_like_manifest_payload(payload: object) -> bool:
    """Heuristic for a JSON payload that can drive /download directly."""
    if not isinstance(payload, dict):
        return False
    root = payload.get('response') if isinstance(payload.get('response'), dict) else payload
    if not isinstance(root, dict):
        return False
    if isinstance(root.get('cdns'), dict) and root.get('cdns', {}).get('cdn'):
        return True
    for key in (
        'manifest_uri', 'manifestUrl', 'manifest_url',
        'playback_uri', 'playbackUri', 'playback_url', 'playbackUrl',
        'stream_uri', 'streamUri', 'stream_url', 'streamUrl',
        'hls_url', 'hlsUrl', 'url'
    ):
        value = root.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False

def trigger_download_from_capture(json_input: str, details_json: str = ''):
    """Run the existing download flow from a synthetic POST context."""
    form_data = {
        'json_input': json_input,
        'details_json': details_json,
        'output': '',
        'category': 'series',
        'container': 'mp4',
        'series_title': '',
        'episode_title': '',
        'season_number': '',
        'episode_number': '',
        'user_agent': 'Mozilla/5.0',
        'referer': '',
        'origin': '',
        'cookie': '',
        'extra_headers': '',
        'force_aac': '',
        'audio_mode': 'prefer',
        'audio_pref': '',
        'subs_mode': 'none',
        'subs_pref': '',
    }
    with app.test_request_context('/download', method='POST', data=form_data):
        return download()

def format_permission_hint(path: Path) -> str:
    """Return a clear hint for bind-mount permission failures."""
    return (
        f'Sem permissão para escrever em {path}. '
        'Ajuste a permissão do diretório no host para o UID/GID usado pelo container '
        '(neste build, 1000:1000) ou monte o volume com permissões de escrita.'
    )

def normalize_download_form(form_data: dict) -> dict:
    """Normalize raw form values into a stable string-based payload."""
    keys = [
        'json_input', 'details_json', 'output', 'category', 'container',
        'series_title', 'episode_title', 'season_number', 'episode_number',
        'user_agent', 'referer', 'origin', 'cookie', 'extra_headers',
        'force_aac', 'audio_mode', 'audio_pref', 'subs_mode', 'subs_pref',
    ]
    normalized = {}
    for key in keys:
      value = form_data.get(key, '')
      if value is None:
          value = ''
      normalized[key] = str(value)
    return normalized

def update_download_job(job_id: str, **patch) -> None:
    """Merge a small state patch into a queued/running job."""
    if not job_id:
        return
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return
        job.update(patch)
        db_upsert_job(job)

def set_download_job_progress(job_id: str, *, percent=None, out_time_ms=None, speed=None, fps=None, frame=None, eta=None, text=None) -> None:
    """Store a normalized progress snapshot for the job."""
    progress = {
        'percent': percent,
        'out_time_ms': out_time_ms,
        'speed': speed,
        'fps': fps,
        'frame': frame,
        'eta': eta,
        'text': text,
        'updated_at': time.time(),
    }
    update_download_job(job_id, progress=progress)

def parse_ffmpeg_progress_line(line: str) -> tuple[str, str]:
    if '=' not in line:
        return '', ''
    key, value = line.split('=', 1)
    return key.strip(), value.strip()

def probe_media_duration_seconds(input_url: str, headers_arg: Optional[str], user_agent: str) -> Optional[float]:
    """Best-effort duration probe for percent progress."""
    if not FFPROBE_BIN:
        return None
    cmd = [
        FFPROBE_BIN,
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        '-user_agent', user_agent,
    ]
    if headers_arg:
        cmd += ['-headers', headers_arg]
    cmd += [input_url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        raw = (out.stdout or '').strip()
        if not raw or raw in ('N/A', '0', '0.0'):
            return None
        value = float(raw)
        return value if value > 0 else None
    except Exception as e:
        print(f'[debug] ffprobe duration error: {e}')
        return None

def perform_download(form_data: dict, job_id: str = '') -> dict:
    """Execute the existing download flow and return a structured result."""
    if not FFMPEG_BIN:
        raise RuntimeError('ffmpeg not found. Install it or add to PATH.')

    form = normalize_download_form(form_data or {})
    print('-----[download job] start-----')

    # --- Form fields ---
    json_input = form.get('json_input', '').strip()
    details_json = form.get('details_json', '').strip()

    # naming & container
    output_name   = (form.get('output', '') or '').strip()  # blank => auto
    category      = (form.get('category', 'series') or 'series').strip().lower()
    if category not in ('series', 'movies', 'christao'):
        category = 'series'
    container     = (form.get('container', 'mp4') or 'mp4').lower()
    if container not in ('mp4', 'mkv'):
        container = 'mp4'
    series_title  = form.get('series_title', '').strip()
    episode_title = form.get('episode_title', '').strip()
    season_number = form.get('season_number', '').strip()
    episode_number = form.get('episode_number', '').strip()

    # headers/options
    user_agent = form.get('user_agent', 'Mozilla/5.0').strip() or 'Mozilla/5.0'
    referer = form.get('referer', '').strip()
    origin  = form.get('origin', '').strip()
    cookie  = form.get('cookie', '').strip()
    extra_headers = form.get('extra_headers', '').splitlines()
    force_aac = form.get('force_aac') == 'on'

    # language modes
    audio_mode = form.get('audio_mode', 'prefer')  # default|prefer|all
    audio_pref = form.get('audio_pref', '').strip().lower()
    subs_mode  = form.get('subs_mode', 'none')     # none|prefer|all
    subs_pref  = form.get('subs_pref', '').strip().lower()

    # build header lines
    header_lines: List[str] = []
    if referer:
        header_lines.append(f'Referer: {referer}')
    if origin:
        header_lines.append(f'Origin: {origin}')
    for line in extra_headers:
        line = line.strip()
        if line and ':' in line:
            header_lines.append(line)
    headers_arg = headers_list_to_ffmpeg_arg(header_lines, cookie=cookie)

    # parse JSON
    try:
        resp = parse_json_object(json_input) or {}
    except Exception as e:
        raise ValueError(f'Invalid JSON: {e}')

    details_resp = {}
    if details_json:
        try:
            details_resp = parse_json_object(details_json) or {}
        except Exception as e:
            raise ValueError(f'Invalid details JSON: {e}')

    drm = (resp.get('drm_type') or '').lower()
    pkg = (resp.get('package_type') or '').lower()
    if drm and drm != 'none':
        raise ValueError(f'Content protected with DRM "{drm}". Cannot download.')

    details_meta = extract_video_details_metadata(details_resp) if details_resp else {}
    merged_resp = dict(details_meta)

    # manifest & input url (consider audio/subtitle groups)
    manifest = build_manifest_url(resp)
    headers_for_fetch = header_lines + ([f'Cookie: {cookie}'] if cookie else [])
    input_url = manifest
    if pkg == 'hls':
        use_master_audio = has_hls_audio_group(manifest, headers_for_fetch, user_agent)
        use_master_subs  = (subs_mode != 'none') and has_hls_subtitle_group(manifest, headers_for_fetch, user_agent)
        use_master = use_master_audio or use_master_subs
        if not DISABLE_VARIANT and not use_master:
            input_url = resolve_hls_best_variant(manifest, headers_for_fetch, user_agent)
            print(f'[debug] media playlist chosen: {input_url}')
        else:
            print('[debug] using master playlist as input (audio/sub groups present or forced)')
            input_url = manifest

    # output file name (server-side auto if empty)
    overrides = {
        'series_title': series_title,
        'episode_title': episode_title,
        'season_number': season_number,
        'episode_number': episode_number,
    }
    final_name = output_name or build_auto_output_name(merged_resp, container, overrides, category)
    if SAVE_TO_LIBRARY:
        outfile = build_library_output_path(merged_resp, overrides, final_name, category)
    else:
        tmpdir = Path(tempfile.mkdtemp(prefix='flask_hls_'))
        outfile = tmpdir / final_name
    try:
        outfile.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise PermissionError(format_permission_hint(outfile.parent))
    if not os.access(outfile.parent, os.W_OK):
        raise PermissionError(format_permission_hint(outfile.parent))
    print(f'[debug] output path: {outfile}')

    # --- ffmpeg command ---
    cmd = [
        FFMPEG_BIN, '-y',
        '-nostdin',
        '-protocol_whitelist', 'file,http,https,tcp,tls,crypto,concat',
        '-reconnect', '1', '-reconnect_streamed', '1',
        '-reconnect_at_eof', '1', '-reconnect_delay_max', '2',
        '-user_agent', user_agent,
    ]
    if os.getenv('DEBUG_FFMPEG') == '1':
        ensure_logs_dir()
        cmd[1:1] = ['-loglevel', 'debug', '-report']
    else:
        cmd[1:1] = ['-loglevel', 'warning', '-stats']

    if headers_arg:
        cmd += ['-headers', headers_arg]

    # input
    cmd += ['-i', input_url]

    # mapping
    def split_langs(s: str) -> List[str]:
        return [x.strip().lower() for x in s.split(',') if x.strip()]

    maps: List[str] = []
    maps += ['-map', '0:v:0']  # first video

    # audio
    if audio_mode == 'all':
        maps += ['-map', '0:a?']
    elif audio_mode == 'prefer' and audio_pref:
        for lang in split_langs(audio_pref):
            maps += ['-map', f'0:a:m:language:{lang}?']
        maps += ['-map', '0:a:0?']  # fallback
    else:
        maps += ['-map', '0:a:0?']  # default

    # subtitles
    if subs_mode == 'all':
        maps += ['-map', '0:s?']
    elif subs_mode == 'prefer' and subs_pref:
        for lang in split_langs(subs_pref):
            maps += ['-map', f'0:s:m:language:{lang}?']
        maps += ['-map', '0:s:0?']  # fallback
    else:
        maps += ['-sn']  # disable subs

    cmd += maps
    cmd += build_ffmpeg_metadata_args(merged_resp, overrides, final_name, extra_details=details_meta if details_meta else None)

    # codecs
    if force_aac:
        cmd += ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k']
    else:
        cmd += ['-c', 'copy']
        if pkg == 'hls':
            cmd += ['-bsf:a', 'aac_adtstoasc']

    # subtitle codec for MP4
    if (subs_mode != 'none') and (container == 'mp4'):
        cmd += ['-c:s', 'mov_text']

    if container == 'mp4':
        cmd += ['-movflags', '+faststart']

    # ✅ output/muxer option must be before the output file
    cmd += ['-max_muxing_queue_size', '2048']
    cmd += [str(outfile)]

    # run
    duration_seconds = probe_media_duration_seconds(input_url, headers_arg, user_agent)
    if duration_seconds:
        print(f'[debug] probed media duration: {duration_seconds:.2f}s')
    if job_id:
        set_download_job_progress(job_id, percent=0, text='Iniciando processamento...')
    printable_cmd = ' '.join('Cookie: ****' if (isinstance(p, str) and 'Cookie:' in p) else str(p) for p in cmd)
    print('[debug] ffmpeg cmd:', printable_cmd)

    env = os.environ.copy()
    env['FFREPORT'] = 'file=logs/ffmpeg-%p.log:level=32'
    proc = subprocess.Popen(
        cmd + ['-progress', 'pipe:1', '-nostats'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(LOGS_DIR) if os.getenv('DEBUG_FFMPEG') == '1' else None,
        env=env,
        bufsize=1,
    )

    stderr_lines: List[str] = []
    progress_state = {'frame': None, 'fps': None, 'speed': None, 'out_time_ms': None, 'percent': None}
    try:
        while True:
            line = proc.stdout.readline() if proc.stdout else ''
            if line == '' and proc.poll() is not None:
                break
            if not line:
                continue
            key, value = parse_ffmpeg_progress_line(line.strip())
            if not key:
                continue
            if key == 'frame':
                progress_state['frame'] = value
            elif key == 'fps':
                progress_state['fps'] = value
            elif key == 'speed':
                progress_state['speed'] = value
            elif key == 'out_time_ms':
                try:
                    progress_state['out_time_ms'] = int(value)
                except Exception:
                    progress_state['out_time_ms'] = None
                if duration_seconds and progress_state['out_time_ms'] is not None:
                    percent = min(100.0, max(0.0, (progress_state['out_time_ms'] / 1000000.0) / duration_seconds * 100.0))
                    progress_state['percent'] = round(percent, 1)
                if job_id:
                    set_download_job_progress(
                        job_id,
                        percent=progress_state['percent'],
                        out_time_ms=progress_state['out_time_ms'],
                        speed=progress_state['speed'],
                        fps=progress_state['fps'],
                        frame=progress_state['frame'],
                        text='Processando download...',
                    )
            elif key == 'progress' and value == 'end':
                if job_id:
                    set_download_job_progress(job_id, percent=100.0, text='Finalizando...')
                break

        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            stderr_lines = proc.stderr.read().splitlines()
    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass

    proc.wait()
    print(f'[debug] ffmpeg returncode={proc.returncode}')
    if stderr_lines:
        for line in stderr_lines[-20:]:
            print('  ', line)

    if proc.returncode != 0 or not outfile.exists() or outfile.stat().st_size == 0:
        tail = stderr_lines[-20:] if stderr_lines else []
        raise RuntimeError('ffmpeg failed.\n' + '\n'.join(tail))

    return {
        'path': str(outfile),
        'name': final_name,
        'category': category,
        'media_root': str(resolve_media_root(category)),
    }

def ensure_download_worker() -> None:
    """Start the single download worker once per process."""
    global DOWNLOAD_WORKER_STARTED
    if DOWNLOAD_WORKER_STARTED:
        return
    with DOWNLOAD_JOBS_LOCK:
        if DOWNLOAD_WORKER_STARTED:
            return
        thread = threading.Thread(target=download_worker_loop, name='download-worker', daemon=True)
        thread.start()
        DOWNLOAD_WORKER_STARTED = True

def create_download_job(form_data: dict) -> dict:
    """Create a queued download job and store its initial state."""
    ensure_download_worker()
    ensure_download_jobs_db()
    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        'job_id': job_id,
        'status': 'queued',
        'created_at': now,
        'started_at': None,
        'finished_at': None,
        'form_data': normalize_download_form(form_data or {}),
        'result': None,
        'error': None,
        'progress': {},
    }
    with DOWNLOAD_JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = job
        db_upsert_job(job)
    DOWNLOAD_QUEUE.put(job_id)
    return job

def download_worker_loop() -> None:
    """Process queued downloads sequentially."""
    while True:
        job_id = DOWNLOAD_QUEUE.get()
        try:
            with DOWNLOAD_JOBS_LOCK:
                job = DOWNLOAD_JOBS.get(job_id)
                if not job:
                    continue
                job['status'] = 'running'
                job['started_at'] = time.time()
                job['error'] = None
                db_upsert_job(job)
            try:
                result = perform_download(job['form_data'], job_id=job_id)
                with DOWNLOAD_JOBS_LOCK:
                    job = DOWNLOAD_JOBS.get(job_id)
                    if job:
                        job['status'] = 'done'
                        job['finished_at'] = time.time()
                        job['result'] = result
                        db_upsert_job(job)
            except Exception as e:
                with DOWNLOAD_JOBS_LOCK:
                    job = DOWNLOAD_JOBS.get(job_id)
                    if job:
                        job['status'] = 'error'
                        job['finished_at'] = time.time()
                        job['error'] = str(e)
                        db_upsert_job(job)
        finally:
            DOWNLOAD_QUEUE.task_done()

def serialize_download_job(job: dict) -> dict:
    """Return a compact public view of a queued job."""
    if not job:
        return {}
    with DOWNLOAD_JOBS_LOCK:
        current = dict(job)
    return {
        'job_id': current.get('job_id'),
        'status': current.get('status'),
        'created_at': current.get('created_at'),
        'started_at': current.get('started_at'),
        'finished_at': current.get('finished_at'),
        'progress': current.get('progress') or {},
        'result': current.get('result'),
        'error': current.get('error'),
        'queue_size': DOWNLOAD_QUEUE.qsize(),
    }

def count_download_jobs() -> dict:
    """Return coarse queue counts for UI display."""
    with DOWNLOAD_JOBS_LOCK:
        jobs = {job.get('job_id'): dict(job) for job in DOWNLOAD_JOBS.values() if job}
    ensure_download_jobs_db()
    with sqlite3.connect(DOWNLOAD_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute('SELECT * FROM download_jobs'):
            job = db_row_to_job(row)
            if job:
                jobs[job['job_id']] = job
    jobs_list = list(jobs.values())
    queued = sum(1 for job in jobs_list if job.get('status') == 'queued')
    running = sum(1 for job in jobs_list if job.get('status') == 'running')
    finished = sum(1 for job in jobs_list if job.get('status') in ('done', 'error'))
    return {
        'queued': queued,
        'running': running,
        'finished': finished,
        'active_or_pending': queued + running,
        'total': len(jobs_list),
    }


# -------------------- Routes --------------------

@app.get('/')
def index():
    return render_template('index.html')

@app.get('/health')
def health():
    if not FFMPEG_BIN:
        return {'ffmpeg': None, 'version': None}, 500
    try:
        out = subprocess.run([FFMPEG_BIN, '-version'], capture_output=True, text=True, timeout=5)
        first = out.stdout.splitlines()[0] if out.stdout else ''
        return {'ffmpeg': FFMPEG_BIN, 'version': first}, 200
    except Exception as e:
        return {'ffmpeg': FFMPEG_BIN, 'error': str(e)}, 500

@app.errorhandler(400)
def handle_bad_request(err):
    return jsonify({'ok': False, 'error': getattr(err, 'description', str(err))}), 400

@app.errorhandler(500)
def handle_server_error(err):
    return jsonify({'ok': False, 'error': getattr(err, 'description', str(err))}), 500

@app.get('/tampermonkey_capture.user.js')
def download_userscript():
    if not USERSCRIPT_TEMPLATE.exists():
        abort(404, 'Userscript template not found.')
    server_origin = request.url_root.rstrip('/')
    connect_host = request.host.split(':', 1)[0]
    script = USERSCRIPT_TEMPLATE.read_text(encoding='utf-8')
    script = script.replace('// @match        https://kids.gominno.com/*', '// @match        https://kids.gominno.com/*')
    script = script.replace('__CAPTURE_ENDPOINT__', f'{server_origin}/api/capture')
    script = script.replace('__CAPTURE_CONNECT__', connect_host)
    response = app.response_class(script, mimetype='application/javascript; charset=utf-8')
    response.headers['Content-Disposition'] = 'attachment; filename=tampermonkey_capture.user.js'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.get('/api/capture/latest')
def capture_latest():
    state = load_capture_state()
    record = state.get('latest')
    manifest_record = state.get('manifest')
    details_record = state.get('details')
    if not record and not manifest_record and not details_record:
        return jsonify({'ok': True, 'has_capture': False, 'expected_key': state.get('expected_key', '')}), 200
    return jsonify({
        'ok': True,
        'has_capture': True,
        'capture_id': record.get('capture_id') if record else None,
        'received_at': record.get('received_at') if record else None,
        'source_url': record.get('source_url', '') if record else '',
        'capture_type': record.get('capture_type', 'generic') if record else 'generic',
        'capture_session': record.get('capture_session', '') if record else '',
        'capture_key': record.get('capture_key', '') if record else '',
        'payload': record.get('payload') if record else None,
        'manifest_ready': bool(manifest_record),
        'details_ready': bool(details_record),
        'expected_key': state.get('expected_key', ''),
        'payload_text': json.dumps(record.get('payload'), ensure_ascii=False, indent=2) if record else '',
        'manifest_capture': {
            'capture_id': manifest_record.get('capture_id'),
            'received_at': manifest_record.get('received_at'),
            'source_url': manifest_record.get('source_url', ''),
            'capture_type': manifest_record.get('capture_type', 'manifest'),
            'capture_session': manifest_record.get('capture_session', ''),
            'capture_key': manifest_record.get('capture_key', ''),
            'payload': manifest_record.get('payload'),
            'payload_text': json.dumps(manifest_record.get('payload'), ensure_ascii=False, indent=2),
        } if manifest_record else None,
        'details_capture': {
            'capture_id': details_record.get('capture_id'),
            'received_at': details_record.get('received_at'),
            'source_url': details_record.get('source_url', ''),
            'capture_type': details_record.get('capture_type', 'details'),
            'capture_session': details_record.get('capture_session', ''),
            'capture_key': details_record.get('capture_key', ''),
            'payload': details_record.get('payload'),
            'payload_text': json.dumps(details_record.get('payload'), ensure_ascii=False, indent=2),
        } if details_record else None,
    }), 200

@app.post('/api/capture/expected-key')
def capture_expected_key():
    incoming = request.get_json(silent=True) or {}
    expected_key = ''
    if isinstance(incoming, dict):
        candidate = incoming.get('expected_key')
        if isinstance(candidate, str):
            expected_key = candidate.strip()
    if not expected_key:
        expected_key = request.form.get('expected_key', '').strip()
    state = load_capture_state()
    state['expected_key'] = expected_key
    write_capture_state(state)
    return jsonify({'ok': True, 'expected_key': expected_key}), 200

@app.post('/api/capture')
def capture_store():
    source_url = request.headers.get('X-Capture-Source-Url', '').strip()
    capture_session = request.headers.get('X-Capture-Session', '').strip()
    capture_key = request.headers.get('X-Capture-Key', '').strip()
    incoming = request.get_json(silent=True)
    capture_type = normalize_capture_type(request.headers.get('X-Capture-Type', '').strip() or 'generic')
    payload = incoming
    if payload is None:
        raw = request.form.get('payload', '').strip() or request.form.get('json', '').strip() or request.data.decode('utf-8', 'ignore').strip()
        if not raw:
            abort(400, 'Missing capture payload.')
        try:
            payload = json.loads(raw)
        except Exception as e:
            abort(400, f'Invalid capture JSON: {e}')
    if not isinstance(payload, (dict, list)):
        abort(400, 'Capture payload must be a JSON object or array.')

    if isinstance(payload, dict):
        incoming_capture_type = payload.get('capture_type')
        if isinstance(incoming_capture_type, str) and incoming_capture_type.strip():
            capture_type = normalize_capture_type(incoming_capture_type)
        incoming_capture_session = payload.get('capture_session')
        if isinstance(incoming_capture_session, str) and incoming_capture_session.strip():
            capture_session = incoming_capture_session.strip()
        incoming_capture_key = payload.get('capture_key')
        if isinstance(incoming_capture_key, str) and incoming_capture_key.strip():
            capture_key = incoming_capture_key.strip()
        else:
            capture_key = derive_capture_key_from_payload(payload.get('payload') if isinstance(payload.get('payload'), (dict, list)) else payload)
        if 'payload' in payload and isinstance(payload.get('payload'), (dict, list)):
            source_url = str(payload.get('source_url') or source_url).strip()
            if not capture_session:
                nested_capture_session = payload.get('capture_session')
                if isinstance(nested_capture_session, str) and nested_capture_session.strip():
                    capture_session = nested_capture_session.strip()
            payload = payload['payload']
    else:
        capture_key = derive_capture_key_from_payload(payload)

    expected_key = str(load_capture_state().get('expected_key', '') or '').strip()
    if expected_key:
        normalized_expected = expected_key.lower()
        normalized_capture = str(capture_key or '').strip().lower()
        if normalized_capture and normalized_capture != normalized_expected:
            print(f'[debug] ignoring capture for mismatched key: got={capture_key} expected={expected_key}')
            return jsonify({
                'ok': True,
                'ignored': True,
                'reason': 'capture_key_mismatch',
                'expected_key': expected_key,
                'capture_key': capture_key,
                'capture_type': capture_type,
                'source_url': source_url,
            }), 200

    record = store_capture_payload(
        payload,
        source_url=source_url,
        capture_type=capture_type,
        capture_session=capture_session,
        capture_key=capture_key,
    )
    state = load_capture_state()
    manifest_record = state.get('manifest')
    details_record = state.get('details')
    return jsonify({
        'ok': True,
        'capture_id': record['capture_id'],
        'received_at': record['received_at'],
        'source_url': source_url,
        'capture_type': capture_type,
        'capture_session': record.get('capture_session', ''),
        'capture_key': record.get('capture_key', ''),
        'manifest_ready': bool(manifest_record),
        'details_ready': bool(details_record),
    }), 200

@app.post('/download')
def download():
    job = create_download_job(request.form.to_dict(flat=True))
    counts = count_download_jobs()
    return jsonify({
        'ok': True,
        'queued': True,
        'job_id': job['job_id'],
        'status': job['status'],
        'queue_size': counts['active_or_pending'],
        'queued_count': counts['queued'],
        'running_count': counts['running'],
        'total_jobs': counts['total'],
    }), 202

@app.get('/api/download-jobs/<job_id>')
def download_job_status(job_id: str):
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        snapshot = dict(job) if job else None
    if not snapshot:
        job = db_get_job(job_id)
        if job:
            with DOWNLOAD_JOBS_LOCK:
                DOWNLOAD_JOBS[job_id] = job
            snapshot = dict(job)
    if not snapshot:
        return jsonify({'ok': False, 'error': 'job_not_found'}), 404
    counts = count_download_jobs()
    return jsonify({
        'ok': True,
        'job': {
            'job_id': snapshot.get('job_id'),
            'status': snapshot.get('status'),
            'created_at': snapshot.get('created_at'),
            'started_at': snapshot.get('started_at'),
            'finished_at': snapshot.get('finished_at'),
            'progress': snapshot.get('progress') or {},
            'result': snapshot.get('result'),
            'error': snapshot.get('error'),
        },
        'queue_size': counts['active_or_pending'],
        'queued_count': counts['queued'],
        'running_count': counts['running'],
        'total_jobs': counts['total'],
    }), 200

@app.get('/api/download-jobs')
def download_jobs_list():
    with DOWNLOAD_JOBS_LOCK:
        jobs = {job.get('job_id'): serialize_download_job(job) for job in DOWNLOAD_JOBS.values() if job}
    ensure_download_jobs_db()
    with sqlite3.connect(DOWNLOAD_DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute('SELECT * FROM download_jobs ORDER BY created_at DESC'):
            job = db_row_to_job(row)
            if job:
                jobs[job['job_id']] = serialize_download_job(job)
    counts = count_download_jobs()
    jobs = list(jobs.values())
    jobs.sort(key=lambda item: item.get('created_at') or 0, reverse=True)
    return jsonify({
        'ok': True,
        'jobs': jobs,
        'queue_size': counts['active_or_pending'],
        'queued_count': counts['queued'],
        'running_count': counts['running'],
        'total_jobs': counts['total'],
    }), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT','5000'))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG') == '1')
