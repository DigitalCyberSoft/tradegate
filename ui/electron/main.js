const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");

// Parse CLI args: --dialog <type>
let dialogType = null;
let dialogData = {};

const args = process.argv.slice(2);
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--dialog" && args[i + 1]) {
    dialogType = args[++i];
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

const htmlFile = path.join(__dirname, "dialogs", `${dialogType}.html`);

const dialogSizes = {
  "account-picker":  { width: 620, height: 480 },
  "edit-account":    { width: 540, height: 520 },
  "add-account":     { width: 540, height: 580 },
  "add-password":    { width: 500, height: 300 },
  "password-prompt": { width: 500, height: 300 },
  "confirm-delete":       { width: 480, height: 260 },
  "set-master-password":  { width: 500, height: 380 },
};
const size = dialogSizes[dialogType] || { width: 480, height: 360 };

function createWindow() {
  const win = new BrowserWindow({
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

ipcMain.on("dialog-cancel", () => {
  app.quit();
});

app.whenReady().then(async () => {
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
