# Tradegate

Auto-login launcher for trading platforms. Stores credentials securely and automates the login form fill so you can get into your trading platform with a single command.

## Supported Platforms

| Platform | Binary | Notes |
|----------|--------|-------|
| **Interactive Brokers TWS** | `~/Jts/tws` | Java Swing app |
| **tastytrade** | `/opt/tastytrade/bin/tastytrade` | Electron-based |
| **thinkorswim** | `~/thinkorswim/thinkorswim` | Charles Schwab; two-page login (Login ID, then password) |

## Requirements

- Python 3.11+
- A system keyring (gnome-keyring, macOS Keychain, or Windows Credential Manager)
- **Linux**: X11 and `xdotool` (finds, focuses and types into the login window); optionally `tesseract-ocr` and ImageMagick for pre-filled username detection
- **macOS**: Screen Recording permission for pyautogui
- **Windows**: No extra dependencies

## Installation

```bash
pip install .
```

## Usage

```bash
# Store credentials for a platform
tradegate add tws --username myuser

# Launch and auto-login
tradegate launch tws

# Launch without auto-submitting the login form
tradegate launch tws --no-submit

# List stored accounts
tradegate list

# Open the account picker GUI
tradegate manage

# Remove stored credentials
tradegate remove tws myuser

# Debug: dump AT-SPI accessibility tree (Linux)
tradegate inspect tws
```

Pass `-v` for verbose/debug logging (shows timings, OCR output, window detection details).

## Configuration

Config lives at:
- **Linux**: `~/.config/tradegate/config.toml`
- **macOS**: `~/Library/Application Support/tradegate/config.toml`
- **Windows**: `%APPDATA%\tradegate\config.toml`

Example:

```toml
[general]
credential_backend = "keyring"   # "keyring", "encrypted_file", or "both"
input_strategy = "auto"          # "auto" (xdotool on Linux, pyautogui elsewhere), "pyautogui", "xdotool", "atspi"
auto_submit = false

[platforms.tws]
binary = "~/Jts/tws"
window_timeout = 120
login_ready_delay = 1
field_order = ["username", "password"]
focus_shield = "enforce"   # "off", "log", or "enforce" — see below
restore_focus = true       # hand focus back after login — see below

[platforms.thinkorswim]
binary = "~/thinkorswim/thinkorswim"
wm_class = "java-lang-Thread"
title_pattern = "Logon to thinkorswim"
update_timeout = 1800      # keep waiting this long while the TOS updater runs
```

Settings saved in your config override the built-in defaults. A config
written by an older tradegate may hold `wm_class = "thinkorswim-Launcher"`
and `binary = ""` for thinkorswim; those match no window, so replace them
with the values above.

## thinkorswim

The thinkorswim login has two pages: Login ID, then password. The form
is a Chromium window embedded in the "Logon to thinkorswim" frame, and its
title changes from `Charles Schwab Log In` to `Password` on the second
page. tradegate fills the Login ID and submits it (the password page can't
be reached otherwise), waits for the title change, then fills the password.
`auto_submit` only controls the final Log In.

thinkorswim may update itself before showing the login page. While the
updater window is on screen, tradegate keeps waiting past
`window_timeout`, up to `update_timeout` seconds.

The two-page flow uses `xdotool`, so it is Linux only, and it does not
handle a saved Login ID that skips the first page.

## Focus Shield (Linux/X11)

TWS raises its main window and steals keyboard focus when the main window
opens, when it reconnects to IB servers, and on its nightly auto-restart.
There is no TWS setting for this, and the window manager cannot block it:
Java/AWT takes focus via `XSetInputFocus`, which is not WM-interceptable.

The focus shield is an `LD_PRELOAD` library (`native/twsquiet.c`) that
tradegate injects into the TWS process at launch. TWS takes focus by
calling `XSetInputFocus` on its AWT focus proxy; the call may use either
`CurrentTime` or a fresh server timestamp the JVM fetches itself, so the
timestamp does not tell you whether you asked for the focus change. The
shield instead gates on evidence of user intent: it allows the action only
when a `WM_TAKE_FOCUS` message just arrived (the window manager granting
focus after you clicked the window or its title bar) **and** there was a
recent click or window-switch key (Alt/Super/Tab). The same gate covers every way TWS
takes over the screen:

- **focus**: `XSetInputFocus` on the AWT focus proxy is dropped when
  self-initiated;
- **raise / "pulls to front"**: `XRaiseWindow`, `XMapRaised`, and
  `XConfigureWindow` stack-above requests are dropped when self-initiated;
- **map-time focus**: new TWS windows are marked `_NET_WM_USER_TIME=0` so
  the WM does not focus them on map;
- **windows covering your work**: when TWS opens a window on its own
  (launch, reconnect, auto-restart), the shim sets `_NET_WM_STATE_BELOW` on
  it *before* it maps, so the window manager places it at the back with no
  flash, and the blocked raise (above) keeps it there. AWT re-manages
  `_NET_WM_STATE` (it maximizes), which can wipe the flag, so the shim's
  monitor thread re-adds it whenever AWT clears it. The instant you click a
  TWS window's taskbar entry or Alt-Tab to it, the flag is released so it
  comes to the front on demand. tradegate separately un-parks the login
  window so you can log in, and clicks it (a real XTEST click via
  `xdotool`) so the shield lets it take keyboard focus for the fill.

A takeover with none of the intent signals behind it — launch, reconnect,
auto-restart — is suppressed on all three vectors. Clicking into TWS,
alt-tabbing to it, and typing in it are unaffected.

Build once per machine (requires gcc and libX11 headers):

```bash
native/build.sh
```

Modes for `focus_shield` under `[platforms.tws]` (default: `enforce`):

- `off` — no injection
- `log` — observe and log TWS's raise/focus calls, block nothing
- `enforce` — block focus steals

Decisions are logged to `~/.local/state/tradegate/twsquiet.log` in both
active modes, so misbehavior is diagnosable from the log after the fact.
If the shield ever blocks something it shouldn't (or misses something),
switch to `log` mode to observe TWS's calls without interference. An
existing `LD_PRELOAD` (e.g. fakexrandr) is preserved, and a missing shim
library downgrades to a warning — TWS always launches.

### Watchdog fallback (optional, off by default)

The shield is the fix and should be sufficient. `restore_focus` is a
separate, reactive fallback for the case where focus is taken by a path the
shield does not sit on — the window manager itself activating a platform
window (focus succession, an attention request) rather than TWS calling
`XSetInputFocus`. Unlike the shield it is reactive: it lets the focus
change happen and then reverses it, so there is a brief flicker. Leave it
off unless you still see focus loss with the shield on; then set
`restore_focus = true`.

The watchdog runs for the whole platform session (until the platform
exits). It watches `_NET_ACTIVE_WINDOW` and, whenever a platform window
becomes active, checks for evidence you caused it:

- a mouse click in the last ~1.5s (clicking the window, its taskbar button,
  a notification), or
- an Alt/Super/Tab press in the same window (Alt-Tab and friends).

If neither is present, focus is snapped back to the last window you were
using. Plain typing is deliberately **not** treated as intent — keystrokes
landing in a window that just raised itself are the failure being
prevented. Input is read via XInput2 raw events, so a click anywhere counts,
not just clicks inside the window.

Limits: reverts are rate-limited (4/min, then a 5-minute pause) so the
watchdog never fights you; a focus switch made by neither click nor
switcher key (e.g. a custom `wmctrl` hotkey) will be reverted; and a real
steal within ~1.5s of an unrelated click will be allowed through. Decisions
are logged to `~/.local/state/tradegate/focuswatch.log`. Ctrl-C stops the
watchdog (login has already completed). Set `restore_focus_scope = "launch"`
to guard only for `window_timeout` seconds after launch instead of the whole
session.

The watchdog needs `python-xlib` (`pip install 'tradegate[linux]'`, or
`dnf install python3-xlib`). Without it, `restore_focus` logs that it is
disabled and does nothing; the focus shield is unaffected.

## Credential Storage

Two backends, usable independently or together:

- **System keyring** (default) — uses the OS credential store via the `keyring` library
- **Encrypted file** — AES-encrypted local file (Fernet + PBKDF2), protected by a master password

## How It Works

1. Looks up stored credentials for the chosen platform/account
2. Takes a per-platform lock; a second launch of the same platform while one
   is still filling exits with "launch already in progress"
3. Launches the trading platform binary
4. Waits for the login window to appear (matched by window class and title;
   only visible windows count, because Java apps keep hidden windows with
   the same title)
5. Activates and clicks the login window so it has keyboard focus
6. Optionally OCRs the window to detect a pre-filled username (skips re-typing)
7. Fills username and password via keyboard automation. On Linux, each
   keystroke is sent only if the login window is still the active window;
   if focus moves elsewhere the fill stops and reports failure instead of
   typing into another window. Linux types the text rather than pasting
   it, so the password never goes on the clipboard (macOS and Windows
   still paste through the clipboard).
8. Optionally submits the form

## License

Private.
