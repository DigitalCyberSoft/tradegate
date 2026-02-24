const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tradegate", {
  onInitData: (callback) => {
    ipcRenderer.on("init-data", (_event, data) => callback(data));
  },
  sendResult: (data) => {
    ipcRenderer.send("dialog-result", data);
  },
  cancel: () => {
    ipcRenderer.send("dialog-cancel");
  },
});
