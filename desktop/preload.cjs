const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('pilotDesktop', Object.freeze({
  identity: () => ipcRenderer.invoke('pilot:identity'),
  restart: () => ipcRenderer.invoke('pilot:restart'),
  pickFiles: () => ipcRenderer.invoke('pilot:pick-files')
}));
