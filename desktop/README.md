# Study Binder desktop app (Windows)

The same Study Binder page, in its own window, saving every change to real files:

| What | Where |
|---|---|
| Topics and notes | `Documents\Study Binder\study-binder.json` (same format as a ⬇ Backup file) |
| Last voice recording | `Documents\Study Binder\voice-notes.json` |
| Automatic backup copies | `Documents\Study Binder\backups\` (every half hour while working, at each start, and before anything removes a topic or several notes; all kept for 2 days, then one a day for 60 days, then one a month) |
| Program, settings, Google API key | `%LOCALAPPDATA%\Study Binder\` |

## Install or update
Download `Install Study Binder.bat` and double-click it. It needs Python 3.8+ (already on the PC). It downloads this repo, copies the program to `%LOCALAPPDATA%\Study Binder\app`, adds a desktop and Start menu icon, and opens the app. It never touches the notes.

## How it works
- `study_binder.pyw` (standard library only) runs a private web server on `127.0.0.1:47815` and opens it in a Microsoft Edge app window.
- The server adds `window.__SB_DESKTOP__` to the page. `index.html` then saves through the server (`appStore`) instead of browser storage. Each save is written to a temp file, flushed to disk, swapped in, and read back before the page is told it worked; a failed save shows the red warning.
- Requests need a secret token and the right `Host`, so other websites can't reach the notes.
- Updates: the server downloads a newer `index.html` from GitHub Pages when `version.json` changes, checks it, and keeps the last good copy for offline use. Publishing the site updates the app too.
- The server stops by itself 10 minutes after the window is closed.
- If `study-binder.json` can't be read, it's kept (renamed `.unreadable-…`) and the newest good backup is brought back.
