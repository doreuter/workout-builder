"use strict";

// Saving workouts straight into the Zwift folder from the browser (File System Access API:
// Chrome, Edge, Brave and Opera on desktop). The user picks the folder once; the browser keeps
// the permission handle in IndexedDB, so it never leaves this computer.
const ZwiftFolder = (() => {
  const supported = typeof window.showDirectoryPicker === "function";
  const DB = "vibegrind", STORE = "handles", KEY = "zwift-folder";
  // Files we wrote; "Workout Builder" was the app's name before VibeGrind.
  const OUR_AUTHORS = ["<author>VibeGrind</author>", "<author>Workout Builder</author>"];
  let handle = null; // the numbered Zwift account folder

  function idb(mode, op) {
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(DB, 1);
      open.onupgradeneeded = () => open.result.createObjectStore(STORE);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const tx = open.result.transaction(STORE, mode);
        const req = op(tx.objectStore(STORE));
        tx.oncomplete = () => resolve(req.result);
        tx.onerror = () => reject(tx.error);
      };
    });
  }

  async function load() {
    if (!supported) return null;
    try { handle = (await idb("readonly", (s) => s.get(KEY))) || null; } catch { handle = null; }
    return handle;
  }

  async function remember(h) {
    handle = h;
    try { await idb("readwrite", (s) => (h ? s.put(h, KEY) : s.delete(KEY))); } catch { /* still works this session */ }
  }

  // Accept the numbered folder itself, or Zwift / Workouts above it if there's exactly one account folder.
  async function resolveFolder(picked) {
    let dir = picked;
    for (let depth = 0; depth < 3; depth++) {
      if (/^\d+$/.test(dir.name)) return dir;
      const subdirs = [];
      for await (const [name, entry] of dir.entries()) if (entry.kind === "directory") subdirs.push(entry);
      const numbered = subdirs.filter((d) => /^\d+$/.test(d.name));
      if (numbered.length === 1) return numbered[0];
      if (numbered.length > 1) throw new Error("This folder holds several Zwift accounts. Choose the numbered folder of your own account.");
      const workouts = subdirs.find((d) => d.name.toLowerCase() === "workouts");
      if (!workouts) break;
      dir = workouts;
    }
    throw new Error("That doesn't look like your Zwift workouts folder. Choose Documents → Zwift → Workouts → the folder with a number.");
  }

  async function connect() {
    const picked = await window.showDirectoryPicker({ id: "zwift-workouts", mode: "readwrite", startIn: "documents" });
    const dir = await resolveFolder(picked);
    await remember(dir);
    return dir.name;
  }

  async function ensurePermission() {
    if (!handle) return false;
    const opts = { mode: "readwrite" };
    if ((await handle.queryPermission(opts)) === "granted") return true;
    return (await handle.requestPermission(opts)) === "granted"; // the browser asks; needs a click
  }

  async function existing(name) {
    try { return await handle.getFileHandle(name); } catch (e) { if (e.name === "NotFoundError") return null; throw e; }
  }

  // Replace an earlier export of ours, but never someone else's file: that one gets a _1, _2… name.
  async function write(filename, blob) {
    if (!(await ensurePermission())) throw new Error("VibeGrind needs your permission to save into the Zwift folder.");
    const dot = filename.lastIndexOf(".");
    const stem = filename.slice(0, dot), ext = filename.slice(dot);
    let name = filename, n = 1, file;
    while ((file = await existing(name))) {
      const head = await (await file.getFile()).slice(0, 500).text();
      if (OUR_AUTHORS.some((a) => head.includes(a))) break;
      name = `${stem}_${n++}${ext}`;
    }
    const fh = await handle.getFileHandle(name, { create: true });
    const out = await fh.createWritable();
    await out.write(blob);
    await out.close();
    return name;
  }

  async function listWorkouts() {
    if (!(await ensurePermission())) throw new Error("VibeGrind needs your permission to read the Zwift folder.");
    const files = [];
    for await (const [name, entry] of handle.entries()) {
      if (entry.kind === "file" && name.toLowerCase().endsWith(".zwo")) files.push(await entry.getFile());
    }
    return files;
  }

  return {
    supported,
    load,
    connect,
    disconnect: () => remember(null),
    write,
    listWorkouts,
    resolveFolder,
    useHandle: remember, // for tests
    get folderName() { return handle ? handle.name : null; },
  };
})();

// Which kind of device this is, for the right instructions.
function deviceKind() {
  const ua = navigator.userAgent;
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return "tablet";
  if (/Android|iPhone|Mobile/.test(ua)) return "phone";
  if (/Windows/.test(ua)) return "windows";
  if (/Macintosh|Mac OS X/.test(ua)) return "mac";
  return "desktop";
}
