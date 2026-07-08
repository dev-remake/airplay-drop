#!/usr/bin/env python3
# airplay-gui.py — Airflow-style GUI on top of airplay-play.py.
#
# Window with: device dropdown (+ rescan), drag-and-drop zone (or click to
# browse), subtitle + fix-audio checkboxes, play/pause, stop, seek bar,
# volume slider, elapsed/total time.
#
# All AirPlay control is plain HTTP to the receiver (see airplay-play.py);
# this file is only the interface. Network and ffmpeg work run in threads
# so the window never freezes; widgets are only touched from the Tk main
# thread via root.after().
#
# Run:  python3 airplay-gui.py [optional video file]

import importlib.util
import json
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

# import the CLI script as a module despite the hyphen in its filename
_spec = importlib.util.spec_from_file_location(
  'core', Path(__file__).with_name('airplay-play.py'))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

# drag-and-drop is the one thing Tk can't do natively on macOS
try:
  from tkinterdnd2 import DND_FILES, TkinterDnD
  ROOT_CLS = TkinterDnD.Tk
except ImportError:  # ponytail: without tkinterdnd2 the Browse click still works
  DND_FILES = None
  ROOT_CLS = tk.Tk

VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v', '.webm', '.ts', '.wmv', '.flv')

# remembers last device, AirPlay password and per-file playback positions
CONFIG = Path.home() / '.airplay-drop.json'


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


class App:
  def __init__(self, root, initial_file=None):
    self.root = root
    self.devices = []          # [(name, ip, port)]
    self.file = None           # Path of the picked video
    self.playing = False
    self.paused = False
    self.duration = 0.0
    self.dragging_seek = False
    self.server = None
    self.transcoder = None     # live-subtitle ffmpeg process
    self.resume_offset = 0.0   # seconds cut off the front in live-subs mode
    self.total_duration = 0.0  # real file length from ffprobe
    self.cfg = load_config()
    core.airplay_request.password = self.cfg.get('password')  # saved AirPlay password

    root.title('AirPlay Drop')
    root.geometry('460x600')
    root.minsize(430, 540)
    root.protocol('WM_DELETE_WINDOW', self.on_close)

    GRAY = '#98989d'  # readable secondary gray in both light and dark mode
    self.gray = GRAY
    ttk.Style().configure('Treeview', rowheight=26)
    body = ttk.Frame(root, padding=(16, 12, 16, 10))
    body.pack(fill='both', expand=True)

    def section(text, top=10):
      ttk.Label(body, text=text.upper(), foreground=GRAY,
                font=('Helvetica', 11)).pack(fill='x', pady=(top, 3))

    # --- device ---
    section('AirPlay device', top=0)
    row = ttk.Frame(body); row.pack(fill='x')
    self.device_box = ttk.Combobox(row, state='readonly', values=['Scanning...'])
    self.device_box.pack(side='left', fill='x', expand=True)
    ttk.Button(row, text='↻', width=2, command=self.scan).pack(side='left', padx=(6, 0))

    # --- drop zone: rounded dashed outline drawn on a canvas so it looks
    # native in both light and dark mode ---
    self.drop = tk.Canvas(body, height=104, highlightthickness=0, bg=root.cget('bg'))
    self.drop.pack(fill='x', pady=(14, 2))
    # canvas text can't use adaptive label colors on every Tk build — probe once
    try:
      probe = self.drop.create_text(0, 0, text='', fill='systemTextColor')
      self.drop.delete(probe)
      self.text_color = 'systemTextColor'
    except tk.TclError:
      self.text_color = '#444444'
    self.drop.bind('<Configure>', self.redraw_drop)
    self.drop.bind('<Button-1>', lambda e: self.browse())
    if DND_FILES:
      self.drop.drop_target_register(DND_FILES)
      self.drop.dnd_bind('<<Drop>>', self.on_drop)

    # --- options: subtitles + track picker + audio fix on one line ---
    row = ttk.Frame(body); row.pack(fill='x', pady=(6, 0))
    self.subs_var = tk.BooleanVar()
    self.remux_var = tk.BooleanVar()
    ttk.Checkbutton(row, text='Subtitles', variable=self.subs_var).pack(side='left')
    self.subtrack_box = ttk.Combobox(row, state='readonly', values=[], width=16)
    self.subtrack_box.pack(side='left', fill='x', expand=True, padx=8)
    ttk.Checkbutton(row, text='Fix audio', variable=self.remux_var).pack(side='left')

    # --- recent files, with a where-you-left-off bar ---
    section('Recent')
    self.recent = ttk.Treeview(body, columns=('prog',), show='tree', height=6)
    self.recent.column('#0', stretch=True)
    self.recent.column('prog', width=170, stretch=False)
    self.recent.pack(fill='both', expand=True)
    self.recent.bind('<<TreeviewSelect>>', self.on_recent_select)
    self.recent.bind('<BackSpace>', self.on_recent_delete)
    self.recent.bind('<Delete>', self.on_recent_delete)

    # --- seek bar + time ---
    row = ttk.Frame(body); row.pack(fill='x', pady=(12, 0))
    self.seek = ttk.Scale(row, from_=0, to=100, value=0)
    self.seek.pack(side='left', fill='x', expand=True)
    self.seek.bind('<Button-1>', lambda e: setattr(self, 'dragging_seek', True))
    self.seek.bind('<ButtonRelease-1>', self.on_seek)
    self.time_label = ttk.Label(row, text='0:00:00 / 0:00:00', width=18,
                                anchor='e', foreground=GRAY)
    self.time_label.pack(side='left')

    # --- transport (no volume: AirPin ignores network volume commands,
    # the projector remote is the volume control) ---
    row = ttk.Frame(body); row.pack(fill='x', pady=(8, 0))
    self.play_btn = ttk.Button(row, text='▶ Play', width=9, command=self.play_pause)
    self.play_btn.pack(side='left')
    ttk.Button(row, text='◼ Stop', width=9, command=self.stop).pack(side='left', padx=(8, 0))

    self.status = ttk.Label(body, text='', foreground=GRAY, anchor='w',
                            font=('Helvetica', 11))
    self.status.pack(fill='x', pady=(8, 0))

    self.scan()
    self.render_history()
    # preload the dropped file, else the last played one
    preload = initial_file or self.cfg.get('last_file')
    if preload:
      self.set_file(Path(preload))

  # ---------- helpers ----------

  def bg(self, fn, *a):
    # run network/ffmpeg work off the UI thread
    threading.Thread(target=fn, args=a, daemon=True).start()

  def ui(self, fn, *a):
    # touch widgets only from the main thread
    self.root.after(0, lambda: fn(*a))

  def set_status(self, text):
    self.root.after(0, lambda: self.status.config(text=text))

  def current_device(self):
    i = self.device_box.current()
    if i < 0 or i >= len(self.devices):
      return None
    return self.devices[i]

  # ---------- device scan ----------

  def scan(self):
    self.device_box.set('Scanning...')
    def work():
      found = core.discover()
      def done():
        self.devices = found
        names = [f'{n} ({ip})' for n, ip, _ in found] or ['No devices found']
        self.device_box['values'] = names
        idx = 0
        for i, (n, _, _) in enumerate(found):  # preselect last used device
          if n == self.cfg.get('device'):
            idx = i
            break
        self.device_box.current(idx)
      self.ui(done)
    self.bg(work)

  # ---------- file selection ----------

  def on_drop(self, event):
    paths = self.root.tk.splitlist(event.data)  # handles {paths with spaces}
    if paths:
      self.set_file(Path(paths[0]))

  def browse(self):
    p = filedialog.askopenfilename(filetypes=[('Video', ' '.join('*' + e for e in VIDEO_EXTS)),
                                              ('All files', '*')])
    if p:
      self.set_file(Path(p))

  def redraw_drop(self, event=None):
    # (re)draw the drop zone: rounded dashed border + current state text
    c = self.drop
    c.delete('all')
    w = max(c.winfo_width(), 200)
    h = 104
    r = 16
    x1, y1, x2, y2 = 3, 3, w - 3, h - 3
    pts = [x1+r,y1, x2-r,y1, x2,y1, x2,y1+r, x2,y2-r, x2,y2, x2-r,y2,
           x1+r,y2, x1,y2, x1,y2-r, x1,y1+r, x1,y1]
    c.create_polygon(pts, smooth=True, fill='', outline=self.gray,
                     dash=(5, 4), width=1.4)
    if self.file:
      c.create_text(w/2, h/2 - 16, text='🎬', font=('Helvetica', 24))
      c.create_text(w/2, h/2 + 14, text=self.file.name, fill=self.text_color,
                    font=('Helvetica', 13), width=w - 44)
      c.create_text(w/2, h/2 + 34, text='drop another file to switch',
                    fill=self.gray, font=('Helvetica', 10))
    else:
      c.create_text(w/2, h/2 - 16, text='🎬', font=('Helvetica', 24))
      c.create_text(w/2, h/2 + 12, text='Drop a video here',
                    fill=self.text_color, font=('Helvetica', 13))
      c.create_text(w/2, h/2 + 32, text='or click to browse',
                    fill=self.gray, font=('Helvetica', 10))

  def set_file(self, path, add_history=True):
    if not path.exists():
      return
    self.file = path
    self.redraw_drop()
    self.set_status('Ready. Hit Play.')
    if add_history:
      hist = self.cfg.setdefault('history', [])
      sp = str(path)
      if sp in hist:
        hist.remove(sp)
      hist.insert(0, sp)
      del hist[20:]  # keep the list short
      save_config(self.cfg)
      self.render_history()
    self.bg(self.load_subtracks, path)

  def load_subtracks(self, path):
    tracks = core.list_sub_tracks(path)
    def done():
      if self.file != path:
        return  # a newer file was picked meanwhile
      self.subtrack_box['values'] = tracks or ['(no embedded subs)']
      remembered = self.cfg.get('subtracks', {}).get(str(path), 0)
      self.subtrack_box.current(min(remembered, max(len(tracks) - 1, 0)))
    self.ui(done)

  # ---------- recent list ----------

  @staticmethod
  def bar(frac):
    n = round(max(0.0, min(frac, 1.0)) * 10)
    return '█' * n + '░' * (10 - n)

  def render_history(self):
    self.recent.delete(*self.recent.get_children())
    for p in self.cfg.get('history', []):
      pos, dur = self.cfg.get('positions', {}).get(p, [0, 0])
      frac = pos / dur if dur else 0
      self.recent.insert('', 'end', iid=p, text=Path(p).name,
                         values=(f'{self.bar(frac)} {core.fmt_time(pos)}',))

  def on_recent_select(self, event):
    sel = self.recent.selection()
    if sel:
      # load without reshuffling the list under the click
      self.set_file(Path(sel[0]), add_history=False)

  def on_recent_delete(self, event):
    for iid in self.recent.selection():
      self.cfg.get('history', []).remove(iid)
      self.cfg.get('positions', {}).pop(iid, None)
    save_config(self.cfg)
    self.render_history()

  # ---------- playback ----------

  def play_pause(self):
    if self.playing:  # acting as pause/resume toggle
      dev = self.current_device()
      if not dev:
        return
      self.paused = not self.paused
      rate = 0 if self.paused else 1
      self.play_btn.config(text='▶ Resume' if self.paused else '⏸ Pause')
      self.bg(core.airplay_request, dev[1], dev[2], 'POST', f'/rate?value={rate}.000000')
      return

    if not self.file:
      messagebox.showinfo('AirPlay Drop', 'Drop or pick a video first.')
      return
    dev = self.current_device()
    if not dev:
      messagebox.showinfo('AirPlay Drop', 'No AirPlay device — is the projector app running?')
      return
    # offer to resume if we have a saved position for this file
    resume_sec = 0.0
    saved = self.cfg.get('positions', {}).get(str(self.file))
    if saved and saved[1] > 0 and 60 < saved[0] < saved[1] * 0.95:
      if messagebox.askyesno('AirPlay Drop',
                             f'Resume {self.file.name}\nfrom {core.fmt_time(saved[0])}?'):
        resume_sec = saved[0]
    # read all widget state here on the main thread, hand plain values to the worker
    self._play_args = (dev, resume_sec, self.subs_var.get(), self.remux_var.get(),
                       max(self.subtrack_box.current(), 0))
    self.play_btn.config(state='disabled')
    self.bg(self.start_playback, *self._play_args)

  def start_playback(self, dev, resume_sec=0.0, subs=False, remux=False, track=0):
    name, ip, port = dev
    src = self.file
    self.resume_offset = 0.0
    try:
      self.kill_transcoder()
      # the receiver misreports duration on live streams — use the real one
      self.total_duration = core.duration_of(src)
      if subs:
        # Airflow-style: burn subs while streaming — starts in seconds
        self.set_status('Starting subtitled stream...')
        hls, self.transcoder = core.burn_subs_live(src, int(resume_sec), track)
        self.cfg.setdefault('subtracks', {})[str(src)] = track  # remember choice
        self.resume_offset = float(int(resume_sec))
        core.OneFileHandler.file_path = hls
        url_name, start_frac = 'index.m3u8', 0.0
      else:
        if remux:
          self.set_status('Repacking audio...')
          src = core.remux(src)
        core.OneFileHandler.file_path = src
        url_name = urllib.parse.quote(src.name)
        # direct file play: resume is a 0-1 fraction of known duration
        saved = self.cfg.get('positions', {}).get(str(self.file))
        start_frac = resume_sec / saved[1] if resume_sec and saved and saved[1] else 0.0

      if not self.server:
        self.server = core.start_server(core.OneFileHandler.file_path)
      url = (f'http://{core.my_lan_ip(ip)}:{self.server.server_address[1]}/'
             f'{url_name}')
      self.set_status(f'Sending to {name}...')
      core.play(ip, port, url, start_frac)
    except core.PlayError as e:
      if e.status == 401 and not core.airplay_request.password:
        # receiver is password-protected: ask and retry
        self.set_status('Password required (HTTP 401).')
        self.ui(self.ask_password, dev)
      elif e.status == 401:
        # password was given but rejected — clear it so Play re-asks,
        # and show details instead of looping the prompt
        core.airplay_request.password = None
        self.set_status('Password rejected.')
        self.ui(self.show_error, 'Password rejected',
                f'Device: {name} ({ip}:{port})\nFile: {src.name}\n'
                f'The password was rejected — check it in the AirPin settings, '
                f'then hit Play to try again.\n\n{e}')
      else:
        self.set_status(f'Play failed (HTTP {e.status}).')
        self.ui(self.show_error, 'Play failed',
                f'Device: {name} ({ip}:{port})\nFile: {src.name}\n\n{e}')
      self.root.after(0, lambda: self.play_btn.config(state='normal'))
      return
    except SystemExit as e:   # core uses sys.exit for errors — show, don't die
      msg = str(e) or 'Failed.'
      self.set_status(msg.splitlines()[0])
      self.ui(self.show_error, 'Cannot prepare file', msg)
      self.root.after(0, lambda: self.play_btn.config(state='normal'))
      return
    except subprocess.CalledProcessError:
      self.set_status('ffmpeg failed — try without subtitles / fix audio.')
      self.root.after(0, lambda: self.play_btn.config(state='normal'))
      return

    self.playing, self.paused = True, False
    def started():
      self.play_btn.config(state='normal', text='⏸ Pause')
      self.set_status(f'Playing on {name}')
      # it worked — remember device, password and file for next launch
      self.cfg['device'] = name
      self.cfg['password'] = core.airplay_request.password
      self.cfg['last_file'] = str(self.file)
      save_config(self.cfg)
    self.ui(started)
    self.bg(self.poll_loop, dev)

  def poll_loop(self, dev):
    # read position/duration every 2s while playing
    import time
    _, ip, port = dev
    while self.playing:
      time.sleep(2)
      try:
        status, data, _ = core.airplay_request(ip, port, 'GET', '/scrub')
      except OSError:
        break
      if status != 200:
        continue
      import re
      d = dict(re.findall(r'(\w+): ([\d.]+)', data))
      pos, dur = float(d.get('position', 0)), float(d.get('duration', 0))
      def update():
        off = self.resume_offset  # live-subs stream starts at the resume point
        total = self.total_duration or (dur + off)  # ffprobe total, receiver fallback
        if dur > 0:
          self.duration = dur
          self.seek.config(to=max(total - off, dur))
          if not self.dragging_seek:
            self.seek.set(pos)
          # track position for the resume-on-next-play offer
          sp = str(self.file)
          self.cfg.setdefault('positions', {})[sp] = [pos + off, total]
          if self.recent.exists(sp):  # live-update this row's progress bar
            self.recent.item(sp, values=(
              f'{self.bar((pos + off) / total if total else 0)} {core.fmt_time(pos + off)}',))
        self.time_label.config(text=f'{core.fmt_time(pos + off)} / {core.fmt_time(total)}')
      self.ui(update)

  def on_seek(self, event):
    self.dragging_seek = False
    dev = self.current_device()
    if dev and self.playing and self.duration:
      self.bg(core.airplay_request, dev[1], dev[2], 'POST',
              f'/scrub?position={float(self.seek.get()):.3f}')

  def kill_transcoder(self):
    if self.transcoder:
      self.transcoder.kill()
      self.transcoder = None

  def ask_password(self, dev):
    pw = simpledialog.askstring(
      'AirPlay password',
      f'{dev[0]} requires a password (HTTP 401).\n'
      'It is set on the projector in AirPin: Settings → password/verification.\n'
      'Type it here — or Cancel, disable it on the projector, and hit Play again.',
      show='*', parent=self.root)
    if pw:
      core.airplay_request.password = pw
      self.play_btn.config(state='disabled')
      self.bg(self.start_playback, *self._play_args)

  def show_error(self, title, text):
    # error window with a copy button so the details can be pasted verbatim
    win = tk.Toplevel(self.root)
    win.title(title)
    box = tk.Text(win, width=72, height=14, wrap='word')
    box.insert('1.0', text)
    box.config(state='disabled')
    box.pack(fill='both', expand=True, padx=10, pady=10)
    def copy():
      self.root.clipboard_clear()
      self.root.clipboard_append(text)
      copy_btn.config(text='Copied ✓')
    copy_btn = ttk.Button(win, text='Copy error', command=copy)
    copy_btn.pack(side='left', padx=10, pady=(0, 10))
    ttk.Button(win, text='Close', command=win.destroy).pack(side='right', padx=10, pady=(0, 10))

  def stop(self):
    dev = self.current_device()
    self.playing, self.paused = False, False
    self.play_btn.config(text='▶ Play')
    self.seek.set(0)
    self.set_status('Stopped.')
    self.kill_transcoder()
    save_config(self.cfg)  # keep the resume position
    if dev:
      self.bg(core.airplay_request, dev[1], dev[2], 'POST', '/stop')

  def on_close(self):
    self.kill_transcoder()
    save_config(self.cfg)  # keep the resume position
    if self.playing:
      dev = self.current_device()
      if dev:
        try:
          core.airplay_request(dev[1], dev[2], 'POST', '/stop')
        except OSError:
          pass
    self.root.destroy()


def main():
  root = ROOT_CLS()
  App(root, sys.argv[1] if len(sys.argv) > 1 else None)
  root.mainloop()


if __name__ == '__main__':
  main()
