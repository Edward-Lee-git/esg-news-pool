# -*- coding: utf-8 -*-
"""
News pool collector
===================
검색어 목록을 받아 네이버 뉴스 검색 API와 구글 뉴스 RSS로 기사 목록을 수집하고,
세 개의 텍스트 파일로 나누어 저장한다.

검색어는 환경변수 QUERIES_JSON 으로 주입한다(저장소에 남기지 않는다).
형식:
  {
    "set_a": {"naver": [...], "rss": [...]},
    "set_b": {"naver": [...], "rss": [...]},
    "set_c": {"naver": [...], "rss": [...]},
    "sweep": ["impacton.net", "esgeconomy.com", ...]
  }
sweep 결과는 세 파일에 모두 포함된다.

출력: pool/set-a.txt / set-b.txt / set-c.txt
  각 줄 형식:  YYYY-MM-DD HH:MM | 매체 | 제목 | URL
  검색어나 분류 정보는 기록하지 않는다.

환경변수:
  NAVER_API_KEY_ID, NAVER_API_KEY  (없으면 RSS만 사용)
  QUERIES_JSON                     (없으면 config/queries.sample.json 사용)
"""

import os
import re
import json
import time
import html
import urllib.parse
import datetime as dt
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
import feedparser
import holidays

KST = ZoneInfo("Asia/Seoul")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAVER_ENDPOINT = "https://naverapihub.apigw.ntruss.com/search/v1/news"
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"

MAX_LINES = 1200         # 세트당 최대 기사 수
LINES_PER_FILE = 250     # 파일 1개당 줄 수 (읽는 쪽 용량 제한 대응)
SETS = ["set_a", "set_b", "set_c"]


# ---------------------------------------------------------------
def resolve_window(now=None):
    """전일부터 거꾸로 올라가 처음 만나는 평일 00:00을 시작점으로 삼는다.
    토·일 및 한국 법정공휴일(대체공휴일 포함)은 건너뛴다."""
    now = now or dt.datetime.now(KST)
    kr = holidays.country_holidays("KR", years=[now.year - 1, now.year])
    d = now.date() - dt.timedelta(days=1)
    for _ in range(30):
        if d.weekday() < 5 and d not in kr:
            break
        d -= dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(0, 0), tzinfo=KST), now


def load_queries():
    raw = os.environ.get("QUERIES_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[FATAL] QUERIES_JSON 형식 오류: {e}")
    path = os.path.join(ROOT, "config", "queries.sample.json")
    print("[INFO] QUERIES_JSON 미설정 -> 샘플 검색어 사용")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------
def naver_search(query, key_id, key, max_items=300):
    out = []
    headers = {"X-NCP-APIGW-API-KEY-ID": key_id, "X-NCP-APIGW-API-KEY": key}
    for start in range(1, max_items, 100):
        try:
            r = requests.get(NAVER_ENDPOINT, headers=headers, timeout=15,
                             params={"query": query, "display": 100,
                                     "start": start, "sort": "date"})
            if r.status_code != 200:
                print(f"    [naver {r.status_code}] {r.text[:100]}")
                break
            items = r.json().get("items", [])
        except Exception as e:
            print(f"    [naver ERR] {e}")
            break
        if not items:
            break
        for it in items:
            link = it.get("originallink") or it.get("link", "")
            out.append({
                "title": clean(it.get("title", "")),
                "media": domain_of(link),
                "published": parse_rfc822(it.get("pubDate", "")),
                "url": link,
            })
        if len(items) < 100:
            break
        time.sleep(0.12)
    return out


def google_rss(query):
    out = []
    try:
        feed = feedparser.parse(GOOGLE_RSS.format(q=urllib.parse.quote(query)))
    except Exception as e:
        print(f"    [rss ERR] {e}")
        return out
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)\
                    .astimezone(KST).isoformat()
        out.append({
            "title": clean(e.get("title", "")),
            "media": (e.get("source", {}) or {}).get("title", "")
                     or domain_of(e.get("link", "")),
            "published": pub,
            "url": e.get("link", ""),
        })
    return out


# ---------------------------------------------------------------
def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def domain_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def parse_rfc822(s):
    try:
        return dt.datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")\
                 .astimezone(KST).isoformat()
    except Exception:
        return None


def norm(t):
    return re.sub(r"[^0-9a-z가-힣]", "", (t or "").lower())


def dedupe(items, threshold=0.88):
    seen, kept = set(), []
    for a in items:
        u = (a.get("url") or "").split("?")[0]
        if u and u in seen:
            continue
        n = norm(a["title"])
        if not n:
            continue
        if any(abs(len(n) - len(k["_n"])) <= 25
               and SequenceMatcher(None, n, k["_n"]).ratio() >= threshold
               for k in kept):
            continue
        if u:
            seen.add(u)
        a["_n"] = n
        kept.append(a)
    for k in kept:
        k.pop("_n", None)
    return kept


def in_window(a, start, end):
    p = a.get("published")
    if not p:
        return True
    try:
        return start <= dt.datetime.fromisoformat(p) <= end + dt.timedelta(hours=1)
    except Exception:
        return True


def resolve_google_links(items, workers=16, cap=2500):
    """구글 뉴스 RSS가 주는 중계 주소를 원출처 주소로 바꾼다.
    - 링크 길이가 크게 줄어 파일 용량이 절반 이하로 떨어진다
    - 리포트에 원출처 링크가 실린다
    실패하면 원래 주소를 그대로 둔다(수집 자체는 실패시키지 않는다)."""
    targets = [a for a in items
               if "news.google.com" in (a.get("url") or "")][:cap]
    if not targets:
        return items

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    ok = 0

    def one(a):
        nonlocal ok
        try:
            r = session.get(a["url"], timeout=8, allow_redirects=True)
            final = r.url or ""
            if final and "news.google.com" not in final:
                a["url"] = final.split("?")[0]
                if not a.get("media"):
                    a["media"] = domain_of(final)
                ok += 1
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, targets))

    print(f"  링크 정리: {len(targets)}건 중 {ok}건 원출처 확인 "
          f"({ok*100//max(len(targets),1)}%)")
    return items


def balance_by_date(items, limit):
    """건수 상한을 넘을 때, 날짜별로 골고루 남긴다.
    최신순으로 자르면 검색기간 첫날(월요일 기준 금요일) 기사가 통째로
    날아가므로, 각 날짜에서 돌아가며 뽑는다."""
    if len(items) <= limit:
        return items, False
    by_day = {}
    for a in items:
        by_day.setdefault((a.get("published") or "")[:10], []).append(a)
    for k in by_day:
        by_day[k].sort(key=lambda a: (a.get("published") or ""), reverse=True)

    days = sorted(by_day.keys(), reverse=True)
    picked, i = [], 0
    while len(picked) < limit:
        added = False
        for d in days:
            if i < len(by_day[d]) and len(picked) < limit:
                picked.append(by_day[d][i])
                added = True
        if not added:
            break
        i += 1

    kept = {}
    for a in picked:
        kept.setdefault((a.get("published") or "")[:10], 0)
        kept[(a.get("published") or "")[:10]] += 1
    print(f"      날짜별 배분: " +
          ", ".join(f"{d[5:]} {n}건" for d, n in sorted(kept.items())))
    return picked, True


def to_lines(items):
    lines = []
    for a in items:
        pub = (a.get("published") or "")[:16].replace("T", " ") or "날짜미상"
        title = a["title"].replace("|", "/")
        lines.append(f"{pub} | {a.get('media','')} | {title} | {a.get('url','')}")
    return lines


# ---------------------------------------------------------------
def main():
    key_id = os.environ.get("NAVER_API_KEY_ID", "")
    key = os.environ.get("NAVER_API_KEY", "")
    if not (key_id and key):
        print("[WARN] 네이버 키 미설정 -> RSS만 사용합니다")

    start, end = resolve_window()
    days = (end.date() - start.date()).days + 1
    print(f"기간: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} (KST), {days}일치\n")

    cfg = load_queries()

    # 공통 매체 훑기
    sweep = []
    for site in cfg.get("sweep", []):
        got = google_rss(f"site:{site} when:{days}d")
        print(f"  sweep {site}: +{len(got)}")
        sweep += got

    if sweep:
        print()
        sweep = resolve_google_links(sweep)

    outdir = os.path.join(ROOT, "pool")
    os.makedirs(outdir, exist_ok=True)
    summary = []

    for s in SETS:
        spec = cfg.get(s) or {}
        items = []
        if key_id and key:
            for q in spec.get("naver", []):
                items += naver_search(q, key_id, key)
        for q in spec.get("rss", []):
            items += google_rss(f"{q} when:{days}d")
        raw_n = len(items)

        items += sweep
        items = [a for a in items if in_window(a, start, end)]
        items = dedupe(items)
        items.sort(key=lambda a: (a.get("published") or ""), reverse=True)

        items = resolve_google_links(items)
        print(f"      기간내 고유 기사 {len(items)}건")
        items, cut = balance_by_date(items, MAX_LINES)

        lines = to_lines(items)

        # 읽는 쪽 용량 제한 때문에 파일당 LINES_PER_FILE 줄로 나눈다
        base = s.replace("_", "-")
        chunks = [lines[i:i + LINES_PER_FILE]
                  for i in range(0, len(lines), LINES_PER_FILE)] or [[]]
        names = []
        for idx, chunk in enumerate(chunks, 1):
            header = [
                f"# collected: {end:%Y-%m-%d %H:%M} KST",
                f"# window: {start:%Y-%m-%d} 00:00 ~ {end:%Y-%m-%d %H:%M} KST",
                f"# part {idx} of {len(chunks)}, lines: {len(chunk)}",
                "# format: date | media | title | url",
                "",
            ]
            name = f"{base}-{idx}.txt"
            with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
                f.write("\n".join(header + chunk) + "\n")
            kb = os.path.getsize(os.path.join(outdir, name)) / 1024
            names.append(f"{name} ({len(chunk)}줄, {kb:.0f}KB)")

        # 지난번보다 파일 수가 줄었으면 남은 파일 삭제
        for old in range(len(chunks) + 1, 20):
            p = os.path.join(outdir, f"{base}-{old}.txt")
            if os.path.exists(p):
                os.remove(p)
            else:
                break

        print(f"  {base}: 고유수집 {raw_n} + 매체훑기 {len(sweep)} "
              f"-> {len(lines)}줄{' (절단됨)' if cut else ''}")
        for n in names:
            print(f"      {n}")
        summary.append(f"{base}: {len(lines)}줄 / {len(chunks)}개 파일")

    with open(os.path.join(outdir, "index.txt"), "w", encoding="utf-8") as f:
        f.write(f"collected: {end:%Y-%m-%d %H:%M} KST\n")
        f.write(f"window: {start:%Y-%m-%d} 00:00 ~ {end:%Y-%m-%d %H:%M} KST\n")
        f.write("\n".join(summary) + "\n")

    print(f"\n완료: {', '.join(summary)}")


if __name__ == "__main__":
    main()
