const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const net = require('node:net');
const { spawn } = require('node:child_process');
app.setName('PILOT Studio');
app.setAppUserModelId('PILOT.Studio');

const releaseRoot = process.env.PILOT_RELEASE_ROOT || path.resolve(__dirname, '..');
const manifest = JSON.parse(fs.readFileSync(path.join(releaseRoot, 'release.json'), 'utf8'));
const token = crypto.randomBytes(32).toString('hex');
const baseDataDir = path.resolve(process.env.PILOT_DATA_DIR || path.join(process.env.LOCALAPPDATA || path.resolve(app.getPath('appData'), '..', 'Local'), 'PILOT Studio', 'data-v2'));
function selectedDataDir(base) {
  const anchor = path.basename(path.dirname(base)) === 'restored' && /^restore-[0-9a-f]{32}$/.test(path.basename(base)) ? path.resolve(base, '..', '..') : path.dirname(base);
  const marker = path.join(anchor, 'active-storage.json');
  if (!fs.existsSync(marker)) return base;
  const selected = JSON.parse(fs.readFileSync(marker, 'utf8'));
  const target = path.resolve(selected.data_dir || '');
  if (selected.format !== 'pilot-active-storage/2' || path.dirname(target) !== path.join(anchor, 'restored') || !/^restore-[0-9a-f]{32}$/.test(path.basename(target))) throw new Error('선택한 복원 저장소 정보가 올바르지 않습니다.');
  const recordBytes = fs.readFileSync(path.join(target, 'restore-result.json'));
  const record = JSON.parse(recordBytes.toString('utf8'));
  if (record.status !== 'verified' || record.id !== path.basename(target) || path.resolve(record.data_dir) !== target || crypto.createHash('sha256').update(recordBytes).digest('hex') !== selected.restore_record_sha256) throw new Error('복원 검증 기록이 바뀌었습니다. 저장소를 다시 확인해 주세요.');
  return target;
}
let startupError = null;
let dataDir = baseDataDir;
try { dataDir = selectedDataDir(baseDataDir); } catch (error) { startupError = error; }
app.setPath('userData', path.join(dataDir, 'electron'));
fs.mkdirSync(path.join(dataDir, 'logs'), { recursive: true });
app.setAppLogsPath(path.join(dataDir, 'logs'));
process.env.ELECTRON_LOG_FILE = path.join(dataDir, 'logs', 'desktop.log');
process.env.CHROME_LOG_FILE = path.join(dataDir, 'logs', 'chromium.log');
app.commandLine.appendSwitch('log-file', process.env.ELECTRON_LOG_FILE);
let backend = null;
let window = null;
let baseURL = '';
let quitting = false;
let restarting = false;

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
  });
}

async function waitHealthy() {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    if (backend.exitCode !== null) throw new Error('백엔드가 종료되었습니다. 저장소의 진단 로그를 확인해 주세요.');
    try {
      const response = await fetch(baseURL + '/api/health', { headers: { 'X-Pilot-Token': token }, signal: AbortSignal.timeout(1500) });
      if (response.ok) {
        const health = await response.json();
        if (health.build_id !== manifest.build_id) throw new Error('실행 버전 불일치: 업데이트를 복구해 주세요.');
        if (health.session_fingerprint !== crypto.createHash('sha256').update(token).digest('hex')) throw new Error('백엔드 세션 불일치: 이 창에서 시작한 서버가 아닙니다.');
        return;
      }
    } catch (error) {
      if (error.message.startsWith('실행 버전') || error.message.startsWith('백엔드 세션')) throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 180));
  }
  throw new Error('프로그램 준비 시간이 초과되었습니다. MariaDB 시작 로그를 확인해 주세요.');
}

async function start() {
  if (startupError) throw startupError;
  if (process.env.PILOT_BUILD_ID && process.env.PILOT_BUILD_ID !== manifest.build_id) throw new Error('런처와 설치 빌드가 다릅니다.');
  const port = await freePort();
  baseURL = 'http://127.0.0.1:' + port;
  fs.mkdirSync(path.join(dataDir, 'logs'), { recursive: true });
  const log = fs.openSync(path.join(dataDir, 'logs', 'desktop-backend.log'), 'a');
  const packagedBackend = path.join(releaseRoot, 'backend', 'PILOT-Backend.exe');
  const packaged = fs.existsSync(packagedBackend);
  const python = process.env.PILOT_PYTHON || path.resolve(releaseRoot, '..', 'pilot-studio', '.venv', 'Scripts', 'python.exe');
  backend = spawn(packaged ? packagedBackend : python, packaged ? ['--server'] : [path.join(releaseRoot, 'launcher.py'), '--server'], {
    cwd: releaseRoot, windowsHide: true,
    stdio: ['ignore', log, log],
    env: { ...process.env, PILOT_PORT: String(port), PILOT_DATA_DIR: dataDir, PILOT_SESSION_TOKEN: token, PILOT_BUILD_ID: manifest.build_id,
      PILOT_RELEASE_ROOT: releaseRoot, PYTHONUTF8: '1',
      PILOT_PARENT_PID: String(process.pid),
      PILOT_RESOURCE_DIR: packaged ? path.join(releaseRoot, 'backend', '_internal') : path.join(releaseRoot, 'packaging', 'resources') }
  });
  fs.closeSync(log);
  backend.once('error', error => dialog.showErrorBox('PILOT Studio', error.message));
  await waitHealthy();
  window = new BrowserWindow({ width: 1440, height: 960, minWidth: 980, minHeight: 720, show: false,
    backgroundColor: '#edf3f4', title: 'PILOT Studio ' + manifest.app_version,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, preload: path.join(__dirname, 'preload.cjs') }
  });
  window.setMenuBarVisibility(false);
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(baseURL + '/api/files/') || url.startsWith(baseURL + '/api/backups/')) {
      window.webContents.downloadURL(url, { headers: { 'X-Pilot-Token': token } });
    }
    return { action: 'deny' };
  });
  window.webContents.session.on('will-download', (_event, item) => {
    item.setSaveDialogOptions({ title: 'PILOT Studio 파일 저장', defaultPath: path.join(app.getPath('downloads'), item.getFilename()) });
    item.once('done', (_done, state) => {
      if (state === 'completed') shell.showItemInFolder(item.getSavePath());
      else if (state !== 'cancelled') dialog.showErrorBox('파일 저장 실패', '파일 저장을 완료하지 못했습니다. 다시 시도해 주세요.');
    });
  });
  window.webContents.on('will-navigate', (event, url) => { if (!url.startsWith(baseURL + '/')) event.preventDefault(); });
  window.webContents.session.webRequest.onBeforeSendHeaders({ urls: [baseURL + '/*'] }, (details, callback) => {
    details.requestHeaders['X-Pilot-Token'] = token;
    callback({ requestHeaders: details.requestHeaders });
  });
  await window.loadURL(baseURL + '/?token=' + token + '&build=' + manifest.build_id);
  if (!process.argv.includes('--smoke')) window.show();
  if (process.argv.includes('--smoke')) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    const rendered = await window.webContents.executeJavaScript('({title:document.title,text:document.body.innerText,children:document.querySelector("#root")?.childElementCount||0})');
    if (!rendered.children || rendered.text.trim().length < 20) throw new Error('UI_RENDER_FAILED');
    const evidence = { status: 'ready', build_id: manifest.build_id, title: rendered.title, text: rendered.text.slice(0, 2000), backend: baseURL };
    const output = process.env.PILOT_SMOKE_RESULT;
    if (output) fs.writeFileSync(output, JSON.stringify(evidence, null, 2));
    app.quit();
  }
}

ipcMain.handle('pilot:identity', () => ({ app_version: manifest.app_version, build_id: manifest.build_id }));
ipcMain.handle('pilot:restart', () => {
  selectedDataDir(baseDataDir);
  restarting = true;
  app.quit();
  return { status: 'restarting' };
});
ipcMain.handle('pilot:pick-files', async () => {
  const selected = await dialog.showOpenDialog(window, { properties: ['openFile', 'multiSelections'], filters: [{ name: 'STEP 모델', extensions: ['stp', 'step'] }] });
  return selected.canceled ? [] : selected.filePaths;
});

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.whenReady().then(start).catch(error => {
    if (process.argv.includes('--smoke')) {
      if (process.env.PILOT_SMOKE_RESULT) fs.writeFileSync(process.env.PILOT_SMOKE_RESULT, JSON.stringify({ status: 'failed', build_id: manifest.build_id, error: error.message }, null, 2));
    } else dialog.showErrorBox('PILOT Studio 시작 실패', error.message);
    app.exit(1);
  });
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (quitting || !backend || backend.exitCode !== null) return;
  event.preventDefault(); quitting = true;
  (async () => {
    try { await fetch(baseURL + '/api/shutdown', { method: 'POST', headers: { 'X-Pilot-Token': token }, signal: AbortSignal.timeout(3000) }); } catch {}
    const deadline = Date.now() + 20000;
    while (backend.exitCode === null && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 100));
    if (backend.exitCode === null) backend.kill();
    if (restarting) {
      const installation = process.env.PILOT_INSTALL_ROOT;
      if (installation && fs.existsSync(path.join(installation, 'PILOT-Studio.exe'))) {
        const next = spawn(path.join(installation, 'PILOT-Studio.exe'), ['--wait-close'], { detached: true, stdio: 'ignore', windowsHide: true, env: process.env });
        next.unref();
      } else {
        app.relaunch();
      }
    }
    app.exit(0);
  })();
});
