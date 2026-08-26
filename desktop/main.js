// LinkChat desktop shell (Electron). Boots the Python engine, waits for it to be
// healthy, then opens a branded window onto the web UI it serves at 127.0.0.1:8770.
// The engine serves BOTH the API and the built web/dist (server.py), so this shell
// just needs to start it and point a window at it — no Vite, no bundler at runtime.
//
//   cd the parent program/web && npm run build      # build the UI once (server serves web/dist)
//   cd the parent program/desktop && npm install && npm start
//
// Auto-update (electron-updater + a GitHub-releases feed) is the next step — the
// electron-builder config in package.json is the foundation for it.

const { app, BrowserWindow, shell } = require("electron");
const { spawn, execFileSync } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");
const { checkForUpdates } = require("./updater");  // macOS free-path self-updater (no Squirrel)

const PORT = 8790;
const HEALTH = `http://127.0.0.1:${PORT}/api/health`;
const APP_URL = `http://127.0.0.1:${PORT}/`;
// desktop/ → the parent program/ → automation/  (the CWD the package resolves from)
const APP_ROOT = path.resolve(__dirname, "..");   // the LinkChat folder
const PYTHON = process.platform === "win32" ? "python" : "python3";

let engine = null;
let win = null;

function ping(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => { res.resume(); resolve(res.statusCode === 200); });
    req.on("error", () => resolve(false));
    req.setTimeout(1500, () => { req.destroy(); resolve(false); });
  });
}

// Path to the bundled PyInstaller engine binary when PACKAGED (extraResources -> engine/).
// In dev (not packaged) we fall back to the source tree via system python. This is the
// cross-platform spawn: on macOS users have no system Python, so the frozen engine is the
// only thing that runs; on Windows dev this keeps `npm start` working unchanged.
function frozenEngineCmd() {
  if (!app.isPackaged) return null;
  const bin = process.platform === "win32" ? "linkchat-engine.exe" : "linkchat-engine";
  return path.join(process.resourcesPath, "engine", bin);
}

// macOS, non-notarized build: when the user approves the app via "Open Anyway", macOS
// whitelists ONLY the top-level shell. The bundled engine binary (and side-shipped Chromium)
// are SEPARATE executables that stay quarantined, so the OS KILLS the engine when we spawn it
// — the window opens but the engine never does, and the UI shows "is the engine running?".
// The shell is already user-approved by the time this runs, so strip the quarantine flag from
// the whole bundle in-process. This removes any need for the tester to touch Terminal.
// No-op on Windows; best-effort (a failure here must never block launch).
function declawQuarantineMac() {
  if (process.platform !== "darwin" || !app.isPackaged) return;
  try {
    const appBundle = path.resolve(process.resourcesPath, ".."); // .../LinkChat.app/Contents
    const appRoot = path.resolve(appBundle, "..");               // .../LinkChat.app
    execFileSync("xattr", ["-dr", "com.apple.quarantine", appRoot], { timeout: 20000 });
    console.log("[mac] cleared com.apple.quarantine on", appRoot);
  } catch (e) {
    console.error("[mac] declaw quarantine failed (non-fatal):", e && e.message);
  }
}

// Engine stdout/stderr -> a log file in the per-user data dir, so a launch failure is
// diagnosable from Finder (~/Library/Application Support/LinkChat/engine.log) without any
// Terminal. Falls back to "ignore" if the log can't be opened.
function engineStdio() {
  try {
    const dir = app.getPath("userData");
    fs.mkdirSync(dir, { recursive: true });
    const fd = fs.openSync(path.join(dir, "engine.log"), "a");
    return ["ignore", fd, fd];
  } catch (_) {
    return "ignore";
  }
}

async function ensureEngine() {
  if (await ping(HEALTH)) return; // already running (e.g. dev) — reuse it
  declawQuarantineMac();          // unblock the bundled engine before we spawn it (macOS)
  const stdio = engineStdio();
  const exe = frozenEngineCmd();
  if (exe) {
    // Chromium is SIDE-SHIPPED next to the app (extraResources -> ms-playwright) because
    // PyInstaller can't sign it inside the frozen engine on macOS. Point Playwright at it.
    const browsersPath = path.join(process.resourcesPath, "ms-playwright");
    engine = spawn(exe, ["serve", "--port", String(PORT)], {
      cwd: path.dirname(exe),
      stdio,
      env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: browsersPath },
      ...(process.platform === "win32" ? { windowsHide: true } : {}),
    });
  } else {
    engine = spawn(PYTHON, ["-m", "engine", "serve", "--port", String(PORT)], {
      cwd: APP_ROOT,
      windowsHide: true,
      stdio,
    });
  }
  engine.on("error", (e) => console.error("engine spawn failed:", e));
  // wait up to ~30s for health
  for (let i = 0; i < 60; i++) {
    if (await ping(HEALTH)) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  console.error("engine did not become healthy in time");
}

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 940,
    minHeight: 640,
    backgroundColor: "#fbfcfc",
    title: "LinkChat",
    autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.loadURL(APP_URL);
  // open external links (e.g. "Open profile") in the real browser, not the app window
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http")) { shell.openExternal(url); return { action: "deny" }; }
    return { action: "allow" };
  });
  win.on("closed", () => { win = null; });
}

app.whenReady().then(async () => {
  await ensureEngine();
  createWindow();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
  // macOS free-path self-update: check the R2 manifest a few seconds after the window is up, so it
  // never competes with launch. Asks before applying; any failure is silent (manual download stays
  // the fallback). No-op on Windows (Velopack) and in dev.
  setTimeout(() => { try { checkForUpdates({ prompt: true }); } catch (_) {} }, 8000);
});

function shutdown() {
  if (engine && !engine.killed) { try { engine.kill(); } catch (_) {} }
}
app.on("window-all-closed", () => { shutdown(); if (process.platform !== "darwin") app.quit(); });
app.on("before-quit", shutdown);
process.on("exit", shutdown);
