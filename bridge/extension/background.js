// AntiCompress: intercept downloads before Firefox saves them, ask via the
// native host's terminal chooser: stream (AntiCompress) or normal download.
const RESTART_URLS = new Set();
const HOST = "anticompress";

// Only streamable archives get the chooser — everything else (including
// rar/7z, which cannot stream) downloads normally, zero interruption.
const ARCHIVE_EXT = /\.(zip|tar|gz|zst|xz|bz2|tzst|iso|acpkg)$/i;
const PASS_EXT = /\.(rar|7z|001|mp3|mp4|mkv|avi|mov|wav|flac|webm|jpg|jpeg|png|gif|webp|pdf|txt|doc|docx|xls|xlsx|ppt|pptx|exe|msi|apk|dmg|epub)$/i;

function classify(name) {
  if (ARCHIVE_EXT.test(name)) return "archive";
  if (PASS_EXT.test(name)) return "pass"; // let Firefox save it, no chooser
  return "ask"; // unknown -> chooser, we can't tell what it is
}

function ask(url, filename, size) {
  const port = browser.runtime.connectNative(HOST);
  port.onMessage.addListener((msg) => {
    console.log("AntiCompress host message:", JSON.stringify(msg));
    if (msg && msg.action === "normal") {
      RESTART_URLS.add(url);
      browser.downloads
        .download({ url })
        .then((id) => console.log("AntiCompress restarted download:", id))
        .catch((e) => console.error("AntiCompress restart FAILED:", e.message));
      port.disconnect();
    } else if (msg && msg.action === "finished") {
      port.disconnect();
    }
    // "stream": keep the port open. The native host stays alive until the
    // download finishes; disconnecting early makes Firefox tear down the
    // host's process tree, killing the still-running download.
  });
  port.onDisconnect.addListener(() => {
    if (port.error) {
      console.error("AntiCompress native host error:", port.error.message);
    }
  });
  port.postMessage({ type: "download", url, filename, size });
}

browser.downloads.onCreated.addListener(async (item) => {
  if (RESTART_URLS.has(item.url)) {
    // Our own "Normal download" restart — let Firefox save it untouched.
    RESTART_URLS.delete(item.url);
    return;
  }
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) {
    // In-page generated downloads can't be streamed — let Firefox handle them.
    return;
  }
  const kind = classify(item.filename || item.url);
  if (kind === "pass") {
    return; // media/docs/executables: normal download, no interruption
  }
  try {
    await browser.downloads.cancel(item.id);
  } catch (e) {
    // Download already finished (tiny file) — the chooser still gets offered.
  }
  ask(item.url, (item.filename || "").split(/[\\/]/).pop(), item.fileSize > 0 ? item.fileSize : 0);
});

// Secondary flow: right-click a link -> Download with AntiCompress.
browser.contextMenus.create({
  id: "anticompress-dl",
  title: "Download with AntiCompress",
  contexts: ["link"],
});

browser.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "anticompress-dl" && info.linkUrl) {
    const name = info.linkUrl.split("/").pop() || "download";
    ask(info.linkUrl, decodeURIComponent(name), 0);
  }
});
