# 本地素材库服务。只依赖 Python 标准库，只监听本机。
# 启动：py -3 server/server.py
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import zipfile
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HOST = "127.0.0.1"
PORT = 8787
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = os.path.join(ROOT, "library")
STATIC = os.path.join(HERE, "static")
INDEX_PATH = os.path.join(LIBRARY, "index.json")
WORKS_DIR = os.path.join(LIBRARY, "works")
WORKS_INDEX = os.path.join(WORKS_DIR, "index.json")
CONFIG_PATH = os.path.join(HERE, "config.json")
COMPOSE_TIMEOUT = 120
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
ALLOWED_HOSTS = {"pbs.twimg.com", "video.twimg.com"}
MAX_BYTES = 512 * 1024 * 1024
TIMEOUT = 60
ID_RE = re.compile(r"^\d{1,30}$")
EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}

index_lock = threading.Lock()
config_lock = threading.Lock()

# 内置模板不可删除。{text} 会被替换成素材正文，要求模型返回严格 JSON，方便拆成可编辑的字段。
BUILTIN_PROMPTS = [
    {
        "name": "小红书图文",
        "builtin": True,
        "text": (
            "你是小红书文案作者。根据下面的素材写一篇小红书图文笔记，按页拆分。\n"
            "篇幅要求：一共 {pages} 页，每页不超过 {per_page} 个字。素材超过 800 字时先精简到 800 字以内再拆。\n"
            "第一页是封面，8 到 16 个字。用【】标出每页最关键的 1 到 2 个词，比如【敏感肌】怎么选。\n"
            "另给标题（不超过 20 字）、正文（口语化，分段）和 4 到 8 个话题标签，标签不带 # 号。\n"
            "只返回 JSON，不要其他文字："
            "{\"title\": \"\", \"pages\": [\"封面文字\", \"第二页文字\"], \"body\": \"\", \"tags\": []}\n\n"
            "素材：\n{text}"
        ),
    }
]


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"base_url": "", "api_key": "", "model": "", "endpoint": "chat", "prompts": []}
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("prompts", [])
    return data


def save_config(data):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def public_config():
    """返回给页面的配置。密钥只暴露是否已填写，不回明文。"""
    data = load_config()
    prompts = list(BUILTIN_PROMPTS)
    for item in data.get("prompts") or []:
        if isinstance(item, dict) and item.get("name") and not item.get("builtin"):
            prompts.append({"name": str(item["name"]), "builtin": False, "text": str(item.get("text") or "")})
    return {
        "base_url": data.get("base_url") or "",
        "has_key": bool(data.get("api_key")),
        "model": data.get("model") or "",
        "endpoint": data.get("endpoint") or "chat",
        "prompts": prompts,
    }


def apply_config(payload):
    with config_lock:
        data = load_config()
        for key in ("base_url", "model"):
            if key in payload:
                data[key] = str(payload[key]).strip()
        if payload.get("endpoint") in ("chat", "responses"):
            data["endpoint"] = payload["endpoint"]
        # 留空表示不修改密钥，避免页面为了回显而把明文拿回去。
        if payload.get("api_key"):
            data["api_key"] = str(payload["api_key"]).strip()
        if isinstance(payload.get("prompts"), list):
            data["prompts"] = [
                {"name": str(item["name"])[:40], "text": str(item.get("text") or "")}
                for item in payload["prompts"]
                if isinstance(item, dict) and item.get("name") and not item.get("builtin")
            ]
        save_config(data)
    return public_config()


def read_tweet(tweet_id):
    meta = os.path.join(safe_dir(tweet_id), "tweet.json")
    with open(meta, encoding="utf-8") as fh:
        return json.load(fh)


# 字数档位决定页数和每页上限。排版好不好看取决于一页里有多少字，所以在生成时就卡住。
LENGTH_TIERS = [
    (40, 1, 40, "极短"),
    (150, 3, 50, "短文"),
    (400, 4, 80, "中文"),
    (800, 6, 110, "长文"),
    (10 ** 9, 6, 110, "超限"),
]


def tier_for(text, override=""):
    length = len(text or "")
    tiers = {name: (pages, per_page) for _limit, pages, per_page, name in LENGTH_TIERS}
    if override in tiers:
        pages, per_page = tiers[override]
        return {"name": override, "pages": pages, "perPage": per_page, "length": length}
    for limit, pages, per_page, name in LENGTH_TIERS:
        if length <= limit:
            return {"name": name, "pages": pages, "perPage": per_page, "length": length}
    return {"name": "超限", "pages": 6, "perPage": 110, "length": length}


def fill_prompt(template, text, tier=None):
    tier = tier or tier_for(text)
    return (
        template.replace("{text}", text)
        .replace("{pages}", str(tier["pages"]))
        .replace("{per_page}", str(tier["perPage"]))
    )


def upstream_request(prompt):
    """按配置构造上游请求。chat 和 responses 的请求体不同，分开写。"""
    data = load_config()
    base = (data.get("base_url") or "").rstrip("/")
    key = data.get("api_key") or ""
    model = data.get("model") or ""
    if not base or not key or not model:
        raise ValueError("还没配置模型。先到设置里填写接口地址、密钥和模型名。")
    if data.get("endpoint") == "responses":
        url = base + "/responses"
        body = {"model": model, "input": prompt, "stream": True}
    else:
        url = base + "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": True}
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    return req


def take_delta(line, collected):
    """解析一条 SSE 行，把增量文本追加到 collected，返回本次增量。"""
    if not line.startswith("data:"):
        return ""
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return ""
    delta = extract_delta(event)
    if delta:
        collected.append(delta)
    return delta


def extract_delta(event):
    """从一条 SSE 事件里取出新增的文本。两种端点的增量字段不同。"""
    if event.get("choices"):
        delta = event["choices"][0].get("delta") or {}
        return delta.get("content") or ""
    if event.get("type") == "response.output_text.delta":
        return event.get("delta") or ""
    return ""


def parse_result(text):
    """小红书模板要求返回 JSON。解析失败就把原文放进正文，不报错。"""
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"title": "", "body": text.strip(), "tags": [], "pages": [text.strip()]}
    if not isinstance(data, dict):
        return {"title": "", "body": text.strip(), "tags": [], "pages": [text.strip()]}
    tags = [str(t) for t in data.get("tags") or [] if str(t).strip()]
    pages = [str(p).strip() for p in data.get("pages") or [] if str(p).strip()]
    if not pages:
        pages = [str(data.get("body") or text.strip())]
    return {
        "title": str(data.get("title") or ""),
        "body": str(data.get("body") or ""),
        "tags": tags,
        "pages": pages,
    }


def build_zip(payload):
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "")
    tags = [str(t).lstrip("#").strip() for t in (payload.get("tags") or []) if str(t).strip()]
    lines = []
    if title:
        lines.append("# " + title)
        lines.append("")
    lines.append(body)
    if tags:
        lines.append("")
        lines.append(" ".join("#" + t for t in tags))
    doc = "\n".join(lines).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("文案.md", doc)
        for index, item in enumerate(payload.get("images") or [], start=1):
            if not isinstance(item, dict):
                raise ValueError("配图格式不对")
            filename = str(item.get("file") or "")
            src = media_path(str(item.get("id") or ""), filename)
            if not os.path.exists(src):
                raise ValueError("找不到配图 " + filename)
            ext = os.path.splitext(filename)[1]
            zf.write(src, "images/%d%s" % (index, ext))
    safe_title = re.sub(r'[\\/:*?"<>|]', "", title)[:40] or "未命名"
    return buf.getvalue(), safe_title + ".zip"


def read_works_index():
    if not os.path.exists(WORKS_INDEX):
        return []
    with open(WORKS_INDEX, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def write_works_index(items):
    os.makedirs(WORKS_DIR, exist_ok=True)
    tmp = WORKS_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, WORKS_INDEX)


def works_by_source():
    """每条素材产出了多少作品、分别是哪些。列素材时用，不写回素材索引。"""
    counts = {}
    for item in read_works_index():
        source = str(item.get("sourceId") or "")
        counts.setdefault(source, []).append({"id": item.get("id"), "title": item.get("title") or ""})
    return counts


def save_work(payload):
    source = str(payload.get("sourceId") or "")
    if not ID_RE.match(source):
        raise ValueError("来源素材不对")
    # 确认素材真实存在，避免作品指向一条没有的素材。
    if not os.path.exists(os.path.join(safe_dir(source), "tweet.json")):
        raise ValueError("找不到来源素材")
    images = []
    for item in payload.get("images") or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("file") or "")
        src = media_path(source, filename)
        if os.path.exists(src):
            images.append({"id": source, "file": filename})
    incoming = str(payload.get("id") or "")
    # 带已有 id 就是更新，不新建。id 必须是纯数字，避免路径穿越。
    work_id = incoming if re.match(r"^\d{1,20}$", incoming) else str(int(time.time() * 1000))
    work = {
        "id": work_id,
        "platform": "xiaohongshu",
        "sourceId": source,
        "title": str(payload.get("title") or "")[:60],
        "body": str(payload.get("body") or ""),
        "tags": [str(t).lstrip("#").strip() for t in (payload.get("tags") or []) if str(t).strip()],
        "pages": [
            {"text": str(p.get("text") if isinstance(p, dict) else p).strip(), "size": int(p.get("size") or 0) if isinstance(p, dict) else 0}
            for p in (payload.get("pages") or [])
            if str(p.get("text") if isinstance(p, dict) else p).strip()
        ][:12],
        "theme": payload.get("theme") if payload.get("theme") in ("autumn", "sketch", "cartoon", "literary", "beige", "plain", "post") else "autumn",
        "images": images,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    folder = os.path.join(WORKS_DIR, work_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "work.json"), "w", encoding="utf-8") as fh:
        json.dump(work, fh, ensure_ascii=False, indent=2)
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    entry = {
        "id": work_id,
        "sourceId": source,
        "title": work["title"],
        "authorName": str(author.get("name") or ""),
        "createdAt": work["createdAt"],
        "imageCount": len(images),
    }
    items = [item for item in read_works_index() if item.get("id") != work_id]
    items.append(entry)
    write_works_index(items)
    return work


def delete_work(work_id):
    if not re.match(r"^\d{1,20}$", work_id):
        raise ValueError("bad id")
    folder = os.path.join(WORKS_DIR, work_id)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    write_works_index([item for item in read_works_index() if item.get("id") != work_id])


def delete_tweet(tweet_id):
    folder = safe_dir(tweet_id)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    with index_lock:
        write_index([item for item in read_index() if item.get("id") != tweet_id])
    # 素材没了，它产出的作品也一起清掉，避免作品指向一条不存在的素材。
    for item in list(read_works_index()):
        if item.get("sourceId") == tweet_id:
            delete_work(item.get("id") or "")


def read_index():
    if not os.path.exists(INDEX_PATH):
        return []
    with open(INDEX_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else []


def write_index(items):
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_PATH)


def entry_of(tweet, media_count, skipped):
    author = tweet.get("author") or {}
    files = [item.get("file") for item in (tweet.get("media") or []) if isinstance(item, dict)]
    cover = next((name for name in files if name), "")
    return {
        "id": tweet["id"],
        "authorName": str(author.get("name") or ""),
        "authorHandle": str(author.get("handle") or ""),
        "text": str(tweet.get("text") or "")[:80],
        "createdAt": str(tweet.get("createdAt") or ""),
        "capturedAt": tweet.get("capturedAt") or "",
        "mediaCount": media_count,
        "skipped": skipped,
        "cover": cover,
    }


def upsert_index(entry):
    with index_lock:
        items = [item for item in read_index() if item.get("id") != entry["id"]]
        items.append(entry)
        write_index(items)


def safe_dir(tweet_id):
    if not ID_RE.match(tweet_id):
        raise ValueError("bad id")
    path = os.path.join(LIBRARY, tweet_id)
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(LIBRARY)]) != os.path.abspath(LIBRARY):
        raise ValueError("bad id")
    return path


def allowed_url(raw):
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    # 端口也要卡死。只放行主机名的话，测试或误配置把 127.0.0.1 加进白名单后，
    # 同主机上其他端口的服务也能被这个接口请求到。
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        return False
    port = parsed.port
    return port in (None, 443)


MEDIA_NAME = re.compile(r"^media-\d+\.(jpg|png|webp|gif|mp4)$")
THUMB_MAX = 480


def media_path(tweet_id, filename):
    """校验 id 和文件名，返回库内的媒体文件路径。不合法抛 ValueError。"""
    if not MEDIA_NAME.match(filename):
        raise ValueError("bad filename")
    return os.path.join(safe_dir(tweet_id), filename)


def thumbnail(tweet_id, filename):
    """返回 (路径, content_type)。视频不截帧，返回占位标记让前端自己画。"""
    source = media_path(tweet_id, filename)
    if not os.path.exists(source):
        raise FileNotFoundError(source)
    if filename.endswith(".mp4"):
        return None, "video"
    cache = os.path.join(safe_dir(tweet_id), ".thumbs", filename + ".jpg")
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(source):
        return cache, "image/jpeg"
    from PIL import Image

    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with Image.open(source) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX, THUMB_MAX))
        img.save(cache + ".part", "JPEG", quality=80)
    os.replace(cache + ".part", cache)
    return cache, "image/jpeg"


def sniff_ext(content_type, url, kind):
    base = (content_type or "").split(";")[0].strip().lower()
    if base in EXT_BY_TYPE:
        return EXT_BY_TYPE[base]
    if base == "video/mp4" or kind in ("video", "animated_gif"):
        return ".mp4"
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def download(url, dest):
    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://x.com/", "Accept": "*/*"},
    )
    with urlopen(req, timeout=TIMEOUT) as resp:
        total = 0
        with open(dest + ".part", "wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("media too large")
                fh.write(chunk)
        return sniff_ext(resp.headers.get("Content-Type"), url, "")


def split_pending(folder, media):
    """把已落盘的和待下载的分开。编号按可下载项计数，和 save_media 的编号规则一致。"""
    done = {}
    for name in os.listdir(folder):
        match = re.match(r"^media-(\d+)\.(jpg|png|webp|gif|mp4)$", name)
        if match:
            done[int(match.group(1))] = name
    have = []
    need = []
    seq = 0
    for item in media:
        if not isinstance(item, dict) or item.get("skipped"):
            continue
        seq += 1
        if seq in done:
            have.append({"type": item.get("type") or "photo", "file": done[seq], "sourceUrl": item.get("url") or ""})
        else:
            need.append((seq, item))
    return have, need


def save_media(folder, numbered):
    """numbered 是 (序号, 媒体) 的列表，序号由 split_pending 给定，保证重试时文件名不变。"""
    saved = []
    failed = []
    skipped = []
    for seq, item in numbered:
        if not isinstance(item, dict):
            continue
        if item.get("skipped"):
            skipped.append({"type": item.get("type") or "video", "reason": str(item["skipped"])})
            continue
        url = item.get("url")
        if not isinstance(url, str) or not allowed_url(url):
            failed.append({"type": item.get("type") or "", "sourceUrl": url or "", "reason": "地址不在允许的范围内"})
            continue
        dest = os.path.join(folder, "media-%d" % seq)
        try:
            ext = download(url, dest)
            if item.get("type") in ("video", "animated_gif") and ext != ".mp4":
                ext = ".mp4"
            final = dest + ext
            os.replace(dest + ".part", final)
            saved.append(
                {
                    "type": item.get("type") or "photo",
                    "file": os.path.basename(final),
                    "sourceUrl": url,
                }
            )
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            part = dest + ".part"
            if os.path.exists(part):
                os.remove(part)
            failed.append({"type": item.get("type") or "", "sourceUrl": url, "reason": str(exc)})
    return saved, failed, skipped


def capture(payload):
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "bad-payload", "message": "请求内容不是 JSON 对象"}
    tweet_id = str(payload.get("id") or "")
    if not ID_RE.match(tweet_id):
        return 400, {"ok": False, "error": "bad-id", "message": "贴文 id 不合法"}
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    urls = []
    for item in payload.get("urls") if isinstance(payload.get("urls"), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        urls.append(
            {
                "url": item["url"],
                "expanded": str(item.get("expanded") or ""),
                "display": str(item.get("display") or ""),
            }
        )
    tweet = {
        "id": tweet_id,
        "url": str(payload.get("url") or ""),
        "text": str(payload.get("text") or ""),
        "author": {"name": str(author.get("name") or ""), "handle": str(author.get("handle") or "")},
        "createdAt": str(payload.get("createdAt") or ""),
        "urls": urls,
        "media": [],
    }

    folder = safe_dir(tweet_id)
    marker = os.path.join(folder, ".incomplete")
    if os.path.isdir(folder) and not os.path.exists(marker):
        return 200, {"ok": True, "status": "exists", "id": tweet_id, "skipped": [], "failed": []}

    os.makedirs(LIBRARY, exist_ok=True)
    os.makedirs(folder, exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("downloading\n")
    # 重试前清掉上次没下完的半截文件，已完成的媒体文件保留不动。
    for name in os.listdir(folder):
        if name.endswith(".part"):
            os.remove(os.path.join(folder, name))

    have, need = split_pending(folder, media)
    saved, failed, _downloaded = save_media(folder, need)
    skipped = [
        {"type": item.get("type") or "video", "reason": str(item.get("skipped"))}
        for item in media
        if isinstance(item, dict) and item.get("skipped")
    ]
    tweet["media"] = sorted(have + saved, key=lambda item: item["file"])
    tweet["skipped"] = skipped
    tweet["failed"] = failed
    with open(os.path.join(folder, "tweet.json"), "w", encoding="utf-8") as fh:
        json.dump(tweet, fh, ensure_ascii=False, indent=2)

    if failed:
        # 留着 .incomplete，下次收录同一条时会把没下到的媒体补上。
        status = "partial"
    else:
        if os.path.exists(marker):
            os.remove(marker)
        status = "saved"
    upsert_index(entry_of(tweet, len(tweet["media"]), len(skipped)))
    return 200, {"ok": True, "status": status, "id": tweet_id, "skipped": skipped, "failed": failed}


def resume_incomplete():
    if not os.path.isdir(LIBRARY):
        return
    for name in os.listdir(LIBRARY):
        folder = os.path.join(LIBRARY, name)
        marker = os.path.join(folder, ".incomplete")
        meta = os.path.join(folder, "tweet.json")
        if not (os.path.isdir(folder) and os.path.exists(marker) and os.path.exists(meta)):
            continue
        try:
            with open(meta, encoding="utf-8") as fh:
                tweet = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        pending = [
            (index + 1, {"type": item.get("type") or "", "url": item.get("sourceUrl")})
            for index, item in enumerate(tweet.get("failed") or [])
            if isinstance(item, dict) and item.get("sourceUrl")
        ]
        if not pending:
            continue
        print("续传 %s，还有 %d 个媒体" % (name, len(pending)), flush=True)
        saved, failed, _skipped = save_media(folder, pending)
        tweet["media"] = sorted((tweet.get("media") or []) + saved, key=lambda item: item.get("file", ""))
        tweet["failed"] = failed
        with open(meta, "w", encoding="utf-8") as fh:
            json.dump(tweet, fh, ensure_ascii=False, indent=2)
        if not failed and os.path.exists(marker):
            os.remove(marker)
        upsert_index(entry_of(tweet, len(tweet["media"]), len(tweet.get("skipped") or [])))


def query_items(q, offset=0, limit=30):
    needle = (q or "").strip().lower()
    items = read_index()
    if backfill_covers(items):
        write_index(items)
    if needle:
        items = [
            item
            for item in items
            if needle in (item.get("text") or "").lower()
            or needle in (item.get("authorName") or "").lower()
            or needle in (item.get("authorHandle") or "").lower()
        ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    counts = works_by_source()
    for item in items:
        related = counts.get(str(item.get("id") or ""), [])
        item["works"] = len(related)
        item["workList"] = related
    total = len(items)
    offset = max(0, offset)
    limit = min(max(1, limit), 60)
    return items[offset:offset + limit], total


def backfill_covers(items):
    """旧索引没有 cover 字段。从 tweet.json 补一次，避免升级后封面全空。"""
    changed = False
    for item in items:
        if "cover" in item:
            continue
        changed = True
        item["cover"] = ""
        try:
            meta = os.path.join(safe_dir(str(item.get("id") or "")), "tweet.json")
            with open(meta, encoding="utf-8") as fh:
                tweet = json.load(fh)
        except (ValueError, OSError, json.JSONDecodeError):
            continue
        files = [m.get("file") for m in (tweet.get("media") or []) if isinstance(m, dict) and m.get("file")]
        if files:
            item["cover"] = files[0]
    return changed


class Handler(BaseHTTPRequestHandler):
    server_version = "collect/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type, download_name=None):
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self.send_error(404)
            return
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            if download_name:
                self.send_header("Content-Disposition", "inline; filename=\"%s\"" % download_name)
            self.end_headers()
            shutil.copyfileobj(fh, self.wfile)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/config":
            self.send_json(200, public_config())
            return
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/works":
            items = sorted(read_works_index(), key=lambda item: item.get("createdAt") or "", reverse=True)
            self.send_json(200, {"items": items})
            return
        if path.startswith("/api/works/"):
            work_id = path[len("/api/works/"):]
            if not re.match(r"^\d{1,20}$", work_id):
                self.send_json(400, {"ok": False, "error": "bad-id"})
                return
            meta = os.path.join(WORKS_DIR, work_id, "work.json")
            if not os.path.exists(meta):
                self.send_json(404, {"ok": False, "error": "not-found"})
                return
            with open(meta, encoding="utf-8") as fh:
                self.send_json(200, json.load(fh))
            return
        if path == "/api/items":
            params = parse_qs(parsed.query)
            q = params.get("q", [""])[0]
            try:
                offset = int(params.get("offset", ["0"])[0])
                limit = int(params.get("limit", ["30"])[0])
            except ValueError:
                self.send_json(400, {"ok": False, "error": "bad-paging"})
                return
            items, total = query_items(q, offset, limit)
            self.send_json(200, {"items": items, "total": total})
            return
        if path.startswith("/api/tweets/"):
            tweet_id = path[len("/api/tweets/"):]
            try:
                meta = os.path.join(safe_dir(tweet_id), "tweet.json")
            except ValueError:
                self.send_json(400, {"ok": False, "error": "bad-id"})
                return
            if not os.path.exists(meta):
                self.send_json(404, {"ok": False, "error": "not-found"})
                return
            with open(meta, encoding="utf-8") as fh:
                self.send_json(200, json.load(fh))
            return
        if path.startswith("/media/") or path.startswith("/thumb/"):
            parts = path.split("/")
            if len(parts) != 4:
                self.send_error(404)
                return
            _, kind, tweet_id, filename = parts
            try:
                if kind == "thumb":
                    file_path, content_type = thumbnail(tweet_id, filename)
                    if file_path is None:
                        self.send_json(200, {"ok": False, "video": True})
                        return
                    self.send_file(file_path, content_type, filename + ".jpg")
                    return
                file_path = media_path(tweet_id, filename)
            except (ValueError, FileNotFoundError):
                self.send_error(404)
                return
            content_type = "video/mp4" if filename.endswith(".mp4") else "image/" + filename.rsplit(".", 1)[1]
            if filename.endswith(".jpg"):
                content_type = "image/jpeg"
            self.send_file(file_path, content_type, filename)
            return
        if path in ("/", "/index.html"):
            self.send_file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
            return
        if path.startswith("/backgrounds/") or path.startswith("/fonts/"):
            name = path.split("/", 2)[2]
            if not re.match(r"^[A-Za-z0-9_-]+\.(jpg|ttf|otf)$", name):
                self.send_error(404)
                return
            folder = "backgrounds" if path.startswith("/backgrounds/") else "fonts"
            kind = {"jpg": "image/jpeg", "ttf": "font/ttf", "otf": "font/otf"}[name.rsplit(".", 1)[1]]
            self.send_file(os.path.join(STATIC, folder, name), kind)
            return
        self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8 * 1024 * 1024:
            self.send_json(400, {"ok": False, "error": "bad-length", "message": "请求体为空或过大"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "bad-json", "message": "不是合法的 JSON"})
            return None

    def stream_compose(self, prompt):
        """读上游的 SSE，转成自己的 SSE 回给浏览器。最后一条事件带解析后的结果。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(payload):
            line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

        try:
            req = upstream_request(prompt)
        except ValueError as exc:
            emit({"error": str(exc)})
            return
        collected = []
        try:
            with urlopen(req, timeout=COMPOSE_TIMEOUT) as resp:
                pending = ""
                while True:
                    block = resp.read(1024)
                    if not block:
                        break
                    pending += block.decode("utf-8", "replace")
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        delta = take_delta(line.strip(), collected)
                        if delta:
                            emit({"delta": delta})
                # 上游没以换行结尾时，最后一段也要处理。
                delta = take_delta(pending.strip(), collected)
                if delta:
                    emit({"delta": delta})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            emit({"error": "模型接口返回 %d：%s" % (exc.code, detail)})
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            emit({"error": "连接模型接口失败：" + str(exc)})
            return
        emit({"done": True, "result": parse_result("".join(collected))})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            payload = self.read_json()
            if payload is None:
                return
            self.send_json(200, apply_config(payload))
            return
        if path == "/api/works":
            payload = self.read_json()
            if payload is None:
                return
            try:
                work = save_work(payload)
            except (ValueError, OSError) as exc:
                self.send_json(400, {"ok": False, "error": "bad-work", "message": str(exc)})
                return
            self.send_json(200, work)
            return
        if path == "/api/compose":
            payload = self.read_json()
            if payload is None:
                return
            try:
                tweet = read_tweet(str(payload.get("id") or ""))
            except (ValueError, OSError, json.JSONDecodeError):
                self.send_json(404, {"ok": False, "error": "not-found", "message": "找不到这条素材"})
                return
            template = str(payload.get("prompt") or "")
            if not template.strip():
                self.send_json(400, {"ok": False, "error": "empty-prompt", "message": "提示词是空的"})
                return
            tier = tier_for(tweet.get("text") or "", str(payload.get("tier") or ""))
            self.stream_compose(fill_prompt(template, tweet.get("text") or "", tier))
            return
        if path == "/api/export":
            payload = self.read_json()
            if payload is None:
                return
            try:
                data, name = build_zip(payload)
            except (ValueError, OSError) as exc:
                self.send_json(400, {"ok": False, "error": "bad-export", "message": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", "attachment; filename=\"note.zip\"")
            self.send_header("X-Filename", urllib.parse.quote(name))
            self.end_headers()
            self.wfile.write(data)
            return
        if path != "/api/capture":
            self.send_error(404)
            return
        payload = self.read_json()
        if payload is None:
            return
        code, result = capture(payload)
        self.send_json(code, result)

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/works/"):
                delete_work(path[len("/api/works/"):])
            elif path.startswith("/api/tweets/"):
                delete_tweet(path[len("/api/tweets/"):])
            else:
                self.send_error(404)
                return
        except ValueError:
            self.send_json(400, {"ok": False, "error": "bad-id"})
            return
        self.send_json(200, {"ok": True})


def main():
    os.makedirs(LIBRARY, exist_ok=True)
    threading.Thread(target=resume_incomplete, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("素材库已启动： http://%s:%d/" % (HOST, PORT), flush=True)
    print("库目录： %s" % LIBRARY, flush=True)
    print("关闭这个窗口即停止服务。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)


if __name__ == "__main__":
    main()
