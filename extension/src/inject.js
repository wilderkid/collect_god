// 跑在页面自己的 JS 世界里，截获 X 发给自己的 GraphQL 响应。
// 不主动请求，所以不需要额外的 token，也不会比正常浏览多打一枪。
(function () {
  if (window.__xcollectInstalled) return;
  window.__xcollectInstalled = true;

  const BRIDGE_ID = "xcollect-bridge-9f3a";
  const api = window.XCollectNormalize;
  let state = { page: "not-tweet", tweet: null };

  function statusId() {
    const match = location.pathname.match(/\/status\/(\d+)/);
    return match ? match[1] : "";
  }

  function publish() {
    const payload = JSON.stringify(state);
    const write = () => {
      let node = document.getElementById(BRIDGE_ID);
      if (!node) {
        node = document.createElement("div");
        node.id = BRIDGE_ID;
        node.hidden = true;
        node.setAttribute("aria-hidden", "true");
        document.documentElement.appendChild(node);
      }
      node.textContent = payload;
      document.dispatchEvent(new CustomEvent("xcollect:update"));
    };
    if (document.documentElement) {
      write();
      return;
    }
    const timer = setInterval(() => {
      if (!document.documentElement) return;
      clearInterval(timer);
      write();
    }, 30);
  }

  function setWaiting(id) {
    state = { page: id ? "waiting" : "not-tweet", tweet: null };
    publish();
  }

  function accept(tweet) {
    if (!tweet || tweet.id !== statusId()) return;
    if (state.tweet && state.tweet.id === tweet.id && api.score(state.tweet) >= api.score(tweet)) return;
    state = { page: "ready", tweet };
    publish();
  }

  function ingestText(text) {
    const id = statusId();
    if (!id || !text || text.indexOf(id) === -1) return;
    try {
      accept(api.extractTweet(JSON.parse(text), id));
    } catch (e) {
      // 不是完整 JSON 时不猜，避免把半截响应解析错。
    }
  }

  function shouldInspect(url, init) {
    if (!url || url.indexOf("/graphql/") === -1) return false;
    const id = statusId();
    if (!id) return false;
    if (url.indexOf(id) !== -1) return true;
    if (/TweetDetail|TweetResult/i.test(url)) return true;
    const body = init && init.body;
    return typeof body === "string" && body.indexOf(id) !== -1;
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const inspect = shouldInspect(url, init);
      return originalFetch.apply(this, arguments).then((response) => {
        if (!inspect) return response;
        try {
          response
            .clone()
            .text()
            .then(ingestText)
            .catch(() => {});
        } catch (e) {
          // clone 失败就放弃这一次，不影响页面自己的请求。
        }
        return response;
      });
    };
  }

  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__xcollectUrl = String(url || "");
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    const url = this.__xcollectUrl || "";
    const init = typeof body === "string" ? { body } : null;
    if (shouldInspect(url, init)) {
      this.addEventListener("load", function () {
        try {
          if (this.responseType === "" || this.responseType === "text") ingestText(this.responseText);
          else if (this.responseType === "json" && this.response) ingestText(JSON.stringify(this.response));
        } catch (e) {
          // responseText 在某些 responseType 下会抛，忽略即可。
        }
      });
    }
    return originalSend.apply(this, arguments);
  };

  // 整页加载时，贴文有可能嵌在页面脚本里而不是再请求一次。只扫含当前 id 的脚本。
  function scanScripts() {
    const id = statusId();
    if (!id || !document.scripts) return;
    for (const script of document.scripts) {
      const text = script.textContent;
      if (!text || text.length > 8000000 || text.indexOf(id) === -1) continue;
      if (text.indexOf("rest_id") === -1) continue;
      ingestText(text);
      if (state.page === "ready") return;
      expandEmbedded(text, id);
      if (state.page === "ready") return;
    }
  }

  function sliceBalanced(text, start) {
    if (text[start] !== "{") return "";
    let depth = 0;
    let quote = false;
    let escape = false;
    for (let i = start; i < text.length && i - start < 3000000; i++) {
      const ch = text[i];
      if (quote) {
        if (escape) escape = false;
        else if (ch === "\\") escape = true;
        else if (ch === '"') quote = false;
        continue;
      }
      if (ch === '"') quote = true;
      else if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) return text.slice(start, i + 1);
      }
    }
    return "";
  }

  function expandEmbedded(text, id) {
    let from = 0;
    let guard = 0;
    while (guard < 20) {
      const at = text.indexOf(id, from);
      if (at === -1) return;
      guard++;
      from = at + id.length;
      let brace = at;
      for (let n = 0; n < 6; n++) {
        brace = text.lastIndexOf("{", brace - 1);
        if (brace < 0) break;
        const slice = sliceBalanced(text, brace);
        if (!slice) continue;
        try {
          accept(api.extractTweet(JSON.parse(slice), id));
        } catch (e) {
          continue;
        }
        if (state.page === "ready") return;
      }
    }
  }

  function onNav() {
    const id = statusId();
    if (state.tweet && state.tweet.id === id) return;
    setWaiting(id);
    if (!id) return;
    setTimeout(scanScripts, 0);
    setTimeout(scanScripts, 1200);
  }

  const originalPush = History.prototype.pushState;
  const originalReplace = History.prototype.replaceState;
  History.prototype.pushState = function () {
    const result = originalPush.apply(this, arguments);
    onNav();
    return result;
  };
  History.prototype.replaceState = function () {
    const result = originalReplace.apply(this, arguments);
    onNav();
    return result;
  };
  window.addEventListener("popstate", onNav);
  document.addEventListener("xcollect:request", publish);
  document.addEventListener("xcollect:rescan", () => {
    scanScripts();
    publish();
  });

  setWaiting(statusId());
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scanScripts);
  } else {
    scanScripts();
  }
  window.addEventListener("load", scanScripts);
})();
