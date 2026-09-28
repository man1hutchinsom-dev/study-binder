"""Study Binder desktop app.

Runs a small private web server on this computer (127.0.0.1 only) that shows the
Study Binder page in its own window and saves every change to real files:

    Documents\\Study Binder\\study-binder.json      your topics and notes
    Documents\\Study Binder\\voice-notes.json       the last voice recording
    Documents\\Study Binder\\backups\\               automatic dated copies

Program files, settings (theme, Google API key) and the downloaded app page live
in %LOCALAPPDATA%\\Study Binder. Only the Python standard library is used.
"""

import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_ID = 'study-binder-desktop'
DEFAULT_PORT = 47815
UPDATE_BASE = os.environ.get('SB_UPDATE_URL', 'https://man1hutchinsom-dev.github.io/study-binder/')
IDLE_SECONDS = int(os.environ.get('SB_IDLE_SECONDS', '600'))   # quit this long after the window closes
UPDATE_EVERY = int(os.environ.get('SB_UPDATE_EVERY', '300'))            # seconds between update checks
MAX_BODY = 60 * 1024 * 1024
IS_WINDOWS = os.name == 'nt'
HERE = Path(__file__).resolve().parent

# Which saved values go to which file. Anything else goes to settings.json.
DATA_FILES = {
    'binder-data': 'study-binder.json',
    'binder-voice': 'voice-notes.json',
    'binder-data-damaged-copy': 'damaged-copy.txt',
}
JSON_KEYS = {'binder-data', 'binder-voice'}
MAIN_KEY = 'binder-data'


# ---------------------------------------------------------------- folders

def known_folder(name):
    """Real path of Documents / Desktop / Downloads on Windows, even when OneDrive moved them."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            guids = {
                'Documents': '{FDD39AD0-238F-46AF-ADB4-6C85480369C7}',
                'Desktop': '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',
                'Downloads': '{374DE290-123F-4565-9164-39C4925E467B}',
            }

            class GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                            ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

            guid = GUID()
            ctypes.oledll.ole32.CLSIDFromString(guids[name], ctypes.byref(guid))
            ptr = ctypes.c_wchar_p()
            ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr))
            path = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            if path:
                return Path(path)
        except Exception:
            pass
    return Path.home() / name


def local_home():
    if os.environ.get('SB_HOME'):
        return Path(os.environ['SB_HOME'])
    if IS_WINDOWS and os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA']) / 'Study Binder'
    return Path.home() / '.study-binder'


HOME = local_home()
CONFIG_FILE = HOME / 'config.json'
SETTINGS_FILE = HOME / 'settings.json'
RUN_FILE = HOME / 'run.json'
LOG_FILE = HOME / 'log.txt'
CACHE_HTML = HOME / 'cache' / 'index.html'


def log(*parts):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE.with_suffix('.old.txt'))
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(p) for p in parts) + '\n')
    except Exception:
        pass


def show_error(message):
    log('ERROR', message)
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Study Binder', 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def read_json_file(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def default_data_dir():
    return known_folder('Documents') / 'Study Binder'


def load_data_dir():
    if os.environ.get('SB_DATA_DIR'):
        return Path(os.environ['SB_DATA_DIR'])
    cfg = read_json_file(CONFIG_FILE, {}) or {}
    if isinstance(cfg.get('data_dir'), str) and cfg['data_dir']:
        return Path(cfg['data_dir'])
    return default_data_dir()


def onedrive_root():
    for var in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        p = os.environ.get(var)
        if p and Path(p).is_dir():
            return Path(p)
    if os.environ.get('SB_ONEDRIVE'):
        return Path(os.environ['SB_ONEDRIVE'])
    return None


def is_inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- safe writing

def atomic_write(path, text):
    """Write the whole file or nothing: temp file, flush to disk, swap in, read back to check."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + str(os.getpid()) + '.tmp')
    raw = text.encode('utf-8')
    with open(tmp, 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    last = None
    for attempt in range(15):   # OneDrive or antivirus can hold the file for a moment
        try:
            os.replace(tmp, path)
            last = None
            break
        except PermissionError as e:
            last = e
            time.sleep(0.05 * (attempt + 1))
    if last:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise last
    with open(path, 'rb') as f:
        if f.read() != raw:
            raise IOError('the file on disk did not match after saving')


def stamp_now():
    return datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')


# ---------------------------------------------------------------- the store

class Store:
    """All saved values, in memory and on disk. Every change is written before it is confirmed."""

    def __init__(self, data_dir):
        self.lock = threading.RLock()
        self.data_dir = Path(data_dir)
        self.values = {}
        self.rev = int(time.time() * 1000)   # keeps growing across restarts, so open windows catch up
        self.changes = []        # (rev, key, client)
        self.notice = None       # shown once in the page, e.g. after recovering from a backup
        self.last_saved = 0
        self.last_backup_at = 0
        self.load()

    # -- paths
    def file_for(self, key):
        if key in DATA_FILES:
            return self.data_dir / DATA_FILES[key]
        return None

    @property
    def backup_dir(self):
        return self.data_dir / 'backups'

    # -- loading
    def load(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        for tmp in self.data_dir.glob('.*.tmp'):   # left over from a save that was cut off; the real file is untouched
            try:
                tmp.unlink()
            except Exception:
                pass
        settings = read_json_file(SETTINGS_FILE, {})
        if isinstance(settings, dict):
            self.values.update({k: v for k, v in settings.items() if isinstance(v, str) and k not in DATA_FILES})
        for key in DATA_FILES:
            path = self.file_for(key)
            if not path.exists():
                continue
            text = path.read_bytes().decode('utf-8', errors='replace')
            if key == MAIN_KEY:
                text = self.check_main_file(path, text)
                if text is None:
                    continue
            elif key in JSON_KEYS:
                try:
                    json.loads(text)
                except Exception:
                    log('unreadable', path)
                    self.keep_damaged(path)
                    continue
            self.values[key] = text
        if MAIN_KEY in self.values:
            self.make_backup(self.values[MAIN_KEY], reason='start')

    def keep_damaged(self, path):
        target = path.with_name(path.stem + '.unreadable-' + stamp_now() + path.suffix)
        os.replace(path, target)
        return target

    def check_main_file(self, path, text):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get('topics'), list) and isinstance(obj.get('notes'), list):
                return text
        except Exception:
            pass
        # The binder file can't be read. Keep it, and bring back the newest good backup.
        damaged = self.keep_damaged(path)
        log('main file unreadable, kept as', damaged)
        for b in self.list_backups():
            try:
                btext = b.read_text(encoding='utf-8')
                obj = json.loads(btext)
                if isinstance(obj.get('topics'), list) and isinstance(obj.get('notes'), list):
                    atomic_write(path, btext)
                    self.notice = {'kind': 'recovered', 'backup': b.name, 'damaged': damaged.name,
                                   'when': b.stat().st_mtime * 1000}
                    return btext
            except Exception:
                continue
        # No good backup: hand the damaged text to the page, which keeps a copy and pauses saving.
        self.notice = {'kind': 'damaged', 'damaged': damaged.name}
        return text

    # -- saving
    def validate(self, key, value):
        if key in JSON_KEYS:
            obj = json.loads(value)   # raises if broken
            if key == MAIN_KEY and not (isinstance(obj, dict) and isinstance(obj.get('topics'), list)
                                        and isinstance(obj.get('notes'), list)):
                raise ValueError('not a Study Binder')
            return obj
        return None

    @staticmethod
    def counts(obj):
        return (len(obj['topics']), len(obj['notes'])) if isinstance(obj, dict) else (0, 0)

    def set(self, key, value, client=''):
        if not isinstance(key, str) or not key.startswith('binder-') or len(key) > 80:
            raise ValueError('unknown key')
        if not isinstance(value, str):
            raise ValueError('value must be text')
        with self.lock:
            if self.values.get(key) == value:
                return self.rev
            new_obj = self.validate(key, value) if key != 'binder-data-damaged-copy' else None
            if key == MAIN_KEY and key in self.values:
                # About to lose a topic or several notes (deleting, restoring a backup): keep a copy first.
                try:
                    old_t, old_n = self.counts(json.loads(self.values[key]))
                    new_t, new_n = self.counts(new_obj)
                    if new_t < old_t or new_n <= old_n - 2:
                        self.make_backup(self.values[key], reason='before removing notes')
                except Exception:
                    pass
            path = self.file_for(key)
            if path:
                atomic_write(path, value)
            else:
                self.write_settings({**self.values, key: value})
            self.values[key] = value
            self.bump(key, client)
            if key == MAIN_KEY:
                self.last_saved = time.time()
                if time.time() - self.last_backup_at > 30 * 60:
                    self.make_backup(value, reason='hourly')
            return self.rev

    def remove(self, key, client=''):
        with self.lock:
            if key not in self.values:
                return self.rev
            if key == MAIN_KEY:
                raise ValueError('the binder file is never deleted')
            path = self.file_for(key)
            if path:
                if path.exists():
                    path.unlink()
            else:
                rest = dict(self.values)
                rest.pop(key, None)
                self.write_settings(rest)
            self.values.pop(key, None)
            self.bump(key, client)
            return self.rev

    def write_settings(self, values):
        settings = {k: v for k, v in values.items() if k not in DATA_FILES}
        atomic_write(SETTINGS_FILE, json.dumps(settings, indent=1, ensure_ascii=False))

    def bump(self, key, client):
        self.rev += 1
        self.changes.append((self.rev, key, client))
        del self.changes[:-500]

    def changes_since(self, since, client):
        with self.lock:
            oldest = self.changes[0][0] if self.changes else self.rev + 1
            if since < oldest - 1:
                keys = set(self.values)   # too far behind: send everything
            else:
                keys = {k for r, k, c in self.changes if r > since and c != client}
            return self.rev, {k: self.values.get(k) for k in keys}

    # -- backups
    def list_backups(self):
        try:
            files = [p for p in self.backup_dir.glob('study-binder-*.json') if p.is_file()]
        except Exception:
            return []
        return sorted(files, key=lambda p: p.name, reverse=True)

    def make_backup(self, text, reason=''):
        """Keep a dated copy. Skipped when the newest copy is already identical."""
        with self.lock:
            try:
                backups = self.list_backups()
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if backups:
                    newest = backups[0].read_bytes()
                    if hashlib.sha256(newest).hexdigest() == digest:
                        self.last_backup_at = time.time()
                        return backups[0]
                target = self.backup_dir / ('study-binder-' + stamp_now() + '.json')
                atomic_write(target, text)
                self.last_backup_at = time.time()
                log('backup', target.name, reason)
                self.prune_backups()
                return target
            except Exception as e:
                log('backup failed', e)
                return None

    def prune_backups(self):
        """Keep every copy from the last 2 days, one a day for 60 days, then one a month."""
        now = datetime.datetime.now()
        kept_days, kept_months = set(), set()
        for p in self.list_backups():   # newest first
            m = re.match(r'study-binder-(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})\.json$', p.name)
            if not m:
                continue
            try:
                when = datetime.datetime.strptime(m.group(1) + m.group(2) + m.group(3) + m.group(4), '%Y-%m-%d%H%M%S')
            except ValueError:
                continue
            age = now - when
            day, month = m.group(1), m.group(1)[:7]
            if age <= datetime.timedelta(days=2):
                keep = True
            elif age <= datetime.timedelta(days=60):
                keep = day not in kept_days
            else:
                keep = month not in kept_months
            kept_days.add(day)
            kept_months.add(month)
            if not keep:
                try:
                    p.unlink()
                except Exception:
                    pass

    # -- moving to OneDrive
    def move_to(self, target_dir):
        target_dir = Path(target_dir)
        with self.lock:
            if (target_dir / DATA_FILES[MAIN_KEY]).exists():
                raise ValueError('There is already a Study Binder folder at ' + str(target_dir) + '. Nothing was moved.')
            old = self.data_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for item in old.iterdir():
                if item.name.endswith('.tmp'):
                    continue
                dest = target_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
            for key in DATA_FILES:   # check every data file arrived intact
                src, dst = old / DATA_FILES[key], target_dir / DATA_FILES[key]
                if src.exists() and src.read_bytes() != dst.read_bytes():
                    raise IOError('The copy in the new folder did not match. Nothing was changed.')
            cfg = read_json_file(CONFIG_FILE, {}) or {}
            cfg['data_dir'] = str(target_dir)
            atomic_write(CONFIG_FILE, json.dumps(cfg, indent=1))
            self.data_dir = target_dir
            try:
                (old / 'MOVED - read me.txt').write_text(
                    'Your Study Binder now saves to:\r\n' + str(target_dir) + '\r\n\r\n'
                    'This folder is an old copy from before the move. You can keep it or delete it.\r\n',
                    encoding='utf-8')
            except Exception:
                pass
            log('moved data to', target_dir)


# ---------------------------------------------------------------- finding old backups

def find_old_backups(store):
    places = [known_folder('Downloads'), known_folder('Documents'), known_folder('Desktop')]
    od = onedrive_root()
    if od:
        places += [od, od / 'Documents', od / 'Desktop']
    seen, found = set(), []
    for folder in places:
        try:
            files = list(Path(folder).glob('study-binder*.json'))
        except Exception:
            continue
        for p in files:
            try:
                rp = p.resolve()
                if rp in seen or is_inside(rp, store.data_dir) or p.stat().st_size > 20 * 1024 * 1024:
                    continue
                seen.add(rp)
                obj = json.loads(p.read_text(encoding='utf-8'))
                if not (isinstance(obj.get('topics'), list) and isinstance(obj.get('notes'), list)):
                    continue
                found.append({'path': str(p), 'name': p.name, 'modified': p.stat().st_mtime * 1000,
                              'topics': len(obj['topics']), 'notes': len(obj['notes'])})
            except Exception:
                continue
    found.sort(key=lambda f: f['modified'], reverse=True)
    return found[:8]


# ---------------------------------------------------------------- app page and updates

VERSION_RE = re.compile(r"const APP_VERSION = '([^']+)'")


def html_version(text):
    m = VERSION_RE.search(text or '')
    return m.group(1) if m else ''


def version_key(v):
    m = re.match(r'(\d{4}-\d{2}-\d{2})(?:\.(\d+))?', v or '')
    return (m.group(1), int(m.group(2) or 0)) if m else ('', 0)


def page_is_usable(text):
    return bool(text) and '</html>' in text and '__SB_DESKTOP__' in text and html_version(text) != '' and len(text) > 20000


class AppPage:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_check = 0
        self.text = ''
        self.choose()

    def choose(self):
        best = ''
        for path in (HERE / 'index.html', CACHE_HTML):
            try:
                t = path.read_text(encoding='utf-8')
            except Exception:
                continue
            if page_is_usable(t) and version_key(html_version(t)) > version_key(html_version(best)):
                best = t
        if not best:
            raise RuntimeError('The Study Binder page is missing. Please run the installer again.')
        self.text = best

    @property
    def version(self):
        return html_version(self.text)

    def check_for_update(self, force=False):
        """Download a newer page from the website if there is one. Keeps the current page if anything fails."""
        with self.lock:
            if not force and time.time() - self.last_check < UPDATE_EVERY:
                return
            self.last_check = time.time()
        try:
            remote = json.loads(fetch(UPDATE_BASE + 'version.json?t=' + str(int(time.time()))))['version']
            if version_key(remote) <= version_key(self.version):
                return
            text = fetch(UPDATE_BASE + 'index.html?v=' + remote)
            if not page_is_usable(text) or html_version(text) != remote:
                log('update skipped: downloaded page failed checks', remote)
                return
            atomic_write(CACHE_HTML, text)
            with self.lock:
                self.text = text
            log('updated page to', remote)
        except Exception as e:
            log('update check failed:', e)


def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'StudyBinderDesktop', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode('utf-8')


# ---------------------------------------------------------------- web server

class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows this option would let a second program share the port, so leave it off there.
    allow_reuse_address = not IS_WINDOWS

class App:
    def __init__(self, port):
        self.port = port
        self.token = load_token()
        self.store = Store(load_data_dir())
        self.page = AppPage()
        self.last_seen = time.time()
        self.found_cache = None
        self.server = None

    def info(self):
        s = self.store
        od = onedrive_root()
        backups = s.list_backups()
        return {
            'dataDir': str(s.data_dir),
            'backupDir': str(s.backup_dir),
            'backupCount': len(backups),
            'lastBackup': backups[0].stat().st_mtime * 1000 if backups else 0,
            'lastSaved': s.last_saved * 1000,
            'inOneDrive': bool(od and is_inside(s.data_dir, od)),
            'canMoveToOneDrive': bool(od and not is_inside(s.data_dir, od)),
            'oneDriveTarget': str(od / 'Study Binder') if od else '',
        }

    def boot_payload(self):
        with self.store.lock:
            store = dict(self.store.values)
            rev = self.store.rev
        return {'token': self.token, 'rev': rev, 'store': store, 'notice': self.store.notice,
                'info': self.info(), 'version': self.page.version}

    def render_page(self):
        payload = json.dumps(self.boot_payload(), ensure_ascii=False)
        payload = payload.replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
        inject = '<script>window.__SB_DESKTOP__ = ' + payload + ';</script>'
        text = self.page.text
        i = text.find('<head>')
        return text[:i + 6] + inject + text[i + 6:] if i >= 0 else inject + text


def make_handler(app):
    allowed_hosts = {'127.0.0.1:%d' % app.port, 'localhost:%d' % app.port}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, ctype='application/json; charset=utf-8'):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode('utf-8')
            self.send_response(code)
            if code != 200:
                self.close_connection = True   # a refused request's body may not have been read
                self.send_header('Connection', 'close')
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self):
            # blocks other websites reaching this server through tricks with web addresses
            return self.headers.get('Host', '') in allowed_hosts

        def token_ok(self, query):
            got = self.headers.get('X-SB-Token') or (query.get('token') or [''])[0]
            return secrets.compare_digest(got.encode('utf-8'), app.token.encode('utf-8'))

        def read_body(self):
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                raise ValueError('too large')
            raw = self.rfile.read(n) if n else b''
            return json.loads(raw.decode('utf-8')) if raw else {}

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {'ok': False})
            url = urlparse(self.path)
            q = parse_qs(url.query)
            app.last_seen = time.time()
            if url.path in ('/', '/index.html'):
                return self.send(200, app.render_page(), 'text/html; charset=utf-8')
            if url.path == '/version.json':
                t = threading.Thread(target=app.page.check_for_update, daemon=True)
                t.start()
                t.join(8)   # the page asks in the background, so it can wait for a download
                return self.send(200, {'version': app.page.version})
            if url.path == '/favicon.ico':
                try:
                    return self.send(200, (HERE / 'study-binder.ico').read_bytes(), 'image/x-icon')
                except Exception:
                    return self.send(404, b'', 'text/plain')
            if url.path == '/api/hello':
                return self.send(200, {'app': APP_ID})
            if not self.token_ok(q):
                return self.send(403, {'ok': False, 'error': 'not allowed'})
            if url.path == '/api/poll':
                since = int((q.get('since') or ['0'])[0] or 0)
                rev, changed = app.store.changes_since(since, (q.get('client') or [''])[0])
                return self.send(200, {'ok': True, 'rev': rev, 'changed': changed})
            if url.path == '/api/info':
                return self.send(200, {'ok': True, 'info': app.info()})
            if url.path == '/api/found-backups':
                if app.found_cache is None:
                    app.found_cache = find_old_backups(app.store)
                return self.send(200, {'ok': True, 'found': app.found_cache})
            return self.send(404, {'ok': False})

        def do_POST(self):
            if not self.host_ok():
                return self.send(403, {'ok': False})
            url = urlparse(self.path)
            q = parse_qs(url.query)
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://' + h for h in allowed_hosts):
                return self.send(403, {'ok': False})
            if not self.token_ok(q):
                return self.send(403, {'ok': False, 'error': 'not allowed'})
            app.last_seen = time.time()
            try:
                body = self.read_body()
                s = app.store
                if url.path == '/api/set':
                    rev = s.set(body.get('key'), body.get('value'), str(body.get('client', '')))
                    return self.send(200, {'ok': True, 'rev': rev})
                if url.path == '/api/remove':
                    rev = s.remove(body.get('key'), str(body.get('client', '')))
                    return self.send(200, {'ok': True, 'rev': rev})
                if url.path == '/api/open-folder':
                    folder = s.backup_dir if body.get('which') == 'backups' else s.data_dir
                    open_folder(folder)
                    return self.send(200, {'ok': True})
                if url.path == '/api/backup-now':
                    text = s.values.get(MAIN_KEY)
                    made = s.make_backup(text, reason='button') if text else None
                    return self.send(200, {'ok': bool(made), 'info': app.info()})
                if url.path == '/api/read-found':
                    paths = {f['path'] for f in (app.found_cache or [])}
                    p = body.get('path')
                    if p not in paths:
                        return self.send(403, {'ok': False, 'error': 'not allowed'})
                    return self.send(200, {'ok': True, 'text': Path(p).read_text(encoding='utf-8')})
                if url.path == '/api/move-to-onedrive':
                    od = onedrive_root()
                    if not od:
                        raise ValueError('OneDrive was not found on this computer.')
                    s.move_to(od / 'Study Binder')
                    return self.send(200, {'ok': True, 'info': app.info()})
                if url.path == '/api/dismiss-notice':
                    s.notice = None
                    return self.send(200, {'ok': True})
                if url.path == '/api/quit':
                    self.send(200, {'ok': True})
                    threading.Thread(target=app.server.shutdown, daemon=True).start()
                    return
                return self.send(404, {'ok': False})
            except Exception as e:
                log('request failed', url.path, repr(e))
                return self.send(500, {'ok': False, 'error': str(e)})

    return Handler


def load_token():
    """Secret that proves a request comes from the Study Binder window. Kept across restarts so an
    open window carries on working if the app is restarted."""
    path = HOME / 'secret.txt'
    try:
        t = path.read_text(encoding='utf-8').strip()
        if len(t) >= 32:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(32)
    atomic_write(path, t)
    return t


def open_folder(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(folder))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(folder)])
    else:
        subprocess.Popen(['xdg-open', str(folder)])


# ---------------------------------------------------------------- window

def find_browser():
    if not IS_WINDOWS:
        return None
    roots = [os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')]
    names = [r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe']
    for name in names:
        for root in roots:
            if root and Path(root, name).exists():
                return str(Path(root, name))
    return None


def open_window(url):
    if os.environ.get('SB_NO_WINDOW'):
        return
    exe = find_browser()
    if exe:
        try:
            subprocess.Popen([exe, '--app=' + url, '--user-data-dir=' + str(HOME / 'window'),
                              '--no-first-run', '--no-default-browser-check', '--window-size=1400,900'])
            return
        except Exception as e:
            log('could not open app window', e)
    webbrowser.open(url)


LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never send local calls via a proxy


def already_running(port):
    try:
        with LOCAL_OPENER.open('http://127.0.0.1:%d/api/hello' % port, timeout=2) as r:
            return json.loads(r.read().decode('utf-8')).get('app') == APP_ID
    except Exception:
        return False


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if not IS_WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def watch_idle(app):
    """Quit a few minutes after the window is closed. Sleep/hibernate doesn't count as idle."""
    last_tick = time.time()
    while True:
        time.sleep(5)
        now = time.time()
        if now - last_tick > 30:   # the computer was asleep: give the window time to wake up
            app.last_seen = now
        last_tick = now
        if now - app.last_seen > IDLE_SECONDS:
            log('window closed, stopping')
            app.server.shutdown()
            return


def main():
    HOME.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get('SB_PORT') or DEFAULT_PORT)
    info = read_json_file(RUN_FILE, {}) or {}
    for p in [port] + ([info['port']] if isinstance(info.get('port'), int) else []):
        if already_running(p):
            open_window('http://127.0.0.1:%d/' % p)
            return
    if not port_free(port):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
    try:
        app = App(port)
    except Exception as e:
        log(traceback.format_exc())
        show_error('Study Binder could not start.\n\n' + str(e))
        return
    server = Server(('127.0.0.1', port), make_handler(app))
    app.server = server
    atomic_write(RUN_FILE, json.dumps({'port': port, 'pid': os.getpid()}))
    log('started on port', port, 'data in', app.store.data_dir, 'page', app.page.version)
    threading.Thread(target=watch_idle, args=(app,), daemon=True).start()
    threading.Thread(target=app.page.check_for_update, kwargs={'force': True}, daemon=True).start()
    open_window('http://127.0.0.1:%d/' % port)
    try:
        server.serve_forever()
    finally:
        app.store.lock.acquire()   # wait for any save in progress, and let no new one start
        try:
            RUN_FILE.unlink()
        except Exception:
            pass
        log('stopped')


if __name__ == '__main__':
    main()
