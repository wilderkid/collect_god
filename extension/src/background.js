const SERVER = "http://127.0.0.1:8787";

async function health() {
  try {
    const res = await fetch(SERVER + "/api/health", { cache: "no-store" });
    const data = await res.json();
    return { ok: !!data.ok };
  } catch (e) {
    return { ok: false };
  }
}

async function save(tweet) {
  let res;
  try {
    res = await fetch(SERVER + "/api/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tweet),
    });
  } catch (e) {
    return { ok: false, error: "server-down" };
  }
  try {
    return await res.json();
  } catch (e) {
    return { ok: false, error: "bad-response", message: "本地服务返回了无法识别的内容" };
  }
}

function flash(text) {
  chrome.action.setBadgeBackgroundColor({ color: "#9f3d1c" });
  chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "health") {
    health().then(sendResponse);
    return true;
  }
  if (msg.type === "save") {
    save(msg.tweet).then(sendResponse);
    return true;
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "capture-tweet") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return;
  try {
    const res = await chrome.tabs.sendMessage(tab.id, { type: "capture" });
    flash(res && res.ok ? "OK" : "!");
  } catch (e) {
    flash("!");
  }
});
