#!/usr/bin/env python3
"""
ちいかわ一気読み  あたらしい話の自動チェック（GitHub Actions 用）

リポジトリの中だけで動く。ログイン不要。
  読むだけ: index.html（はじめから入っている話）/ add_urls.txt（手で足したいURL）
  書くのは: chiikawa_update.json（あとから足した話）/ chiikawa_update_state.json（作業メモ）
index.html は書きかえない。アプリが chiikawa_update.json を読みこんで、話をふやす。
"""

import datetime as dt
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ACCOUNT = "ngnchiikawa"
UA = "Mozilla/5.0 (chiikawa-ikkiyomi personal reader)"
EPOCH_MS = 1288834974657
WAIT = 0.5
MAX_CHECK = 200                       # 1回に確認する上限（念のため）
STATUS_RE = re.compile(r"/status(?:es)?/(\d{15,20})")
JST = dt.timezone(dt.timedelta(hours=9))

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "chiikawa_update.json"
STATE = ROOT / "chiikawa_update_state.json"


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


def id_date(tid):
    return dt.datetime.fromtimestamp(((int(tid) >> 22) + EPOCH_MS) / 1000, JST)


def id_from_date(d):
    return (int(d.timestamp() * 1000) - EPOCH_MS) << 22


def embed_token(tid):
    x = (int(tid) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole, frac = int(x), x - int(x)
    s = ""
    while whole:
        s = digits[whole % 36] + s
        whole //= 36
    for _ in range(10):
        frac *= 36
        s += digits[int(frac)]
        frac -= int(frac)
    return s.replace("0", "") or "a"


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def builtin_ids():
    html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"const BUILTIN='([\d,]+)'", html)
    return set(m.group(1).split(",")) if m else set()


def from_timeline():
    code, body = http_get(f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{ACCOUNT}?lang=ja")
    if code != 200:
        return set(), f"埋め込みタイムライン: 取れませんでした（{code or body[:60]}）"
    ids = set(re.findall(r'"(?:id_str|conversation_id_str)"\s*:\s*"(\d{15,20})"', body)) | set(STATUS_RE.findall(body))
    return ids, f"埋め込みタイムライン: {len(ids)} 件"


def from_wayback(since):
    found, notes = set(), []
    for host in ("twitter.com", "x.com"):
        q = urllib.parse.urlencode({
            "url": f"{host}/{ACCOUNT}/status/", "matchType": "prefix", "fl": "original",
            "collapse": "urlkey", "from": since.strftime("%Y%m%d"), "limit": "20000",
        })
        for attempt in range(3):
            code, body = http_get("https://web.archive.org/cdx/search/cdx?" + q, timeout=180)
            if code == 200:
                for line in body.splitlines():
                    if f"/{ACCOUNT}/status" in line.lower():
                        m = STATUS_RE.search(line)
                        if m:
                            found.add(m.group(1))
                break
            if code == 403:
                notes.append(f"{host}: 一覧検索が許可されていません")
                break
            time.sleep(10 * (attempt + 1))
        else:
            notes.append(f"{host}: 応答なし")
        time.sleep(2)
    return found, "Wayback Machine: " + f"{len(found)} 件" + (f"（{'、'.join(notes)}）" if notes else "")


def from_add_file():
    f = ROOT / "add_urls.txt"
    if not f.exists():
        return set(), "add_urls.txt: なし"
    text = f.read_text(encoding="utf-8", errors="replace")
    ids = set(STATUS_RE.findall(text)) | set(re.findall(r"(?<!\d)(\d{15,20})(?!\d)", text))
    return ids, f"add_urls.txt: {len(ids)} 件"


def check(tid, allow_reply):
    q = urllib.parse.urlencode({"id": tid, "lang": "ja", "token": embed_token(tid)})
    code, body = http_get("https://cdn.syndication.twimg.com/tweet-result?" + q, timeout=30)
    if code == 404:
        return "skip", []
    if code != 200:
        return "retry", []
    try:
        t = json.loads(body)
    except ValueError:
        return "retry", []
    if not isinstance(t, dict) or t.get("__typename") != "Tweet":
        return "skip", []
    if (t.get("user") or {}).get("screen_name", "").lower() != ACCOUNT:
        return "skip", []
    if t.get("in_reply_to_status_id_str") and not allow_reply:
        return "skip", []
    urls = [m.get("media_url_https") for m in t.get("mediaDetails") or [] if m.get("type") == "photo"]
    if not urls:
        urls = [p.get("url") for p in t.get("photos") or []]
    urls = [u for u in urls if u]
    return ("keep", urls) if urls else ("skip", [])


def summary(lines):
    print("\n".join(lines))
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    builtin = builtin_ids()
    if not builtin:
        raise SystemExit("index.html から話の一覧が読めませんでした。")
    out = load_json(OUT, {"version": 1, "account": ACCOUNT, "episodes": {}})
    episodes = out.setdefault("episodes", {})
    state = load_json(STATE, {"checked": {}})
    checked = state.setdefault("checked", {})

    have = builtin | set(episodes) | set(map(str, out.get("hide", [])))
    newest = max(have, key=int)
    since = (id_date(newest) - dt.timedelta(days=3)).date()
    floor_id = id_from_date(dt.datetime.combine(since, dt.time(), JST))

    manual, n1 = from_add_file()
    tl, n2 = from_timeline()
    wb, n3 = from_wayback(since)
    auto = {i for i in tl | wb if int(i) >= floor_id}
    skipped = {i for i, v in checked.items() if v == "skip"}
    cands = sorted((auto | manual) - have - skipped, key=int)[:MAX_CHECK]

    added, retry = [], 0
    for tid in cands:
        res, urls = check(tid, allow_reply=tid in manual)
        if res == "retry":
            retry += 1
            time.sleep(5)
            continue
        checked[tid] = res
        if res == "keep":
            episodes[tid] = urls
            added.append(tid)
        time.sleep(WAIT)

    # 中身が同じなら書きこみ結果も同じになるので、変化がない回はコミットされない
    out.update(version=1, account=ACCOUNT)
    out["episodes"] = {k: episodes[k] for k in sorted(episodes, key=int)}
    if added:
        out["updated"] = dt.datetime.now(JST).isoformat(timespec="seconds")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    STATE.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    lines = [f"## ちいかわ 新しい話チェック（{dt.datetime.now(JST):%Y-%m-%d %H:%M}）", "",
             f"- いちばん新しい話: {id_date(newest):%Y-%m-%d}", f"- {n1}", f"- {n2}", f"- {n3}",
             f"- 確認した候補: {len(cands)} 件", ""]
    if added:
        lines.append(f"**あたらしい話 {len(added)} 話を追加しました**")
        lines += [f"- {id_date(t):%Y-%m-%d} https://x.com/{ACCOUNT}/status/{t}" for t in sorted(added, key=int)]
    else:
        lines.append("あたらしい話はありませんでした。")
    if retry:
        lines.append(f"（{retry} 件は確認できなかったので、次の回でもう一度見ます）")
    summary(lines)


if __name__ == "__main__":
    main()
