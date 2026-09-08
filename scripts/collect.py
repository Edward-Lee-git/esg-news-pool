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
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"

# 구글 뉴스 지역·언어 코드. 해외 현지 보도를 잡기 위해 사용한다.
# 네이버 뉴스 검색 API는 국내 등록 매체만 다루므로 해외 보도는 여기서만 확보된다.
LOCALES = {
    "kr": ("ko",    "KR", "ko"),   # 한국어
    "en": ("en-US", "US", "en"),   # 영어 (미국)
    "gb": ("en-GB", "GB", "en"),   # 영어 (영국)
    "id": ("id",    "ID", "id"),   # 인도네시아어
    "ro": ("ro",    "RO", "ro"),   # 루마니아어
}

LINES_PER_FILE = 250     # 파일 1개당 줄 수 (읽는 쪽 용량 제한 대응)

# 세트별 설정
#   sweep : 매체 전수 훑기 결과를 포함할지
#   limit : 최대 기사 수
# set_a(부정기사)는 회사명 검색 결과가 핵심이므로 매체 훑기를 넣지 않는다.
# 넣으면 훑기 결과가 상한을 차지해 회사 검색 결과가 잘려나간다.
SETS = {
    "set_a": {"sweep": False, "limit": 900},
    "set_b": {"sweep": True,  "limit": 1200},
    "set_c": {"sweep": True,  "limit": 1200},
}


# ---------------------------------------------------------------
# 검색어 검증 및 리스크 신호
# ---------------------------------------------------------------
def verify(query, text):
    """검색어의 모든 낱말이 대상 텍스트(제목+요약)에 실제로 있는지 확인한다.
    네이버 검색이 형태소 분해로 돌려준 무관한 기사를 걸러내는 용도."""
    t = (text or "").lower().replace(" ", "")
    for w in re.split(r"\s+", (query or "").strip().strip('"')):
        w = w.strip('"').lower().replace(" ", "")
        if not w or w in ("or", "and"):
            continue
        if w not in t:
            return False
    return True


# 제목에 이 표현이 있으면 부정이슈 가능성이 있는 기사로 본다.
# 검색어가 본문 깊숙이만 언급된 기사(요약문에 안 잡히는 경우)를 살려두기 위한 장치.
RISK_WORDS = [
    "제재", "처분", "과징금", "벌금", "시정명령", "불승인", "불허", "반려",
    "적발", "고발", "기소", "수사", "조사", "압수", "감사", "지적", "논란",
    "의혹", "혐의", "담합", "배임", "횡령", "탈세", "소송", "피소", "패소",
    "사고", "사망", "부상", "붕괴", "화재", "누출", "재해", "리콜", "결함",
    "하자", "불량", "파업", "쟁의", "해고", "갑질", "괴롭힘", "성희롱",
    "차별", "오염", "그린워싱", "인권", "아동노동", "강제노동",
    "유상증자", "지분희석", "주주가치", "소액주주", "반발", "항의", "시위",
    "국정감사", "청문회", "제동", "중단", "취소", "박탈", "경고",
    "scandal", "controversy", "lawsuit", "fine", "penalty", "probe",
    "investigation", "violation", "dispute", "protest", "accident",
    "sengketa", "konflik", "tuntut", "protes", "deforestasi",
    "amenda", "accident", "greva",
]


def has_risk_signal(title):
    t = (title or "").lower()
    return any(w.lower() in t for w in RISK_WORDS)


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
def naver_search(query, key_id, key, max_items=300, tag=""):
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
            title = clean(it.get("title", ""))
            desc = clean(it.get("description", ""))
            # 네이버는 검색어를 형태소로 쪼개 느슨하게 매칭한다.
            # "삼성바이오에피스"를 던지면 "삼성"만 걸린 기사까지 돌려준다.
            # 그래서 검색어의 모든 낱말이 제목+요약에 실제로 있는지 직접 확인한다.
            confirmed = verify(query, title + " " + desc)
            out.append({
                "title": title,
                "media": domain_of(link),
                "published": parse_rfc822(it.get("pubDate", "")),
                "url": link,
                "tags": {tag if confirmed else tag + "?"} if tag else set(),
                "confirmed": confirmed,
                "risk": has_risk_signal(title),
            })
        if len(items) < 100:
            break
        time.sleep(0.12)
    return out


def google_rss(query, locale="kr", tag=""):
    out = []
    hl, gl, lang = LOCALES.get(locale, LOCALES["kr"])
    url = GOOGLE_RSS.format(q=urllib.parse.quote(query), hl=hl, gl=gl, lang=lang)
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"    [rss ERR] {e}")
        return out
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)\
                    .astimezone(KST).isoformat()
        title = clean(e.get("title", ""))
        out.append({
            "title": title,
            "media": (e.get("source", {}) or {}).get("title", "")
                     or domain_of(e.get("link", "")),
            "published": pub,
            "url": e.get("link", ""),
            "tags": {tag} if tag else set(),
            "confirmed": True,      # 구글 뉴스는 구 단위 매칭이 정확하다
            "risk": has_risk_signal(title),
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
            for k in kept:
                if (k.get("url") or "").split("?")[0] == u:
                    k.setdefault("tags", set()).update(a.get("tags") or set())
                    break
            continue
        n = norm(a["title"])
        if not n:
            continue
        dup = None
        for k in kept:
            if abs(len(n) - len(k["_n"])) <= 25 and \
               SequenceMatcher(None, n, k["_n"]).ratio() >= threshold:
                dup = k
                break
        if dup is not None:
            dup.setdefault("tags", set()).update(a.get("tags") or set())
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
        return False   # 게재일시 불명은 제외 (기간 밖 기사 혼입 방지)
    try:
        return start <= dt.datetime.fromisoformat(p) <= end + dt.timedelta(hours=1)
    except Exception:
        return True


def resolve_google_links(items, workers=16, cap=2500):
    """(현재 미사용) 구글 중계 주소를 원출처로 변환. 속도 문제로 기본 비활성."""
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


def short_url(a):
    """구글 뉴스 중계 주소는 400자가 넘고 리포트에 쓸 수도 없으므로
    파일에는 표시만 남긴다. 실제 주소가 필요하면 읽는 쪽에서 제목으로 찾는다."""
    u = a.get("url") or ""
    if "news.google.com" in u:
        return "google:" + (a.get("media") or "unknown")
    return u


def to_lines(items):
    lines = []
    for a in items:
        pub = (a.get("published") or "")[:16].replace("T", " ") or "날짜미상"
        title = a["title"].replace("|", "/")
        tg = ",".join(sorted(a.get("tags") or set()))
        lines.append(f"{pub} | {a.get('media','')} | {title} | {short_url(a)} | {tg}")
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
        got = google_rss(f"site:{site} when:{days}d", tag="sw")
        print(f"  sweep {site}: +{len(got)}")
        sweep += got

    outdir = os.path.join(ROOT, "pool")
    os.makedirs(outdir, exist_ok=True)
    summary = []

    for s, opt in SETS.items():
        spec = cfg.get(s) or {}
        items = []
        pfx = s[-1]                      # set_a -> "a", set_b -> "b", set_c -> "c"
        legend = []                      # 태그 개수 집계용 (내용은 출력하지 않음)

        if key_id and key:
            for i, q in enumerate(spec.get("naver", []), 1):
                t = f"{pfx}{i}"
                legend.append((t, q))
                items += naver_search(q, key_id, key, tag=t)
        for i, q in enumerate(spec.get("rss", []), 1):
            t = f"{pfx}r{i}"
            legend.append((t, q))
            items += google_rss(f"{q} when:{days}d", tag=t)

        # 해외 보도: {"locale": "en", "queries": [...]} 형태의 목록
        for blk in spec.get("foreign", []):
            loc = blk.get("locale", "en")
            n0 = len(items)
            for i, q in enumerate(blk.get("queries", []), 1):
                t = f"{pfx}{loc}{i}"
                legend.append((t, q))
                items += google_rss(f"{q} when:{days}d", locale=loc, tag=t)
            print(f"    해외[{loc}]: +{len(items)-n0}")

        raw_n = len(items)

        use_sweep = sweep if opt["sweep"] else []
        items += use_sweep
        items = [a for a in items if in_window(a, start, end)]

        # 검색어가 제목·요약에 확인되지 않았고 제목에 리스크 신호도 없는 기사는 버린다.
        # (코스피 시황, 부고, 취업박람회 등 검색어가 본문에 스쳐 지나간 기사)
        before = len(items)
        items = [a for a in items
                 if a.get("confirmed") or a.get("risk")
                 or "sw" in (a.get("tags") or set())]
        print(f"      노이즈 제거: {before} -> {len(items)}건")

        items = dedupe(items)
        items.sort(key=lambda a: (a.get("published") or ""), reverse=True)

        print(f"      기간내 고유 기사 {len(items)}건")
        items, cut = balance_by_date(items, opt["limit"])

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
                "# format: date | media | title | url | tags",
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

        # 태그 대응표는 로그에 출력하지 않는다.
        # 공개 저장소의 Actions 로그는 누구나 열람 가능하므로 검색어(회사명)가 노출된다.
        # 태그별 건수 분포를 출력해 검색 정확도를 진단한다.
        # 서로 다른 검색어의 건수가 거의 같으면 느슨한 매칭이 일어나는 신호다.
        from collections import Counter
        tc = Counter()
        for a in items:
            for t in (a.get("tags") or set()):
                tc[t] += 1
        top = ", ".join(f"{t}:{c}" for t, c in tc.most_common(12))
        print(f"    태그 {len(legend)}종 부여 | 상위 분포 {top}")
        print(f"  {base}: 고유수집 {raw_n} + 매체훑기 {len(use_sweep)} "
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
