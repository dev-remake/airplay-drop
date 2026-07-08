#!/usr/bin/env python3
# airplay-play.py — the lazy Airflow: send a local video file to an AirPlay
# receiver (your projector's AirPlay APK) from the Mac.
#
# How it works (AirPlay 1 video protocol, what those APKs implement):
#   1. Find receivers on the network via Bonjour (macOS built-in `dns-sd`).
#   2. Start a tiny HTTP server on the Mac that serves ONLY your video file,
#      with Range support so the player can seek.
#   3. POST the video's URL to http://<projector>:7000/play.
#      The projector downloads and plays it with its own player.
#
# Usage:
#   python3 airplay-play.py <video>            # discover devices, pick, play
#   python3 airplay-play.py <video> --ip X     # skip discovery
#   python3 airplay-play.py <video> --remux    # MKV won't play? repack to MP4,
#                                              #   audio -> AAC (fixes DTS), fast
#   python3 airplay-play.py <video> --subs     # burn in subtitles (.srt next to
#                                              #   the file, or embedded track);
#                                              #   full re-encode, takes minutes
#   python3 airplay-play.py --probe --ip X     # self-check: is the receiver
#                                              #   answering on port 7000?
#
# Ctrl+C stops playback on the projector and exits.

import argparse
import base64
import hashlib
import http.client
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

AIRPLAY_PORT = 7000   # receivers listen here


# ---------- device discovery (Bonjour via built-in dns-sd) ----------

def run_capture(cmd, timeout):
  # dns-sd never exits on its own; run it, kill after timeout, keep output.
  try:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout or ''
  except subprocess.TimeoutExpired as e:
    return (e.stdout or b'').decode(errors='replace') if isinstance(e.stdout, bytes) else (e.stdout or '')


def discover(timeout=3):
  # Browse for _airplay._tcp services, return [(name, ip, port)].
  out = run_capture(['dns-sd', '-B', '_airplay._tcp', 'local.'], timeout)
  names = []
  for line in out.splitlines():
    # match lines like: "14:05:22.339  Add  2  6 local.  _airplay._tcp.  AirScreen[X]"
    if ' Add ' in f' {line} ' and '_airplay._tcp.' in line:
      name = line.split('_airplay._tcp.')[-1].strip()
      if name and name not in names:
        names.append(name)

  devices = []
  for name in names:
    # resolve each instance to hostname:port
    out = run_capture(['dns-sd', '-L', name, '_airplay._tcp', 'local.'], timeout=2)
    m = re.search(r'can be reached at (\S+):(\d+)', out)
    if not m:
      continue
    host, port = m.group(1).rstrip('.'), int(m.group(2))
    try:
      ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
      if ip.startswith('127.'):
        continue  # the Mac's own AirPlay receiver — not a play target
      devices.append((name, ip, port))
    except socket.gaierror:
      pass
  return devices


def pick_device(args):
  if args.ip:
    return ('manual', args.ip, AIRPLAY_PORT)
  print('Searching for AirPlay devices (3s)...')
  devices = discover()
  if not devices:
    sys.exit('No AirPlay devices found. Is the APK running on the projector? '
             'Or pass --ip <projector-ip>.')
  if len(devices) == 1:
    name, ip, port = devices[0]
    print(f'Found: {name} ({ip})')
    return devices[0]
  for i, (name, ip, _) in enumerate(devices, 1):
    print(f'  {i}. {name} ({ip})')
  choice = int(input('Pick device: ')) - 1
  return devices[choice]


# ---------- optional ffmpeg preprocessing ----------

def ffmpeg_path():
  # frozen .app bundle: ffmpeg/ffprobe ship inside the app
  if getattr(sys, 'frozen', False):
    bundled = Path(getattr(sys, '_MEIPASS', '')) / 'bin' / 'ffmpeg'
    if bundled.exists():
      return str(bundled)
  # the static-ffmpeg pip package ships a full build WITH libass (subtitle
  # rendering) — Homebrew's ffmpeg doesn't, so prefer static. Downloads its
  # binaries once on first use, then it's a cached path lookup.
  try:
    from static_ffmpeg import run as _sf
    return _sf.get_or_fetch_platform_executables_else_raise()[0]
  except Exception:
    pass
  # fallbacks can play/remux but NOT burn subtitles (no libass)
  for c in ('/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg',
            shutil.which('ffmpeg')):
    if c and Path(c).exists():
      return c
  sys.exit('ffmpeg not found - run: pip3 install static-ffmpeg')


def ffprobe_path():
  return str(Path(ffmpeg_path()).with_name('ffprobe'))


def duration_of(src):
  # true length in seconds via ffprobe
  r = subprocess.run([ffprobe_path(), '-v', 'error', '-show_entries',
                      'format=duration', '-of', 'csv=p=0', str(src)],
                     capture_output=True, text=True)
  try:
    return float(r.stdout.strip())
  except ValueError:
    return 0.0


def list_sub_tracks(src):
  # labels for every embedded subtitle track, in 0:s:N order,
  # e.g. ['eng — SDH', 'spa — Latin American', ...]
  r = subprocess.run([ffprobe_path(), '-v', 'error', '-select_streams', 's',
                      '-show_entries', 'stream=codec_name:stream_tags=language,title',
                      '-of', 'csv=p=0', str(src)], capture_output=True, text=True)
  tracks = []
  for line in r.stdout.strip().splitlines():
    parts = line.split(',')
    lang = parts[1] if len(parts) > 1 else '?'
    title = ','.join(parts[2:]) if len(parts) > 2 else ''
    tracks.append(f'{lang} — {title}' if title else lang)
  return tracks


def extract_subs(src, start=0, track=0):
  # get subtitles into a temp file with a safe path: ffmpeg's subtitles=
  # filter chokes on special chars in paths ([brackets] etc.), argv paths
  # (-i) don't. Sidecar .srt wins, else embedded track number `track`.
  # start shifts timestamps so subs line up when resuming mid-file.
  ff = ffmpeg_path()
  srt = src.with_suffix('.srt')
  source = srt if srt.exists() else src
  sub_tmp = Path(tempfile.gettempdir()) / 'airplay-sub.ass'
  r = subprocess.run([ff, '-y', '-loglevel', 'error',
                      *(['-ss', str(start)] if start else []),
                      '-i', str(source), '-map', f'0:s:{track}', str(sub_tmp)],
                     capture_output=True, text=True)
  if r.returncode != 0:
    sys.exit('No usable subtitles: no .srt next to the file and no text '
             'subtitle track inside it.\n' + r.stderr[-300:])
  return sub_tmp


def prepared_path(src, suffix):
  # cache the converted file next to the original so a rerun is instant
  out = src.with_name(src.stem + suffix)
  if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
    print(f'Reusing cached {out.name}')
    return out
  return None


def remux(src):
  # repack into MP4: video untouched (fast), audio -> AAC (Android players
  # often can't decode DTS). Runs at disk speed, ~1-2 min for a movie.
  ff = ffmpeg_path()
  out = prepared_path(src, '.airplay.mp4')
  if out:
    return out
  out = src.with_name(src.stem + '.airplay.mp4')
  tmp = out.with_name(out.stem + '.part.mp4')
  print('Remuxing to MP4 (video copied, audio -> AAC)...')
  subprocess.run([ff, '-y', '-loglevel', 'warning', '-stats', '-i', str(src),
                  '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', str(tmp)], check=True)
  tmp.rename(out)  # only a finished conversion becomes the cached file
  return out


def burn_subs(src, track=0):
  # burn subtitles into the picture (pre-encode, cached next to the original;
  # used by the CLI --subs flag — the GUI uses burn_subs_live instead)
  ff = ffmpeg_path()
  suffix = f'.subs{track}.mp4' if track else '.subs.mp4'
  out = prepared_path(src, suffix)
  if out:
    return out
  out = src.with_name(src.stem + suffix)
  sub_tmp = extract_subs(src, 0, track)
  print('Burning subtitles (hardware encode, takes a few minutes)...')
  tmp = out.with_name(out.stem + '.part.mp4')
  subprocess.run([ff, '-y', '-loglevel', 'warning', '-stats', '-i', str(src),
                  '-vf', f'subtitles={sub_tmp}',
                  '-c:v', 'h264_videotoolbox', '-b:v', '6000k',
                  '-c:a', 'aac', '-b:a', '192k', str(tmp)], check=True)
  tmp.rename(out)  # only a finished burn becomes the cached file
  return out


def burn_subs_live(src, start=0, track=0):
  # the Airflow way: transcode + burn subs on the fly into an HLS stream the
  # receiver plays while it's still being made — playback starts in seconds.
  # start = resume position in seconds (subs are extracted with the same
  # offset so they stay in sync). Returns (hls_dir, transcoder_process).
  ff = ffmpeg_path()
  sub_tmp = extract_subs(src, start, track)
  hls = Path(tempfile.gettempdir()) / 'airplay-hls'
  shutil.rmtree(hls, ignore_errors=True)
  hls.mkdir()
  proc = subprocess.Popen(
    [ff, '-y', '-loglevel', 'error',
     *(['-ss', str(start)] if start else []),
     '-i', str(src),
     '-vf', f'subtitles={sub_tmp}',
     '-c:v', 'h264_videotoolbox', '-b:v', '6000k',
     '-c:a', 'aac', '-b:a', '192k',
     '-f', 'hls', '-hls_time', '4', '-hls_playlist_type', 'event',
     '-hls_segment_filename', str(hls / 'seg%05d.ts'),
     str(hls / 'index.m3u8')])
  # wait until the playlist and a couple of segments exist before playing
  for _ in range(120):
    if (hls / 'index.m3u8').exists() and len(list(hls.glob('seg*.ts'))) >= 2:
      return hls, proc
    if proc.poll() is not None:
      sys.exit('Transcoder stopped unexpectedly — try without subtitles.')
    time.sleep(0.5)
  proc.kill()
  sys.exit('Transcoder too slow to start — try without subtitles.')


# ---------- HTTP server with Range support ----------
# serves either one video file, or an HLS directory (playlist + segments);
# stdlib SimpleHTTPRequestHandler has no Range support, and players need it

CTYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/mp4',
          '.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t'}


class OneFileHandler(http.server.BaseHTTPRequestHandler):
  file_path = None  # a video file OR an HLS directory; set before serving

  def log_message(self, *a):  # silence per-request logging
    pass

  def do_HEAD(self):
    self._serve(head=True)

  def do_GET(self):
    self._serve(head=False)

  def _target(self):
    base = Path(self.file_path)
    if not base.is_dir():
      return base  # single-file mode: any path serves the file
    # HLS mode: flat directory, basename only — no path traversal possible
    name = Path(urllib.parse.unquote(self.path.split('?')[0])).name
    p = base / name
    return p if p.is_file() else None

  def _serve(self, head):
    target = self._target()
    if target is None:
      self.send_error(404)
      return
    size = os.path.getsize(target)
    start, end = 0, size - 1
    rng = self.headers.get('Range')
    if rng:
      m = re.match(r'bytes=(\d*)-(\d*)', rng)
      if m:
        if m.group(1):
          start = int(m.group(1))
        if m.group(2):
          end = min(int(m.group(2)), size - 1)
      self.send_response(206)
      self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    else:
      self.send_response(200)
    ctype = CTYPES.get(target.suffix.lower(), 'video/x-matroska')
    self.send_header('Content-Type', ctype)
    self.send_header('Content-Length', str(end - start + 1))
    self.send_header('Accept-Ranges', 'bytes')
    self.end_headers()
    if head:
      return
    with open(target, 'rb') as f:
      f.seek(start)
      remaining = end - start + 1
      while remaining > 0:
        chunk = f.read(min(65536, remaining))
        if not chunk:
          break
        try:
          self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
          return  # player seeked / closed connection — normal
        remaining -= len(chunk)


def start_server(path):
  OneFileHandler.file_path = path
  # port 0 = let the OS pick a free one (no clash if two instances run);
  # read the actual port back from server.server_address
  server = http.server.ThreadingHTTPServer(('0.0.0.0', 0), OneFileHandler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  return server


def my_lan_ip(peer_ip):
  # which of the Mac's IPs the projector can reach: open a UDP socket toward
  # it (sends nothing) and read back the chosen local address
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.connect((peer_ip, AIRPLAY_PORT))
  ip = s.getsockname()[0]
  s.close()
  return ip


# ---------- AirPlay 1 control (plain HTTP on port 7000) ----------

class PlayError(RuntimeError):
  # carries the HTTP status so callers can react (401 -> ask for password)
  def __init__(self, status, detail):
    super().__init__(detail)
    self.status = status


def _digest_header(method, uri, challenge, password, username='AirPlay', cnonce=None):
  # minimal RFC 2617 digest auth — this is how receivers enforce the
  # "AirPlay password" (AirPin: settings -> password/verification)
  fields = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
  realm, nonce = fields.get('realm', ''), fields.get('nonce', '')
  h = lambda s: hashlib.md5(s.encode()).hexdigest()
  ha1 = h(f'{username}:{realm}:{password}')
  ha2 = h(f'{method}:{uri}')
  if 'auth' in fields.get('qop', ''):
    cnonce = cnonce or uuid.uuid4().hex[:16]
    resp = h(f'{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}')
    auth = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}", qop=auth, nc=00000001, cnonce="{cnonce}"')
  else:
    resp = h(f'{ha1}:{nonce}:{ha2}')
    auth = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}"')
  if fields.get('opaque'):
    auth += f', opaque="{fields["opaque"]}"'
  return auth


def airplay_request(ip, port, method, path, body=None):
  conn = http.client.HTTPConnection(ip, port, timeout=10)
  headers = {'User-Agent': 'MediaControl/1.0',
             'X-Apple-Session-ID': airplay_request.session}
  if body is not None:
    headers['Content-Type'] = 'text/parameters'
  try:
    conn.request(method, path, body, headers)
    resp = conn.getresponse()
    data = resp.read().decode(errors='replace')
    hdrs = dict(resp.getheaders())
    challenge = hdrs.get('WWW-Authenticate', '')
    if resp.status == 401 and airplay_request.password and challenge:
      # the nonce is tied to THIS TCP connection (AirPin at least) — the
      # authed retry must go over the same socket; a new connection would
      # get a fresh nonce and always be rejected
      if challenge.lower().startswith('digest'):
        headers['Authorization'] = _digest_header(
          method, path, challenge, airplay_request.password)
      else:
        headers['Authorization'] = 'Basic ' + base64.b64encode(
          f'AirPlay:{airplay_request.password}'.encode()).decode()
      conn.request(method, path, body, headers)
      resp = conn.getresponse()
      data = resp.read().decode(errors='replace')
      hdrs = dict(resp.getheaders())
    return resp.status, data, hdrs
  finally:
    conn.close()

airplay_request.session = str(uuid.uuid4())
airplay_request.password = None  # set from --password flag or the GUI prompt


def play(ip, port, url, start=0.0):
  # start is a relative position 0.0-1.0 (fraction of duration), per AirPlay spec
  body = f'Content-Location: {url}\nStart-Position: {start:.6f}\n'
  status, data, hdrs = airplay_request(ip, port, 'POST', '/play', body)
  if status != 200:
    detail = (f'Receiver refused /play\n'
              f'HTTP status: {status}\n'
              f'WWW-Authenticate: {hdrs.get("WWW-Authenticate", "(none)")}\n'
              f'Response headers: {hdrs}\n'
              f'Body: {data[:500] or "(empty)"}\n'
              f'Sent URL: {url}')
    raise PlayError(status, detail)


def fmt_time(sec):
  sec = int(sec)
  return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}'


def progress_loop(ip, port):
  # poll position/duration; also keeps the session looking alive
  while True:
    time.sleep(5)
    try:
      status, data, _ = airplay_request(ip, port, 'GET', '/scrub')
      d = dict(re.findall(r'(\w+): ([\d.]+)', data)) if status == 200 else {}
      if 'position' in d:
        print(f"\r  ▶ {fmt_time(float(d['position']))} / {fmt_time(float(d.get('duration', 0)))}  ",
              end='', flush=True)
    except OSError:
      print('\nLost connection to receiver.')
      return


# ---------- main ----------

def main():
  ap = argparse.ArgumentParser(description='Send a video to an AirPlay receiver.')
  ap.add_argument('video', nargs='?', help='video file (drag it into the terminal)')
  ap.add_argument('--ip', help='receiver IP, skips discovery')
  ap.add_argument('--password', help='AirPlay password if the receiver has one set')
  ap.add_argument('--remux', action='store_true', help='repack to MP4 + AAC audio first')
  ap.add_argument('--subs', action='store_true', help='burn in subtitles first (slow)')
  ap.add_argument('--sub-track', type=int, default=0,
                  help='which embedded subtitle track to use (default 0)')
  ap.add_argument('--probe', action='store_true', help='just check the receiver responds')
  args = ap.parse_args()

  airplay_request.password = args.password
  name, ip, port = pick_device(args)

  if args.probe:
    # self-check: /server-info is the harmless "who are you" endpoint
    status, data, hdrs = airplay_request(ip, port, 'GET', '/server-info')
    print(f'/server-info -> HTTP {status}')
    if hdrs.get('WWW-Authenticate'):
      print(f'WWW-Authenticate: {hdrs["WWW-Authenticate"]}  (receiver is password-protected)')
    print(data[:2000] or '(empty body)')
    return

  if not args.video:
    ap.error('missing video file')
  src = Path(args.video).expanduser().resolve()
  if not src.exists():
    sys.exit(f'File not found: {src}')

  if args.subs:
    src = burn_subs(src, args.sub_track)
  elif args.remux:
    src = remux(src)

  server = start_server(src)
  url = (f'http://{my_lan_ip(ip)}:{server.server_address[1]}/'
         f'{urllib.parse.quote(src.name)}')
  print(f'Serving {src.name}, telling {name} to play it...')
  try:
    play(ip, port, url)
  except PlayError as e:
    hint = ('\n\n401 = password-protected: rerun with --password <AirPlay password>, '
            'or turn the password off in the receiver app settings.') if e.status == 401 else ''
    sys.exit(f'{e}{hint}')
  print('Playing. Ctrl+C to stop.')

  try:
    progress_loop(ip, port)
  except KeyboardInterrupt:
    try:
      airplay_request(ip, port, 'POST', '/stop')
    except OSError:
      pass
    print('\nStopped.')


if __name__ == '__main__':
  main()
