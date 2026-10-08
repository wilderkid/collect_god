// 隔离世界：读页面桥接节点，把收录请求转给后台，并在页面上给一句反馈。
(function () {
  if (globalThis.__xcollectContent) return;
  globalThis.__xcollectContent = true;

  const BRIDGE_ID = "xcollect-bridge-9f3a";
  let cached = { page: "not-tweet", tweet: null };

  function readBridge() {
    const node = document.getElementById(BRIDGE_ID);
    if (!node || !node.textContent) return null;
    try {
      const data = JSON.parse(node.textContent);
      if (!data || typeof data !== "object") return null;
      cached = data;
      return data;
    } catch (e) {
      return null;
    }
  }

  document.addEventListener("xcollect:update", readBridge);

  function pullState() {
    readBridge();
    return new Promise((resolve) => {
      const finish = () => {
        document.removeEventListener("xcollect:update", onUpdate);
        clearTimeout(timer);
        resolve(readBridge() || cached);
      };
      const onUpdate = () => finish();
      const timer = setTimeout(finish, 250);
      document.addEventListener("xcollect:update", onUpdate);
      document.dispatchEvent(new CustomEvent("xcollect:request"));
    });
  }

  function toast(message) {
    let host = document.getElementById("xcollect-toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "xcollect-toast-host";
      host.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;";
      (document.documentElement || document.body).appendChild(host);
    }
    const shadow = host.shadowRoot || host.attachShadow({ mode: "open" });
    shadow.innerHTML = "";
    const style = document.createElement("style");
    style.textContent =
      ".box{font:14px/1.5 'Segoe UI','Microsoft YaHei',sans-serif;color:#1c1917;background:#f7f4ef;" +
      "border:1px solid #e0d9ce;border-radius:10px;padding:10px 14px;max-width:280px;" +
      "box-shadow:0 8px 24px rgba(28,25,23,.18)}";
    const box = document.createElement("div");
    box.className = "box";
    box.textContent = message;
    shadow.append(style, box);
    clearTimeout(host.__timer);
    host.__timer = setTimeout(() => {
      host.remove();
    }, 3600);
  }

  function messageFor(res) {
    if (!res || res.error === "server-down") return "本地服务没开。先双击 start.bat";
    if (res.error === "not-tweet") return "请先打开一条贴文的详情页";
    if (res.error === "no-data") return "还没读到贴文数据，刷新页面后再试一次";
    if (!res.ok) return res.message || "收录失败";
    if (res.status === "exists") return "这条已经收录过了";
    const skipped = res.skipped || [];
    const failed = res.failed || [];
    if (failed.length) return "正文已保存，有媒体下载失败。重启本地服务后会自动重试";
    if (skipped.length) return "已收录。有视频只有流地址，没能下载";
    return "已收录";
  }

  async function doCapture(quiet) {
    document.dispatchEvent(new CustomEvent("xcollect:rescan"));
    const state = await pullState();
    let result;
    if (!state || state.page === "not-tweet") {
      result = { ok: false, error: "not-tweet" };
    } else if (state.page !== "ready" || !state.tweet) {
      result = { ok: false, error: "no-data" };
    } else {
      try {
        result = await chrome.runtime.sendMessage({ type: "save", tweet: state.tweet });
      } catch (e) {
        result = { ok: false, error: "server-down" };
      }
    }
    if (!quiet) toast(messageFor(result));
    return Object.assign({ message: messageFor(result) }, result);
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "getState") {
      pullState().then((state) => sendResponse(state || cached));
      return true;
    }
    if (msg.type === "capture") {
      doCapture(!!msg.quiet).then(sendResponse);
      return true;
    }
  });
})();
