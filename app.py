#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask app to download HLS/DASH from API JSON (no DRM).
- Audio/subtitle language selection (prefer or all).
- HLS: uses master playlist if audio/subtitle groups exist; otherwise picks best variant.
- Auto filename: Serie-t<season>-e-<episode>-<title>.<ext>.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from flask import Flask, request, render_template, send_file, abort
from urllib.parse import urljoin
import urllib.request
import urllib.error
import urllib.parse

app = Flask(__name__)

# Allow disabling best-variant selection by env (debug help)
DISABLE_VARIANT = os.getenv('DISABLE_VARIANT') == '1'


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
    mu = (resp.get('manifest_uri') or '').strip()
    print(f'[debug] manifest_uri in JSON: {mu}')
    if mu.startswith('http://') or mu.startswith('https://'):
        return mu
    cdns = resp.get('cdns', {}).get('cdn', [])
    if not cdns: raise ValueError('No CDN info (response.cdns.cdn[])')
    chosen = next((c for c in cdns if c.get('priority',0)==0), cdns[0])
    base = (chosen.get('base_uri') or '').strip()
    if not base: raise ValueError('base_uri missing in CDN entry')
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

def infer_series_metadata(resp: dict) -> Tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    """Heuristics: (series_title, season, episode, episode_title) from JSON."""
    cand_series = ['series','series_title','seriesTitle','show','program','program_title',
                   'asset_series','collection','collection_title']
    cand_title  = ['title','name','episode','episode_title','episodeTitle','asset_title']
    series_title = next((resp[k] for k in cand_series if isinstance(resp.get(k), str) and resp[k].strip()), None)
    episode_title = next((resp[k] for k in cand_title if isinstance(resp.get(k), str) and resp[k].strip()), None)
    if not series_title:
        for p in (('program','seriesTitle'),('metadata','seriesTitle'),('meta','series')):
            v = get_nested(resp,*p); 
            if isinstance(v,str) and v.strip(): series_title=v.strip(); break
    if not episode_title:
        for p in (('program','title'),('metadata','title'),('meta','title')):
            v = get_nested(resp,*p); 
            if isinstance(v,str) and v.strip(): episode_title=v.strip(); break
    season = resp.get('season') or resp.get('season_number') or resp.get('seasonNumber') or resp.get('seasonNum')
    if isinstance(season, str) and season.isdigit(): season = int(season)
    episode = resp.get('episode') or resp.get('episode_number') or resp.get('episodeNumber') or resp.get('ep')
    if isinstance(episode, str) and episode.isdigit(): episode = int(episode)
    return series_title, season, episode, episode_title

def build_auto_output_name(resp: dict, ext: str, overrides: dict) -> str:
    """
    Serie-t<season>-e-<episode>-<title>.<ext>
    If season/episode provided via form, they are enforced in the name.
    """
    o_series = (overrides.get('series_title') or '').strip()
    o_season = overrides.get('season_number')
    o_episode = overrides.get('episode_number')
    o_title = (overrides.get('episode_title') or '').strip()

    s_title, s_season, s_episode, s_ep_title = infer_series_metadata(resp)

    series = o_series or s_title or 'video'
    season = int(o_season) if (o_season not in (None,'')) else (int(s_season) if s_season is not None else None)
    episode = int(o_episode) if (o_episode not in (None,'')) else (int(s_episode) if s_episode is not None else None)
    title = o_title or (s_ep_title or '')

    name = slug(series)
    if season is not None:  name += f'-t{season}'
    if episode is not None: name += f'-e-{episode}'
    if title:               name += f'-{slug(title)}'
    return f'{name}.{ext}'


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

@app.post('/download')
def download():
    print('-----[/download] start-----')
    if not FFMPEG_BIN:
        abort(500, 'ffmpeg not found. Install it or add to PATH.')

    # --- Form fields ---
    json_input = request.form.get('json_input','').strip()

    # naming & container
    output_name   = (request.form.get('output','') or '').strip()  # blank => auto
    container     = (request.form.get('container','mp4') or 'mp4').lower()
    if container not in ('mp4','mkv'): container = 'mp4'
    series_title  = request.form.get('series_title','').strip()
    episode_title = request.form.get('episode_title','').strip()
    season_number = request.form.get('season_number','').strip()
    episode_number= request.form.get('episode_number','').strip()

    # headers/options
    user_agent = request.form.get('user_agent','Mozilla/5.0').strip() or 'Mozilla/5.0'
    referer = request.form.get('referer','').strip()
    origin  = request.form.get('origin','').strip()
    cookie  = request.form.get('cookie','').strip()
    extra_headers = request.form.get('extra_headers','').splitlines()
    force_aac = request.form.get('force_aac') == 'on'

    # language modes
    audio_mode = request.form.get('audio_mode','prefer')  # default|prefer|all
    audio_pref = request.form.get('audio_pref','').strip().lower()
    subs_mode  = request.form.get('subs_mode','none')     # none|prefer|all
    subs_pref  = request.form.get('subs_pref','').strip().lower()

    # build header lines
    header_lines: List[str] = []
    if referer: header_lines.append(f'Referer: {referer}')
    if origin:  header_lines.append(f'Origin: {origin}')
    for line in extra_headers:
        line=line.strip()
        if line and ':' in line: header_lines.append(line)
    headers_arg = headers_list_to_ffmpeg_arg(header_lines, cookie=cookie)

    # parse JSON
    try:
        data = json.loads(json_input)
        resp = data.get('response') or {}
    except Exception as e:
        abort(400, f'Invalid JSON: {e}')

    drm = (resp.get('drm_type') or '').lower()
    pkg = (resp.get('package_type') or '').lower()
    if drm and drm != 'none':
        abort(400, f'Content protected with DRM "{drm}". Cannot download.')

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
    final_name = output_name or build_auto_output_name(resp, container, overrides)
    tmpdir = Path(tempfile.mkdtemp(prefix='flask_hls_'))
    outfile = tmpdir / final_name

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
        cmd[1:1] = ['-loglevel','debug','-report']
    else:
        cmd[1:1] = ['-loglevel','warning','-stats']

    if headers_arg:
        cmd += ['-headers', headers_arg]

    # input
    cmd += ['-i', input_url]

    # mapping
    def split_langs(s: str) -> List[str]:
        return [x.strip().lower() for x in s.split(',') if x.strip()]

    maps: List[str] = []
    maps += ['-map','0:v:0']  # first video

    # audio
    if audio_mode == 'all':
        maps += ['-map','0:a?']
    elif audio_mode == 'prefer' and audio_pref:
        for lang in split_langs(audio_pref):
            maps += ['-map', f'0:a:m:language:{lang}?']
        maps += ['-map','0:a:0?']  # fallback
    else:
        maps += ['-map','0:a:0?']  # default

    # subtitles
    if subs_mode == 'all':
        maps += ['-map','0:s?']
    elif subs_mode == 'prefer' and subs_pref:
        for lang in split_langs(subs_pref):
            maps += ['-map', f'0:s:m:language:{lang}?']
        maps += ['-map','0:s:0?']  # fallback
    else:
        maps += ['-sn']  # disable subs

    cmd += maps

    # codecs
    if force_aac:
        cmd += ['-c:v','copy','-c:a','aac','-b:a','160k']
    else:
        cmd += ['-c','copy']
        if pkg == 'hls':
            cmd += ['-bsf:a','aac_adtstoasc']

    # subtitle codec for MP4
    if (subs_mode != 'none') and (container == 'mp4'):
        cmd += ['-c:s','mov_text']

    if container == 'mp4':
        cmd += ['-movflags','+faststart']

    # ✅ output/muxer option must be before the output file
    cmd += ['-max_muxing_queue_size', '2048']
    cmd += [str(outfile)]

    # run
    printable_cmd = ' '.join('Cookie: ****' if (isinstance(p,str) and 'Cookie:' in p) else str(p) for p in cmd)
    print('[debug] ffmpeg cmd:', printable_cmd)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    print(f'[debug] ffmpeg returncode={proc.returncode}')
    if proc.stderr:
        for line in proc.stderr.splitlines()[-20:]:
            print('  ', line)

    if proc.returncode != 0 or not outfile.exists() or outfile.stat().st_size == 0:
        tail = (proc.stderr or '').splitlines()[-20:]
        abort(500, 'ffmpeg failed.\n' + '\n'.join(tail))

    mime = 'video/mp4' if container=='mp4' else 'video/x-matroska'
    return send_file(str(outfile), as_attachment=True, download_name=final_name, mimetype=mime)


if __name__ == '__main__':
    port = int(os.getenv('PORT','5000'))
    app.run(host='0.0.0.0', port=port, debug=True)
