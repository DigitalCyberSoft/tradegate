const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");

// Parse CLI args: --dialog <type>
let dialogType = null;
let dialogData = {};
// --stream: stdin carries newline-delimited JSON (first line = init data,
// later lines = state updates) and stdout carries one JSON line per UI
// event. The window stays open until it is closed or sent {"state":"close"}.
let streamMode = false;
let win = null;
let loaded = false;
const pendingUpdates = [];

const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--dialog" && args[i + 1]) {
    dialogType = args[++i];
  } else if (args[i] === "--stream") {
    streamMode = true;
  }
}

if (!dialogType) {
  process.stderr.write("Usage: electron main.js --dialog <type>\n");
  process.exit(1);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let chunks = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => { chunks += chunk; });
    process.stdin.on("end", () => {
      try {
        resolve(chunks ? JSON.parse(chunks) : {});
      } catch (e) {
        reject(new Error(`Error parsing stdin data: ${e.message}`));
      }
    });
    process.stdin.on("error", reject);
  });
}

function readStdinLines(onLine) {
  let buf = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", (chunk) => {
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      if (line.trim()) onLine(line);
    }
  });
  // Parent went away: nothing can act on this window any more.
  process.stdin.on("end", () => app.quit());
}

function handleStreamLine(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (e) {
    process.stderr.write(`Bad stream update: ${e.message}\n`);
    return;
  }
  if (msg.state === "close") {
    app.quit();
    return;
  }
  if (loaded) {
    win.webContents.send("update", msg);
  } else {
    pendingUpdates.push(msg);
  }
}

const htmlFile = path.join(__dirname, "dialogs", `${dialogType}.html`);

const dialogSizes = {
  "account-picker":  { width: 760, height: 480 },
  "edit-account":    { width: 540, height: 520 },
  "add-account":     { width: 540, height: 580 },
  "add-password":    { width: 500, height: 300 },
  "password-prompt": { width: 500, height: 300 },
  "confirm-delete":       { width: 480, height: 260 },
  "set-master-password":  { width: 500, height: 380 },
  "settings":             { width: 540, height: 560 },
  "transfer":             { width: 560, height: 440 },
};
const size = dialogSizes[dialogType] || { width: 480, height: 360 };

function createWindow() {
  win = new BrowserWindow({
    width: size.width,
    height: size.height,
    frame: false,
    resizable: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(htmlFile);

  win.webContents.on("did-finish-load", () => {
    win.webContents.send("init-data", dialogData);
    loaded = true;
    for (const msg of pendingUpdates.splice(0)) {
      win.webContents.send("update", msg);
    }
  });

  win.on("closed", () => {
    // If window closed without sending result, treat as cancel
    app.quit();
  });
}

ipcMain.on("dialog-result", (_event, data) => {
  process.stdout.write(JSON.stringify(data) + "\n");
  app.quit();
});

ipcMain.on("dialog-event", (_event, data) => {
  if (streamMode) {
    process.stdout.write(JSON.stringify(data) + "\n");
  }
});

ipcMain.on("dialog-cancel", () => {
  app.quit();
});

app.whenReady().then(async () => {
  if (streamMode) {
    let first = true;
    readStdinLines((line) => {
      if (first) {
        first = false;
        try {
          dialogData = JSON.parse(line);
        } catch (e) {
          process.stderr.write(`Error parsing init data: ${e.message}\n`);
          process.exit(1);
        }
        createWindow();
      } else {
        handleStreamLine(line);
      }
    });
    return;
  }
  try {
    dialogData = await readStdin();
  } catch (e) {
    process.stderr.write(e.message + "\n");
    process.exit(1);
  }
  createWindow();
});

app.on("window-all-closed", () => {
  app.quit();
});
