#!/usr/bin/env python3
# airplay-app.py — AirPlay Drop desktop app.
# A frameless WebKit window (pywebview) rendering ui.html; all playback,
# discovery, auth and transcoding logic lives in airplay-play.py (imported
# below as `core`). Run:  python3 airplay-app.py [optional video file]

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import webview

# where our files live: next to this script, or inside the frozen .app bundle
BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))

# import the engine despite the hyphen in its filename
_spec = importlib.util.spec_from_file_location('core', BASE / 'airplay-play.py')
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

CONFIG = Path.home() / '.airplay-drop.json'
THUMBS = Path.home() / '.airplay-drop-thumbs'
VIDEO_TYPES = ('Video files (*.mkv;*.mp4;*.avi;*.mov;*.m4v;*.webm;*.ts;*.wmv;*.flv)',
               'All files (*.*)')


def load_config():
  try:
    return json.loads(CONFIG.read_text())
  except (OSError, ValueError):
    return {}


def save_config(cfg):
  try:
    CONFIG.write_text(json.dumps(cfg))
  except OSError:
    pass


def thumb_path(path):
  return THUMBS / (hashlib.md5(str(path).encode()).hexdigest() + '.jpg')


def thumb_uri(path):
  t = thumb_path(path)
  if t.exists():
    return 'data:image/jpeg;base64,' + base64.b64encode(t.read_bytes()).decode()
  return None


class Api:
  def __init__(self):
    self.cfg = load_config()
    core.airplay_request.password = self.cfg.get('password')
    self.window = None
    self.devices = []      # [(name, ip, port)]
    self.device = None
    self.file = None       # Path of the loaded video
    self.playing = False
    self.paused = False
    self.server = None
    self.transcoder = None
    self.offset = 0.0      # seconds cut off the front in live-subs mode
    self.total = 0.0
    self._save_tick = 0

  # ---------- window ----------

  def win_close(self):
    self.stop()
    save_config(self.cfg)
    self.window.destroy()

  def win_minimize(self):
    self.window.minimize()

  # ---------- boot / devices ----------

  def boot(self):
    last = self.cfg.get('last_file')
    file_info = None
    if last and Path(last).exists():
      file_info = self.set_file(last, add_history=False)
    return {'recent': self.recent(), 'file': file_info}

  def scan(self):
    self.devices = core.discover()
    selected = -1
    for i, (n, _, _) in enumerate(self.devices):
      if n == self.cfg.get('device'):
        selected = i
        break
    if selected < 0 and self.devices:
      selected = 0
    self.device = self.devices[selected] if selected >= 0 else None
    return {'devices': [{'name': n, 'ip': ip} for n, ip, _ in self.devices],
            'selected': selected}

  def select_device(self, i):
    self.device = self.devices[i]

  # ---------- files ----------

  def browse(self):
    picked = self.window.create_file_dialog(
      webview.OPEN_DIALOG, allow_multiple=False, file_types=VIDEO_TYPES)
    return self.set_file(picked[0]) if picked else None

  def set_file(self, path, add_history=True):
    src = Path(path)
    if not src.exists():
      return {'error': f'File not found: {src.name}'}
    self.file = src
    self._make_thumb(src)
    if add_history:
      hist = self.cfg.setdefault('history', [])
      sp = str(src)
      if sp in hist:
        hist.remove(sp)
      hist.insert(0, sp)
      del hist[20:]
      save_config(self.cfg)
    pos, dur = self.cfg.get('positions', {}).get(str(src), [0, 0])
    return {'path': str(src), 'name': src.name,
            'total': core.duration_of(src),
            'tracks': core.list_sub_tracks(src),
            'track': self.cfg.get('subtracks', {}).get(str(src), 0),
            'resume': pos if dur else 0,
            'thumb': thumb_uri(src)}

  def _make_thumb(self, src):
    t = thumb_path(src)
    if t.exists():
      return
    THUMBS.mkdir(exist_ok=True)
    dur = core.duration_of(src) or 60
    subprocess.run([core.ffmpeg_path(), '-y', '-loglevel', 'error',
                    '-ss', str(dur * 0.25), '-i', str(src), '-frames:v', '1',
                    '-vf', 'scale=240:-2', '-q:v', '5', str(t)],
                   capture_output=True)

  def recent(self):
    out = []
    positions = self.cfg.get('positions', {})
    for p in self.cfg.get('history', []):
      if not Path(p).exists():
        continue
      pos, dur = positions.get(p, [0, 0])
      out.append({'path': p, 'name': Path(p).name, 'pos': pos, 'dur': dur,
                  'thumb': thumb_uri(p)})
    return out

  def remove_recent(self, path):
    hist = self.cfg.get('history', [])
    if path in hist:
      hist.remove(path)
    self.cfg.get('positions', {}).pop(path, None)
    save_config(self.cfg)
    return self.recent()

  # ---------- playback ----------

  def set_password(self, pw):
    core.airplay_request.password = pw

  def play(self, opts):
    if not self.file:
      return {'error': 'prepare', 'detail': 'No file loaded.'}
    if not self.device:
      return {'error': 'prepare', 'detail': 'No AirPlay device. Is the receiver app running?'}
    name, ip, port = self.device
    src = self.file
    resume = float(opts.get('resume') or 0)
    track = int(opts.get('track') or 0)
    self.offset = 0.0
    try:
      self._kill_transcoder()
      self.total = core.duration_of(src)
      if opts.get('subs'):
        # Airflow-style: burn subs while streaming, starts in seconds
        hls, self.transcoder = core.burn_subs_live(src, int(resume), track)
        self.cfg.setdefault('subtracks', {})[str(src)] = track
        self.offset = float(int(resume))
        core.OneFileHandler.file_path = hls
        url_name, frac = 'index.m3u8', 0.0
      else:
        played = core.remux(src) if opts.get('fix_audio') else src
        core.OneFileHandler.file_path = played
        url_name = urllib.parse.quote(played.name)
        frac = resume / self.total if resume and self.total else 0.0
      if not self.server:
        self.server = core.start_server(core.OneFileHandler.file_path)
      url = (f'http://{core.my_lan_ip(ip)}:{self.server.server_address[1]}/'
             f'{url_name}')
      core.play(ip, port, url, frac)
    except core.PlayError as e:
      if e.status == 401:
        rejected = bool(core.airplay_request.password)
        core.airplay_request.password = None
        return {'need_password': True, 'rejected': rejected}
      return {'error': 'play', 'detail': str(e)}
    except SystemExit as e:
      return {'error': 'prepare', 'detail': str(e) or 'Failed.'}
    except subprocess.CalledProcessError:
      return {'error': 'prepare', 'detail': 'ffmpeg failed. Try without subtitles or audio fix.'}
    self.playing, self.paused = True, False
    self.cfg['device'] = name
    self.cfg['password'] = core.airplay_request.password
    self.cfg['last_file'] = str(src)
    save_config(self.cfg)
    return {'ok': True}

  def pause_toggle(self):
    if self.device and self.playing:
      self.paused = not self.paused
      _, ip, port = self.device
      try:
        core.airplay_request(ip, port, 'POST', f'/rate?value={0 if self.paused else 1}.000000')
      except OSError:
        pass
    return {'paused': self.paused}

  def stop(self):
    self.playing, self.paused = False, False
    self._kill_transcoder()
    save_config(self.cfg)
    if self.device:
      _, ip, port = self.device
      try:
        core.airplay_request(ip, port, 'POST', '/stop')
      except OSError:
        pass

  def skip(self, delta):
    if not (self.device and self.playing):
      return
    _, ip, port = self.device
    try:
      status, data, _ = core.airplay_request(ip, port, 'GET', '/scrub')
      import re
      d = dict(re.findall(r'(\w+): ([\d.]+)', data)) if status == 200 else {}
      pos = float(d.get('position', 0))
      core.airplay_request(ip, port, 'POST', f'/scrub?position={max(pos + delta, 0):.3f}')
    except OSError:
      pass

  def seek_frac(self, frac):
    # fraction of the full timeline -> receiver timeline (minus live-subs offset)
    if not (self.device and self.playing and self.total):
      return
    target = max(self.total * float(frac) - self.offset, 0)
    _, ip, port = self.device
    try:
      core.airplay_request(ip, port, 'POST', f'/scrub?position={target:.3f}')
    except OSError:
      pass

  def status(self):
    st = {'playing': self.playing, 'paused': self.paused,
          'pos': 0, 'dur': 0, 'total': self.total, 'offset': self.offset}
    if not (self.playing and self.device):
      return st
    _, ip, port = self.device
    try:
      status, data, _ = core.airplay_request(ip, port, 'GET', '/scrub')
    except OSError:
      return st
    if status == 200:
      import re
      d = dict(re.findall(r'(\w+): ([\d.]+)', data))
      st['pos'] = float(d.get('position', 0))
      st['dur'] = float(d.get('duration', 0))
      if st['dur'] > 0 and self.file:
        total = self.total or (st['dur'] + self.offset)
        self.cfg.setdefault('positions', {})[str(self.file)] = [st['pos'] + self.offset, total]
        self._save_tick += 1
        if self._save_tick % 5 == 0:
          save_config(self.cfg)
    return st

  def _kill_transcoder(self):
    if self.transcoder:
      self.transcoder.kill()
      self.transcoder = None


def main():
  api = Api()
  window = webview.create_window(
    'AirPlay Drop', str(BASE / 'ui.html'),
    js_api=api, width=1020, height=660, min_size=(860, 560),
    frameless=True, easy_drag=False, background_color='#131315')
  api.window = window
  if len(sys.argv) > 1:
    # file passed by the .app droplet: preload it once the UI is up
    def preload():
      api.set_file(sys.argv[1])
    webview.start(preload)
  else:
    webview.start()


if __name__ == '__main__':
  main()
