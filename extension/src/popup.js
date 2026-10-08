const serverEl = document.getElementById("server");
const hintEl = document.getElementById("hint");
const previewEl = document.getElementById("preview");
const authorEl = document.getElementById("author");
const handleEl = document.getElementById("handle");
const textEl = document.getElementById("text");
const metaEl = document.getElementById("meta");
const saveBtn = document.getElementById("save");
const resultEl = document.getElementById("result");

let serverUp = false;
let readyTweet = null;

function mediaSummary(media) {
  const list = media || [];
  const photos = list.filter((item) => item.type === "photo").length;
  const videos = list.filter((item) => item.type === "video" || item.type === "animated_gif").length;
  if (!photos && !videos) return "无媒体";
  const parts = [];
  if (photos) parts.push("图片 " + photos);
  if (videos) parts.push("视频 " + videos);
  return parts.join(" · ");
}

function showHint(text) {
  hintEl.hidden = false;
  hintEl.textContent = text;
  previewEl.hidden = true;
  readyTweet = null;
  saveBtn.disabled = true;
}

function showReady(tweet) {
  readyTweet = tweet;
  hintEl.hidden = true;
  previewEl.hidden = false;
  authorEl.textContent = (tweet.author && tweet.author.name) || "未知作者";
  handleEl.textContent = tweet.author && tweet.author.handle ? "@" + tweet.author.handle : "";
  textEl.textContent = tweet.text || "（这条没有正文）";
  metaEl.textContent = mediaSummary(tweet.media);
  saveBtn.disabled = !serverUp;
}

async function refresh() {
  const health = await chrome.runtime.sendMessage({ type: "health" });
  serverUp = !!(health && health.ok);
  serverEl.textContent = serverUp ? "本地服务已连接" : "本地服务没开";
  serverEl.classList.toggle("down", !serverUp);

  let tab;
  try {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch (e) {
    showHint("读不到当前标签页");
    return;
  }
  if (!tab || !tab.id) {
    showHint("读不到当前标签页");
    return;
  }

  let state;
  try {
    state = await chrome.tabs.sendMessage(tab.id, { type: "getState" });
  } catch (e) {
    showHint("请打开 X 的贴文详情页。如果刚装好扩展，先刷新那一页。");
    return;
  }
  if (!state || state.page === "not-tweet") {
    showHint("当前不是贴文详情页。打开一条贴文后再收录。");
    return;
  }
  if (state.page !== "ready" || !state.tweet) {
    showHint("已打开贴文，还没读到数据。等一两秒，或刷新页面后再试。");
    return;
  }
  showReady(state.tweet);
  if (!serverUp) resultEl.textContent = "先双击 start.bat，再回来收录。";
}

saveBtn.addEventListener("click", async () => {
  if (!readyTweet) return;
  saveBtn.disabled = true;
  resultEl.textContent = "正在收录…";
  let tab;
  try {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const res = await chrome.tabs.sendMessage(tab.id, { type: "capture", quiet: true });
    resultEl.textContent = (res && res.message) || "收录失败";
    if (res && res.ok) saveBtn.textContent = res.status === "exists" ? "已收录过" : "已收录";
  } catch (e) {
    resultEl.textContent = "收录失败，请刷新贴文页后再试";
    saveBtn.disabled = !serverUp;
  }
});

refresh();
