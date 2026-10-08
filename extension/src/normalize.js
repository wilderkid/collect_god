// 从 X 的 GraphQL JSON 里抽出当前贴文。纯函数，不碰 DOM，方便对照真实响应改。
(function () {
  function asId(value) {
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    if (typeof value === "string" && /^\d+$/.test(value)) return value;
    return "";
  }

  // TweetWithVisibilityResults 等包装层把真正的贴文藏在 .tweet / .tweet_results.result 里。
  function unwrapTweet(node) {
    let cur = node;
    for (let i = 0; i < 5; i++) {
      if (!cur || typeof cur !== "object") return null;
      if (cur.__typename === "TweetWithVisibilityResults" && cur.tweet) {
        cur = cur.tweet;
        continue;
      }
      if (cur.tweet_results && cur.tweet_results.result) {
        cur = cur.tweet_results.result;
        continue;
      }
      if (cur.tweet && cur.tweet.rest_id && !cur.rest_id) {
        cur = cur.tweet;
        continue;
      }
      break;
    }
    if (!cur || typeof cur !== "object") return null;
    if (!asId(cur.rest_id)) return null;
    if (cur.__typename === "TweetTombstone" || cur.__typename === "TweetUnavailable") return null;
    return cur;
  }

  function authorOf(tweet) {
    const user =
      (tweet.core && tweet.core.user_results && tweet.core.user_results.result) ||
      (tweet.core && tweet.core.user_result && tweet.core.user_result.result) ||
      null;
    const legacy = (user && user.legacy) || {};
    const core = (user && user.core) || {};
    return {
      name: legacy.name || core.name || "",
      handle: legacy.screen_name || core.screen_name || "",
    };
  }

  function textOf(tweet) {
    const note =
      tweet.note_tweet &&
      tweet.note_tweet.note_tweet_results &&
      tweet.note_tweet.note_tweet_results.result;
    if (note && typeof note.text === "string" && note.text) return note.text;

    const legacy = tweet.legacy || {};
    const full = legacy.full_text || legacy.text || tweet.full_text || tweet.text || "";
    if (typeof full !== "string") return "";
    // display_text_range 用 UTF-16 下标，和 JS 字符串一致，用来切掉正文末尾的媒体短链。
    const range = legacy.display_text_range;
    if (Array.isArray(range) && range.length === 2) {
      const start = range[0];
      const end = range[1];
      if (
        typeof start === "number" &&
        typeof end === "number" &&
        start >= 0 &&
        end >= start &&
        end <= full.length
      ) {
        return full.slice(start, end);
      }
    }
    return full;
  }

  function createdAtOf(tweet) {
    const raw = (tweet.legacy && tweet.legacy.created_at) || tweet.created_at || "";
    if (!raw || typeof raw !== "string") return "";
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return raw;
    return date.toISOString();
  }

  function origPhotoUrl(raw) {
    try {
      const url = new URL(raw);
      url.searchParams.set("name", "orig");
      return url.toString();
    } catch (e) {
      return raw;
    }
  }

  function mediaListOf(tweet) {
    const legacy = tweet.legacy || {};
    const list =
      (legacy.extended_entities && legacy.extended_entities.media) ||
      (legacy.entities && legacy.entities.media) ||
      tweet.extended_entities?.media ||
      [];
    if (!Array.isArray(list)) return [];
    const out = [];
    for (const item of list) {
      if (!item || typeof item !== "object") continue;
      if (item.type === "photo") {
        const raw = item.media_url_https || item.media_url;
        if (typeof raw === "string" && raw) out.push({ type: "photo", url: origPhotoUrl(raw) });
        continue;
      }
      if (item.type !== "video" && item.type !== "animated_gif") continue;
      const variants = (item.video_info && item.video_info.variants) || [];
      const mp4 = variants
        .filter((v) => v && v.content_type === "video/mp4" && typeof v.url === "string" && v.url)
        .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0))[0];
      const type = item.type === "animated_gif" ? "animated_gif" : "video";
      if (mp4) {
        out.push({ type, url: mp4.url, bitrate: mp4.bitrate || 0 });
      } else {
        out.push({ type: "video", skipped: "m3u8-only" });
      }
    }
    return out;
  }

  // 纯转推的正文和媒体在内层。详情页一般会直接打开原贴，这里只是兜底。
  function contentSource(tweet) {
    const inner =
      tweet.legacy &&
      tweet.legacy.retweeted_status_result &&
      unwrapTweet(tweet.legacy.retweeted_status_result);
    if (!inner) return tweet;
    const outerText = textOf(tweet);
    const innerText = textOf(inner);
    if (innerText && innerText.length >= outerText.length) return inner;
    if (mediaListOf(inner).length && !mediaListOf(tweet).length) return inner;
    return tweet;
  }

  function urlsOf(tweet) {
    const note =
      tweet.note_tweet &&
      tweet.note_tweet.note_tweet_results &&
      tweet.note_tweet.note_tweet_results.result;
    const lists = [
      note && note.entity_set && note.entity_set.urls,
      note && note.entities && note.entities.urls,
      tweet.legacy && tweet.legacy.entities && tweet.legacy.entities.urls,
    ];
    const out = [];
    const seen = new Set();
    for (const list of lists) {
      if (!Array.isArray(list)) continue;
      for (const item of list) {
        if (!item || typeof item.url !== "string" || !item.url || seen.has(item.url)) continue;
        seen.add(item.url);
        out.push({
          url: item.url,
          expanded: typeof item.expanded_url === "string" ? item.expanded_url : "",
          display: typeof item.display_url === "string" ? item.display_url : "",
        });
      }
    }
    return out;
  }

  function normalize(tweet) {
    const source = contentSource(tweet);
    const author = authorOf(source);
    const id = asId(tweet.rest_id);
    const handle = author.handle;
    return {
      id,
      url: handle ? "https://x.com/" + handle + "/status/" + id : "https://x.com/i/status/" + id,
      text: textOf(source),
      author,
      createdAt: createdAtOf(source) || createdAtOf(tweet),
      media: mediaListOf(source),
      urls: urlsOf(source),
    };
  }

  function walk(node, visit, seen) {
    if (!node || typeof node !== "object") return;
    if (seen.has(node)) return;
    seen.add(node);
    if (Array.isArray(node)) {
      for (const item of node) walk(item, visit, seen);
      return;
    }
    visit(node);
    for (const key of Object.keys(node)) walk(node[key], visit, seen);
  }

  function score(tweet) {
    if (!tweet) return -1;
    let value = 0;
    if (tweet.text) value += Math.min(tweet.text.length, 5000);
    if (tweet.author && tweet.author.handle) value += 80;
    if (tweet.author && tweet.author.name) value += 20;
    if (tweet.createdAt) value += 10;
    for (const item of tweet.media || []) {
      value += item.url ? 200 : 20;
    }
    return value;
  }

  // 在一整份 GraphQL 响应里找 rest_id 等于当前贴文的那条，忽略引用贴和线程里的其他贴。
  function extractTweet(root, statusId) {
    const want = asId(statusId);
    if (!want || !root || typeof root !== "object") return null;
    let best = null;
    walk(
      root,
      (node) => {
        const tweet = unwrapTweet(node);
        if (!tweet || asId(tweet.rest_id) !== want) return;
        const normalized = normalize(tweet);
        if (score(normalized) > score(best)) best = normalized;
      },
      new Set()
    );
    return best;
  }

  globalThis.XCollectNormalize = { extractTweet, score };
})();
