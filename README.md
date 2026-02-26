# Tradegate

Auto-login launcher for trading platforms. Stores credentials securely and automates the login form fill so you can get into your trading platform with a single command.

## Supported Platforms

| Platform | Binary | Notes |
|----------|--------|-------|
| **Interactive Brokers TWS** | `~/Jts/tws` | Java Swing app |
| **tastytrade** | `/opt/tastytrade/bin/tastytrade` | Electron-based |
| **thinkorswim** | *(configure in settings)* | Charles Schwab |

## Requirements

- Python 3.11+
- A system keyring (gnome-keyring, macOS Keychain, or Windows Credential Manager)
- **Linux**: `xdotool`, optionally `tesseract-ocr`, `xclip`
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
input_strategy = "auto"          # "auto", "pyautogui", "xdotool", "atspi"
auto_submit = false

[platforms.tws]
binary = "~/Jts/tws"
window_timeout = 120
login_ready_delay = 1
field_order = ["username", "password"]
```

## Credential Storage

Two backends, usable independently or together:

- **System keyring** (default) — uses the OS credential store via the `keyring` library
- **Encrypted file** — AES-encrypted local file (Fernet + PBKDF2), protected by a master password

## How It Works

1. Looks up stored credentials for the chosen platform/account
2. Launches the trading platform binary
3. Waits for the login window to appear (matched by window class and title)
4. Optionally OCRs the window to detect a pre-filled username (skips re-typing)
5. Fills username and password via keyboard automation (clipboard paste preferred for speed)
6. Optionally submits the form

## License

Private.
