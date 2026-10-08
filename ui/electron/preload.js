const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tradegate", {
  onInitData: (callback) => {
    ipcRenderer.on("init-data", (_event, data) => callback(data));
  },
  sendResult: (data) => {
    ipcRenderer.send("dialog-result", data);
  },
  onUpdate: (callback) => {
    ipcRenderer.on("update", (_event, data) => callback(data));
  },
  emit: (data) => {
    ipcRenderer.send("dialog-event", data);
  },
  cancel: () => {
    ipcRenderer.send("dialog-cancel");
  },
});
