"""Installs or updates the Study Binder desktop app on Windows.

Run by "Install Study Binder.bat". Copies the program to %LOCALAPPDATA%\\Study Binder\\app,
adds Study Binder to the desktop and Start menu, and opens it. Your notes in
Documents\\Study Binder are never touched.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent          # the desktop folder of the downloaded copy
REPO_ROOT = SRC.parent
HOME = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'Study Binder'
APP = HOME / 'app'
FILES = [(SRC / 'study_binder.pyw', 'study_binder.pyw'),
         (SRC / 'study-binder.ico', 'study-binder.ico'),
         (REPO_ROOT / 'index.html', 'index.html')]
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(text=''):
    print('  ' + text if text else '')


def copy_file(src, dest):
    data = src.read_bytes()
    tmp = dest.with_name(dest.name + '.new')
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def stop_running_app():
    """Ask an open Study Binder to finish saving and stop, so its program file can be replaced."""
    try:
        port = json.loads((HOME / 'run.json').read_text(encoding='utf-8'))['port']
        token = (HOME / 'secret.txt').read_text(encoding='utf-8').strip()
    except Exception:
        return
    try:
        req = urllib.request.Request('http://127.0.0.1:%d/api/quit' % port, data=b'{}', method='POST',
                                     headers={'X-SB-Token': token, 'Content-Type': 'application/json'})
        LOCAL.open(req, timeout=5).read()
    except Exception:
        return
    for _ in range(20):
        time.sleep(0.25)
        try:
            LOCAL.open('http://127.0.0.1:%d/api/hello' % port, timeout=1).read()
        except Exception:
            return


def make_shortcut(link, target, args, icon, workdir):
    env = dict(os.environ, SB_LNK=str(link), SB_TARGET=str(target), SB_ARGS=args,
               SB_ICON=str(icon) + ',0', SB_WD=str(workdir))
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SB_LNK); "
              "$s.TargetPath = $env:SB_TARGET; $s.Arguments = $env:SB_ARGS; "
              "$s.IconLocation = $env:SB_ICON; $s.WorkingDirectory = $env:SB_WD; "
              "$s.Description = 'Study Binder - your notes, saved on this PC'; $s.Save()")
    subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                   env=env, check=True, capture_output=True)


def special_folder(name):
    out = subprocess.run(['powershell', '-NoProfile', '-Command', "[Environment]::GetFolderPath('%s')" % name],
                         capture_output=True, text=True)
    p = out.stdout.strip()
    return Path(p) if p else None


def main():
    say()
    if sys.version_info < (3, 8):
        say('Study Binder needs Python 3.8 or newer. This computer has Python %d.%d.' % sys.version_info[:2])
        say('Install the latest Python from python.org, then run this setup again.')
        return 1
    for src, _ in FILES:
        if not src.exists():
            say('Setup files are missing (%s). Please download the setup again.' % src.name)
            return 1

    say('1. Copying the program...')
    stop_running_app()
    APP.mkdir(parents=True, exist_ok=True)
    for src, name in FILES:
        copy_file(src, APP / name)

    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    program = APP / 'study_binder.pyw'
    icon = APP / 'study-binder.ico'

    say('2. Adding Study Binder to your desktop and Start menu...')
    made = []
    for folder in (special_folder('Desktop'), special_folder('Programs')):
        if not folder:
            continue
        try:
            make_shortcut(folder / 'Study Binder.lnk', pythonw, '"%s"' % program, icon, APP)
            made.append(folder)
        except Exception as e:
            say('   (Could not add a shortcut in %s: %s)' % (folder, e))
    if not made:
        say('   Shortcuts could not be made. You can open Study Binder by double-clicking:')
        say('   ' + str(program))

    say('3. Opening Study Binder...')
    flags = 0x00000008 | 0x00000200   # run on its own, separate from this window
    subprocess.Popen([str(pythonw), str(program)], cwd=str(APP), creationflags=flags, close_fds=True)
    say()
    say('Done! Study Binder is on your desktop.')
    say('Your notes will be saved in:  Documents\\Study Binder')
    say()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        say('Something went wrong: %s' % e)
        sys.exit(1)
