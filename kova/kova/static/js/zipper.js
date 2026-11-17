/* Packs a project FOLDER into a .zip inside the browser, so people can drop a whole folder
   instead of making a zip themselves. No compression (files are "stored"), which keeps it
   small, fast and dependency-free. The server unpacks it with the same safety checks it
   applies to every upload. */
window.ZipTools = (function () {
  const SKIP_DIRS = new Set(["node_modules", ".git", ".venv", "venv", "__pycache__", ".idea", ".vscode", ".next", ".cache"]);
  const SKIP_FILES = new Set([".DS_Store", "Thumbs.db"]);

  // Files that must never leave the person's computer (secrets, version history, dependencies).
  function skipped(path) {
    const parts = path.split("/");
    const name = parts[parts.length - 1];
    if (SKIP_FILES.has(name) || name === ".env" || name.startsWith(".env.")) return true;
    return parts.slice(0, -1).some((p) => SKIP_DIRS.has(p));
  }

  let table = null;
  function crcTable() {
    if (table) return table;
    table = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0; }
    return table;
  }
  function crc32(bytes) {
    const t = crcTable(); let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = t[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  }
  function dosTime(d) { return { time: (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1), date: ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate() }; }

  /* entries: [{ path: "css/site.css", file: File }]  ->  Blob (a valid .zip) */
  async function build(entries, onProgress) {
    const enc = new TextEncoder();
    const parts = [], central = [];
    let offset = 0, done = 0;
    for (const e of entries) {
      const data = new Uint8Array(await e.file.arrayBuffer());
      const name = enc.encode(e.path);
      const crc = crc32(data);
      const { time, date } = dosTime(e.file.lastModified ? new Date(e.file.lastModified) : new Date());
      const local = new DataView(new ArrayBuffer(30));
      local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true); local.setUint16(6, 0x0800, true);
      local.setUint16(8, 0, true); local.setUint16(10, time, true); local.setUint16(12, date, true);
      local.setUint32(14, crc, true); local.setUint32(18, data.length, true); local.setUint32(22, data.length, true);
      local.setUint16(26, name.length, true); local.setUint16(28, 0, true);
      parts.push(local.buffer, name, data);
      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true); cd.setUint16(4, 20, true); cd.setUint16(6, 20, true); cd.setUint16(8, 0x0800, true);
      cd.setUint16(10, 0, true); cd.setUint16(12, time, true); cd.setUint16(14, date, true);
      cd.setUint32(16, crc, true); cd.setUint32(20, data.length, true); cd.setUint32(24, data.length, true);
      cd.setUint16(28, name.length, true); cd.setUint32(42, offset, true);
      central.push(cd.buffer, name);
      offset += 30 + name.length + data.length;
      if (offset > 0xffffffff - 1e6) throw new Error("That folder is too large to pack. Upload a .zip instead.");
      if (onProgress) onProgress(++done / entries.length);
    }
    let cdSize = 0; central.forEach((c) => { cdSize += c.byteLength !== undefined ? c.byteLength : c.length; });
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true); end.setUint16(8, entries.length, true); end.setUint16(10, entries.length, true);
    end.setUint32(12, cdSize, true); end.setUint32(16, offset, true);
    return new Blob([...parts, ...central, end.buffer], { type: "application/zip" });
  }

  /* Read everything inside a dropped directory entry (drag and drop). */
  function readAll(dirEntry) {
    return new Promise((resolve) => {
      const out = [], reader = dirEntry.createReader();
      (function more() { reader.readEntries((batch) => { if (!batch.length) resolve(out); else { out.push(...batch); more(); } }, () => resolve(out)); })();
    });
  }
  async function walk(entry, prefix, acc) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      acc.push({ path: prefix + entry.name, file });
    } else if (entry.isDirectory) {
      if (SKIP_DIRS.has(entry.name)) { acc.skippedAny = true; return; }
      for (const child of await readAll(entry)) await walk(child, prefix + entry.name + "/", acc);
    }
  }
  /* DataTransfer from a drop  ->  { entries, rootName } */
  async function fromDrop(dataTransfer) {
    const acc = [];
    const items = [...(dataTransfer.items || [])].map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    let rootName = "";
    if (items.length === 1 && items[0].isDirectory) {
      rootName = items[0].name;
      for (const child of await readAll(items[0])) await walk(child, "", acc);   // contents of the folder, not the folder itself
    } else {
      for (const it of items) await walk(it, "", acc);
    }
    return { entries: acc.filter((e) => !skipped(e.path)), rootName };
  }
  /* <input webkitdirectory> FileList  ->  { entries, rootName } */
  function fromInput(fileList) {
    const files = [...fileList];
    const rootName = files.length && files[0].webkitRelativePath ? files[0].webkitRelativePath.split("/")[0] : "";
    const entries = files.map((f) => ({ path: (f.webkitRelativePath || f.name).split("/").slice(1).join("/") || f.name, file: f }));
    return { entries: entries.filter((e) => !skipped(e.path)), rootName };
  }
  return { build, fromDrop, fromInput, skipped };
})();
