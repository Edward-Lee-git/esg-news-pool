# -*- coding: utf-8 -*-
# (분할본) 같은 폴더에 pool_extras.py 가 있어야 실행된다.
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

본문 확인: set_a 에서 태그 끝에 '?'가 붙은 기사(검색어가 제목에 없는 기사)는
  body_check.py 가 본문을 직접 열어 확인한다. 같은 폴더에 body_check.py 가 있어야 한다.

환경변수:
  NAVER_API_KEY_ID, NAVER_API_KEY  (없으면 RSS만 사용)
  EXTRA_QUERIES_JSON               (선택. 검색어를 더하는 보조 Secret: ["검색어"] 는 부정기사, {"set_a":[..],"set_c":[..]} 는 세트별)
  BODY_CHECK                       (on 기본 | log 판정만 출력 | off)
  QUERIES_JSON                     (없으면 config/queries.sample.json 사용)
"""

import os
import re
import json
import hashlib
import time
import html
import urllib.parse
import datetime as dt
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import requests
import feedparser
import holidays

# 보조 모듈 (같은 폴더의 pool_extras.py)
from pool_extras import (
    norm, url_key, verify, has_risk_signal,
    fetch_direct_feeds, DIRECT_FEED_DOMAINS,
    article_keys, STATE_SETS, load_state, exclusion_sets, advance_state, save_state,
)
from pool_stats import (
    in_window, context_snippet, QSTATS, STATS_FILE, STATS_KEEP_DAYS, STATS_HEAD,
    qstat, save_query_stats, keep_priority, balance_by_date, apply_query_cap,
)
from body_check import apply_body_check, load_extra_queries   # set_a 보조 기능(본문 확인, 추가 검색어)

KST = ZoneInfo("Asia/Seoul")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NAVER_ENDPOINT = "https://naverapihub.apigw.ntruss.com/search/v1/news"
GOOGLE_RSS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"
LINKS = {}   # 구글 중계 주소 번호 -> 주소. 기사 목록에는 번호만 남기고 pool/links.txt 에 대응표를 저장한다.

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
    "set_a": {"sweep": False, "limit": 900, "body_check": True, "deep": True},
    # strict=False: 제목+요약 확정. deep=True: 검색어마다 300건 한도에 걸리면 수집 시작 시각까지 최대 1,000건 더 가져온다.
    # set_c 상한 1,500줄 = 파일 6개(루틴 프롬프트는 최대 8개까지 읽도록 되어 있어야 한다).
    # query_cap: 한 검색어가 목록을 독차지하지 못하게 제목에 낱말이 있는 기사를 검색어마다 최신순 100줄까지만 우선 보존한다.
    "set_b": {"sweep": True,  "limit": 1200, "strict": False, "deep": True, "query_cap": 100},
    "set_c": {"sweep": True,  "limit": 1500, "strict": False, "deep": True, "query_cap": 100},
}


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
def naver_search(query, key_id, key, max_items=300, tag="", since=None, strict=True):
    out = []
    # '^검색어' = 제목 필수 모드: 검색어의 모든 낱말이 제목+요약에 있고, 그중 첫 낱말은 반드시 제목에 있어야 확정한다.
    # 제목에 낱말이 없고 요약에만 스친 기사는 위험어가 있어도 살리지 않고 버린다(노이즈 감축용).
    title_req = query.strip().startswith("^")
    query = query.strip().lstrip("^").strip()
    first_word = (query.split() or [""])[0]
    q_words = [w.strip('"') for w in query.split() if w.strip('"') and w.lower() not in ("or", "and")]
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
            # 본문(요약) 언급만으로는 스쳐 지나간 언급과 실제 이슈를 구분할 수 없다.
            # 취업박람회 기사의 참가기업 목록, 재건축 기사의 시공사 목록 등이
            # 모두 본문 언급으로 잡히기 때문이다. 그래서 제목을 기준으로 삼는다.
            # strict=True(부정기사): 검색어의 모든 낱말이 '제목'에 있어야 확정.
            # strict=False(규제동향·일반동향): 제목+요약에 있으면 확정. 종합지 기사는 제목에 낱말을
            # 다 쓰지 않는 경우가 많아 제목만 보면 대부분 탈락한다(부처명+입법예고 등).
            in_title = verify(query, title)
            in_body = in_title or verify(query, title + " " + desc)
            if title_req:
                confirmed = in_body and verify(first_word, title)
                body_only = False
                risk = has_risk_signal(title) if confirmed else False
            else:
                confirmed = in_title if strict else in_body
                body_only = (not in_title) and in_body
                risk = has_risk_signal(title)
            th = bool(confirmed and any(verify(w, title) for w in q_words))     # 제목에 검색어 낱말이 있는가
            ctx = context_snippet(desc, query) if (confirmed and not in_title) else ""
            out.append({
                "title": title,
                "media": domain_of(link),
                "published": parse_rfc822(it.get("pubDate", "")),
                "url": link,
                "tags": {tag if confirmed else tag + "?"} if tag else set(),
                "confirmed": confirmed,
                "body_only": body_only,
                "risk": risk,
                "ctx": ctx,
                "th": th,
            })
        if len(items) < 100:
            break
        ds = [a["published"] for a in out[-len(items):] if a["published"]]
        if since is not None and ds and dt.datetime.fromisoformat(min(ds)) < since:
            break          # 최신순 결과가 이미 수집 시작 시각 이전까지 내려왔으면 더 볼 필요 없다
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
        # 구글 뉴스 제목은 끝에 " - 매체명"이 붙는다. 그 매체명만 정확히 떼야
        # 같은 기사를 사이트 RSS·네이버로 받았을 때 중복으로 합쳐지고, 목록도 짧아진다.
        _src = (e.get("source", {}) or {}).get("title", "")
        if _src and title.endswith(" - " + _src):
            title = title[: -(len(_src) + 3)].rstrip()
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


def dedupe(items, threshold=0.88):
    seen, kept = set(), []
    for a in items:
        u = url_key(a.get("url"))
        if u and u in seen:
            for k in kept:
                if url_key(k.get("url")) == u:
                    k.setdefault("tags", set()).update(a.get("tags") or set())
                    if a.get("th"):
                        k["th"] = True
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
            if a.get("th"):
                dup["th"] = True
            continue
        if u:
            seen.add(u)
        a["_n"] = n
        kept.append(a)
    for k in kept:
        k.pop("_n", None)
    return kept


def short_url(a):
    """구글 뉴스 중계 주소는 길어서(130~340자) 기사 목록에는 `google:매체#번호`만 남기고,
    번호와 실제 주소의 대응표는 pool/links.txt 에 저장한다. 중계 주소는 클릭하면 원문으로 이동한다.
    번호는 주소의 해시(8자리)라 같은 기사는 항상 같은 번호다."""
    u = a.get("url") or ""
    if "news.google.com" in u:
        lid = hashlib.md5(u.encode("utf-8")).hexdigest()[:8]
        LINKS[lid] = u
        media = (a.get("media") or "unknown").replace("|", "/").replace("#", " ")
        return f"google:{media}#{lid}"
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

    QSTATS.clear()
    cfg = load_queries()
    extra = load_extra_queries()
    if extra:
        print("추가 검색어(EXTRA_QUERIES_JSON): " + ", ".join(f"{k} {len(v)}개" for k, v in extra.items()))

    today = end.strftime("%Y-%m-%d")
    state = load_state(ROOT)
    exclude, ex_src = exclusion_sets(ROOT, state, today)
    print(f"제외 기준: {ex_src} (부정기사 {len(exclude['a'])} / 규제동향 {len(exclude['b'])} / "
          f"일반동향 {len(exclude['c'])}개 키)\n")
    kept_keys = {k: set() for k in STATE_SETS}

    # 공통 매체 훑기
    sweep = []
    for site in cfg.get("sweep", []):
        got = google_rss(f"site:{site} when:{days}d", tag="sw")
        print(f"  sweep {site}: +{len(got)}")
        sweep += got

    # 전문지는 사이트 자체 RSS를 먼저 놓는다. 같은 기사가 구글 훑기로도 들어오면
    # 중복 제거에서 원문 주소가 있는 이쪽이 남는다.
    direct = fetch_direct_feeds(DIRECT_FEED_DOMAINS, end)
    sweep = direct + sweep

    outdir = os.path.join(ROOT, "pool")
    os.makedirs(outdir, exist_ok=True)
    summary = []

    for s, opt in SETS.items():
        spec = cfg.get(s) or {}
        items = []
        pfx = s[-1]                      # set_a -> "a", set_b -> "b", set_c -> "c"
        legend = []                      # 태그 개수 집계용 (내용은 출력하지 않음)
        strict = opt.get("strict", True)
        print(f"    [{s}] 검색어별 성과 (원시=받은 건수, 기간내, 확정, 위험어만=제목에 위험어가 있어 남은 것, 탈락, 가장 오래된 기사, ★=검색어당 한도(기본 300건, 기간 시작까지 내려가는 검색은 1,000건)에 걸려 기간 시작까지 못 내려감)")

        if key_id and key:
            kw = {"max_items": 1000, "since": start} if opt.get("deep") else {}   # set_a: 수집 시작 시각까지
            for i, q in enumerate(spec.get("naver", []), 1):
                t = f"{pfx}{i}"
                legend.append((t, q))
                got = naver_search(q, key_id, key, tag=t, strict=strict, **kw)
                qstat(t, q, got, start, end, show_text=(s != "set_a"), cap=kw.get("max_items", 300), kind="naver", sname=s)
                items += got
            for i, q in enumerate(extra.get(s, []), 1):
                t = f"{pfx}x{i}"              # ax1, bx1, cx1 ... (기존 번호와 섞이지 않음)
                legend.append((t, q))
                got = naver_search(q, key_id, key, tag=t, strict=strict, **kw)
                qstat(t, q, got, start, end, show_text=(s != "set_a"), cap=kw.get("max_items", 300), kind="naver", sname=s)
                items += got
        for i, q in enumerate(spec.get("rss", []), 1):
            t = f"{pfx}r{i}"
            legend.append((t, q))
            got = google_rss(f"{q} when:{days}d", tag=t)
            qstat(t, q, got, start, end, show_text=(s != "set_a"), cap=100, kind="rss", sname=s)
            items += got

        # 해외 보도: {"locale": "en", "queries": [...]} 형태의 목록
        for blk in spec.get("foreign", []):
            loc = blk.get("locale", "en")
            n0 = len(items)
            for i, q in enumerate(blk.get("queries", []), 1):
                t = f"{pfx}{loc}{i}"
                legend.append((t, q))
                got = google_rss(f"{q} when:{days}d", locale=loc, tag=t)
                qstat(t, q, got, start, end, show_text=(s != "set_a"), cap=100, kind=f"foreign-{loc}", sname=s)
                items += got
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
        # 남은 것 중 제목 언급 없이 리스크 신호로만 살아난 기사에는 물음표를 붙인다.
        print(f"      노이즈 제거: {before} -> {len(items)}건")

        items = dedupe(items)
        items.sort(key=lambda a: (a.get("published") or ""), reverse=True)

        print(f"      기간내 고유 기사 {len(items)}건")

        ex = exclude.get(pfx, set())
        n_excluded = 0
        if ex:
            before_ex = len(items)
            items = [a for a in items if not (article_keys(a) & ex)]
            n_excluded = before_ex - len(items)
            print(f"      직전 수집본과 겹쳐 제외: {n_excluded}건 ({n_excluded * 100 // max(before_ex, 1)}%)")

        if opt["sweep"]:
            n_over = apply_query_cap(items, opt.get("query_cap", 100))
            print(f"      검색어당 상한({opt.get('query_cap', 100)}줄) 초과로 순위를 낮춘 줄: {n_over}건")
        items, cut = balance_by_date(items, opt["limit"], prio=keep_priority if opt["sweep"] else None)
        if opt.get("body_check"):
            items = apply_body_check(items, dict(legend))   # legend: [(태그, 검색어)]
        for a in items:
            kept_keys[pfx] |= article_keys(a)

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
                "# note: 직전 날짜 정기 수집본에 이미 실린 기사는 이 목록에서 제외되어 있음",
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

    # 구글 중계 주소 대응표: 이번에 기사 목록에 실제로 남은 줄의 번호만 들어간다.
    with open(os.path.join(outdir, "links.txt"), "w", encoding="utf-8") as f:
        f.write(f"# collected: {end:%Y-%m-%d %H:%M} KST\n# format: 번호 | 구글 뉴스 중계 주소(클릭하면 원문으로 이동)\n")
        for k in sorted(LINKS):
            f.write(f"{k} | {LINKS[k]}\n")
    print(f"\n구글 중계 링크 대응표: {len(LINKS)}개 -> pool/links.txt ({os.path.getsize(os.path.join(outdir, 'links.txt')) // 1024}KB)")

    with open(os.path.join(outdir, "index.txt"), "w", encoding="utf-8") as f:
        f.write(f"collected: {end:%Y-%m-%d %H:%M} KST\n")
        f.write(f"window: {start:%Y-%m-%d} 00:00 ~ {end:%Y-%m-%d %H:%M} KST\n")
        f.write("\n".join(summary) + "\n")

    if advance_state():
        save_state(ROOT, today, kept_keys, exclude)
        print("\n제외 기준 저장 (정기 수집: 다음 날 이 수집본과 겹치는 기사를 뺀다)")
        n_stats = save_query_stats(outdir, end, QSTATS)
        print(f"검색어 성과 이력 저장: pool/{STATS_FILE} (누적 {n_stats}행, {STATS_KEEP_DAYS}일 보관)")
    else:
        print("\n제외 기준 저장 안 함 (수동 실행: 다음 정기 수집의 기준을 바꾸지 않는다)")

    print(f"\n완료: {', '.join(summary)}")


if __name__ == "__main__":
    main()
