# 不联网的自测：贴文提取、服务落盘、重复收录、非法地址拒绝。
# 运行：py -3 server/selftest.py
import io
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

NODE = os.path.join(HERE, "_selftest_node.js")


def js_extract(fixture, status_id):
    source = (
        open(os.path.join(ROOT, "extension", "src", "normalize.js"), encoding="utf-8").read()
        + "\nconst fixture = "
        + json.dumps(fixture)
        + ";\nconst result = globalThis.XCollectNormalize.extractTweet(fixture, "
        + json.dumps(status_id)
        + ");\nprocess.stdout.write(JSON.stringify(result));\n"
    )
    with open(NODE, "w", encoding="utf-8") as fh:
        fh.write(source)
    try:
        out = subprocess.run(["node", NODE], capture_output=True, check=True)
    finally:
        os.remove(NODE)
    return json.loads(out.stdout.decode("utf-8"))


def fixture():
    def tweet(rest_id, text, handle, media, note=None):
        node = {
            "__typename": "Tweet",
            "rest_id": rest_id,
            "core": {
                "user_results": {
                    "result": {"legacy": {"name": "作者 " + handle, "screen_name": handle}}
                }
            },
            "legacy": {
                "full_text": text,
                "created_at": "Wed Sep 30 07:12:00 +0000 2026",
                "display_text_range": [0, len(text.split(" https://")[0].encode("utf-16-le")) // 2],
                "entities": {
                    "urls": [
                        {
                            "url": "https://t.co/xxxx",
                            "expanded_url": "https://example.com/post",
                            "display_url": "example.com/post",
                        }
                    ]
                },
                "extended_entities": {"media": media},
            },
        }
        if note:
            node["note_tweet"] = {"note_tweet_results": {"result": {"text": note}}}
        return node

    photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/abc.jpg"}
    video = {
        "type": "video",
        "video_info": {
            "variants": [
                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
                {"content_type": "video/mp4", "bitrate": 832000, "url": "https://video.twimg.com/low.mp4"},
                {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/high.mp4"},
            ]
        },
    }
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {
                "instructions": [
                    {
                        "entries": [
                            {
                                "content": {
                                    "itemContent": {
                                        "tweet_results": {
                                            "result": {
                                                "__typename": "TweetWithVisibilityResults",
                                                "tweet": tweet(
                                                    "111",
                                                    "短正文 https://t.co/xxxx",
                                                    "alice",
                                                    [photo, video],
                                                    note="这是长文全文，超过两百八十字。" * 20,
                                                ),
                                            }
                                        }
                                    }
                                }
                            },
                            {
                                "content": {
                                    "itemContent": {
                                        "tweet_results": {
                                            "result": tweet("222", "这是评论，不该被收录", "bob", [])
                                        }
                                    }
                                }
                            },
                        ]
                    }
                ]
            }
        }
    }


class FakeResponse(io.BytesIO):
    """替身响应：不真的联网，按地址返回固定内容，用来验证下载逻辑。"""

    def __init__(self, url):
        body = ("bytes-of-" + url).encode("utf-8")
        super().__init__(body)
        kind = "video/mp4" if url.endswith(".mp4") else "image/jpeg"
        self.headers = {"Content-Type": kind}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def stub_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    # 只拦截媒体下载。创作请求要打到测试里的假上游，不能被这里吞掉。
    if "twimg.com" not in url:
        return sys.modules[__name__].real_urlopen(req, timeout=timeout)
    return FakeResponse(url)


def post(port, payload):
    return post_raw(port, "/api/capture", payload)


def post_raw(port, path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        resp = exc
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def check(label, cond):
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        raise SystemExit(1)


def main():
    got = js_extract(fixture(), "111")
    check("抽到的是当前贴文而不是评论", got["id"] == "111")
    check("解开了 TweetWithVisibilityResults", got["author"]["handle"] == "alice")
    check("长文优先于 full_text", got["text"].startswith("这是长文全文"))
    check("正文没有被截到 280 字", len(got["text"]) > 280)
    check("图片取原图", got["media"][0]["url"].endswith("name=orig"))
    check("视频取最高码率的 mp4", got["media"][1]["url"].endswith("high.mp4"))
    check(
        "抽出链接的真实地址",
        got["urls"] == [{"url": "https://t.co/xxxx", "expanded": "https://example.com/post", "display": "example.com/post"}],
    )
    check("其他贴文不被误收录", js_extract(fixture(), "999") is None)

    tombstone = {"data": {"result": {"__typename": "TweetTombstone", "rest_id": "333"}}}
    check("已删除的贴文不收录", js_extract(tombstone, "333") is None)

    # m3u8-only
    only_stream = {
        "rest_id": "444",
        "legacy": {
            "full_text": "只有流",
            "extended_entities": {
                "media": [
                    {
                        "type": "video",
                        "video_info": {
                            "variants": [
                                {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"}
                            ]
                        },
                    }
                ]
            },
        },
    }
    streamed = js_extract(only_stream, "444")
    check("没有 mp4 时标记跳过", streamed["media"][0].get("skipped") == "m3u8-only")

    import server as srv

    sys.modules[__name__].real_urlopen = srv.urlopen

    # 不联网：下载函数换成按地址返回固定内容的桩，地址白名单仍走真实逻辑。
    srv.urlopen = stub_urlopen
    base = "https://pbs.twimg.com"

    tmp = tempfile.mkdtemp(prefix="collect-test-")
    srv.LIBRARY = tmp
    srv.INDEX_PATH = os.path.join(tmp, "index.json")
    srv.WORKS_DIR = os.path.join(tmp, "works")
    srv.WORKS_INDEX = os.path.join(srv.WORKS_DIR, "index.json")
    api = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    api_port = api.server_address[1]
    threading.Thread(target=api.serve_forever, daemon=True).start()

    try:
        payload = {
            "id": "111",
            "url": "https://x.com/alice/status/111",
            "text": "正文",
            "author": {"name": "作者", "handle": "alice"},
            "createdAt": "2026-09-30T07:12:00.000Z",
            "media": [
                {"type": "photo", "url": base + "/a.jpg"},
                {"type": "video", "url": base + "/b.mp4", "bitrate": 2176000},
                {"type": "video", "skipped": "m3u8-only"},
                {"type": "photo", "url": "https://evil.example/steal.jpg"},
            ],
        }

        first = post(api_port, payload)
        check("首次收录成功", first["status"] == "partial")
        check("跳过的视频被记录", len(first["skipped"]) == 1)
        check("非法地址被拒绝", len(first["failed"]) == 1)
        folder = os.path.join(tmp, "111")
        check("图片落盘", os.path.exists(os.path.join(folder, "media-1.jpg")))
        check("视频落盘", os.path.exists(os.path.join(folder, "media-2.mp4")))
        check("没下完时保留标记", os.path.exists(os.path.join(folder, ".incomplete")))

        again = post(api_port, payload)
        check("未完成的收录会重试而不是直接算已存在", again["status"] == "partial")

        # 去掉失败项再收录一次，应当收尾并清除标记。
        payload["media"] = payload["media"][:3]
        done = post(api_port, payload)
        check("补齐后状态为已保存", done["status"] == "saved")
        check("完成后清除标记", not os.path.exists(os.path.join(folder, ".incomplete")))

        final = post(api_port, payload)
        check("完成后重复收录返回已存在", final["status"] == "exists")
        names = sorted(os.listdir(folder))
        check("没有产生第二份文件", names == ["media-1.jpg", "media-2.mp4", "tweet.json"])

        bad = post(api_port, {"id": "../etc", "text": "x"})
        check("非法 id 被拒绝", bad["ok"] is False)
        check("没有写出库目录之外的文件", not os.path.exists(os.path.join(os.path.dirname(tmp), "etc")))

        with urllib.request.urlopen("http://127.0.0.1:%d/api/items?q=alice" % api_port) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data["items"]
        check("搜索能按作者找到", len(items) == 1 and items[0]["id"] == "111")
        check("搜索结果带总数", data["total"] == 1)
        check("索引带封面文件名", items[0]["cover"] == "media-1.jpg")

        with urllib.request.urlopen("http://127.0.0.1:%d/api/items?offset=5" % api_port) as resp:
            beyond = json.loads(resp.read().decode("utf-8"))
        check("偏移超出总数时为空", beyond["items"] == [] and beyond["total"] == 1)

        # 旧索引没有 cover 字段，查询时应从 tweet.json 补上并回写。
        srv.write_index([{k: v for k, v in items[0].items() if k != "cover"}])
        with urllib.request.urlopen("http://127.0.0.1:%d/api/items" % api_port) as resp:
            repaired = json.loads(resp.read().decode("utf-8"))["items"]
        check("旧索引自动补封面", repaired[0].get("cover") == "media-1.jpg")

        with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/items?q=%s" % (api_port, urllib.parse.quote("不存在"))
        ) as resp:
            empty = json.loads(resp.read().decode("utf-8"))["items"]
        check("搜索无匹配时为空", empty == [])

        with urllib.request.urlopen("http://127.0.0.1:%d/" % api_port) as resp:
            page = resp.read().decode("utf-8")
        check("浏览页能打开", "素材库" in page)

        # 创作：用一个本地假上游，验证提示词填入了素材正文、结果被解析成 JSON。
        class FakeUpstream(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                prompt = body["messages"][0]["content"]
                assert "正文" in prompt, prompt
                answer = json.dumps({"title": "测试标题", "body": "测试正文", "tags": ["标签"]}, ensure_ascii=False)
                chunk = json.dumps({"choices": [{"delta": {"content": answer}}]})
                payload = ("data: " + chunk + "\n\ndata: [DONE]\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        srv.CONFIG_PATH = os.path.join(tmp, "config.json")
        srv.save_config(
            {
                "base_url": "http://127.0.0.1:%d" % upstream.server_address[1],
                "api_key": "test-key",
                "model": "fake",
                "endpoint": "chat",
                "prompts": [],
            }
        )
        with urllib.request.urlopen("http://127.0.0.1:%d/api/config" % api_port) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        check("配置不回显密钥", cfg["has_key"] is True and "api_key" not in cfg)
        check("内置模板始终存在", any(item["name"] == "小红书图文" for item in cfg["prompts"]))

        compose_req = urllib.request.Request(
            "http://127.0.0.1:%d/api/compose" % api_port,
            data=json.dumps({"id": "111", "prompt": "改写：{text}"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(compose_req) as resp:
            raw_body = resp.read().decode("utf-8")
        events = [
            json.loads(line[len("data:"):])
            for line in raw_body.split("\n")
            if line.startswith("data:")
        ]
        if not events:
            print("DEBUG compose body:", repr(raw_body[:400]))
        done = events[-1]
        check("创作结果被解析", done.get("result", {}).get("title") == "测试标题" and done.get("result", {}).get("tags") == ["标签"])
        check("没有分页时退回单页", done.get("result", {}).get("pages") == ["测试正文"])

        export_req = urllib.request.Request(
            "http://127.0.0.1:%d/api/export" % api_port,
            data=json.dumps(
                {"title": "标题", "body": "正文", "tags": ["甲"], "images": [{"id": "111", "file": "media-1.jpg"}]}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(export_req) as resp:
            import zipfile as zf

            pack = zf.ZipFile(io.BytesIO(resp.read()))
        check("导出包含文案和图片", "文案.md" in pack.namelist() and "images/1.jpg" in pack.namelist())

        bad_export = post_raw(
            api_port,
            "/api/export",
            {"title": "x", "body": "", "tags": [], "images": [{"id": "111", "file": "../server.py"}]},
        )
        check("导出拒绝库外文件", bad_export["ok"] is False)

        saved = post_raw(
            api_port,
            "/api/works",
            {"sourceId": "111", "title": "作品标题", "body": "作品正文", "tags": ["甲"], "pages": ["封面一句", "第二页要点"], "theme": "nope", "images": [{"file": "media-1.jpg"}], "author": {"name": "作者"}},
        )
        check("作品保存成功", saved.get("title") == "作品标题" and saved.get("sourceId") == "111")
        check("作品带各页文字和字号", saved.get("pages") == [{"text": "封面一句", "size": 0}, {"text": "第二页要点", "size": 0}])
        check("非法主题回退到默认", saved.get("theme") == "autumn")

        with urllib.request.urlopen("http://127.0.0.1:%d/api/items" % api_port) as resp:
            counted = json.loads(resp.read().decode("utf-8"))["items"]
        check("素材标记已创作", counted[0].get("works") == 1 and counted[0]["workList"][0]["title"] == "作品标题")

        with urllib.request.urlopen("http://127.0.0.1:%d/api/works" % api_port) as resp:
            works = json.loads(resp.read().decode("utf-8"))["items"]
        check("作品列表能读到", len(works) == 1 and works[0]["id"] == saved["id"])

        del_req = urllib.request.Request("http://127.0.0.1:%d/api/tweets/111" % api_port, method="DELETE")
        with urllib.request.urlopen(del_req) as resp:
            deleted = json.loads(resp.read().decode("utf-8"))
        check("删除素材成功", deleted.get("ok") is True)
        check("素材目录被清掉", not os.path.exists(os.path.join(tmp, "111")))
        with urllib.request.urlopen("http://127.0.0.1:%d/api/works" % api_port) as resp:
            after = json.loads(resp.read().decode("utf-8"))["items"]
        check("素材的作品一并删除", after == [])

        # 作品只引用素材图片，不复制文件。
        check("作品不复制图片", not os.path.exists(os.path.join(tmp, "works", saved["id"], "media-1.jpg")))

        upstream.shutdown()
    finally:
        api.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("全部通过")


if __name__ == "__main__":
    main()
