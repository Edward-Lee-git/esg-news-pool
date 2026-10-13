# -*- coding: utf-8 -*-
"""
collect.py 보조 모듈 (scripts/pool_extras.py) - collect.py 와 같은 폴더에 있어야 한다.

  - 검색어 검증·리스크 신호어
  - 중복 판정용 주소 정규화(url_key)
  - 전문지 사이트 자체 RSS 수집
  - 직전 정기 수집본과 겹치는 기사 제외(state.json)
"""

import os
import re
import json
import html
import zlib
import urllib.parse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
import feedparser

KST = ZoneInfo("Asia/Seoul")


def norm(t):
    return re.sub(r"[^0-9a-z가-힣]", "", (t or "").lower())


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


# 리스크 신호가 있어도 부정이슈가 아닌 정형 기사 유형.
# 증시 시황("코스피 붕괴"), 부고, 인사 등은 신호어가 들어가도 제외한다.
BOILERPLATE = [
    "코스피", "코스닥", "증시", "시황", "마감", "장중", "개장", "환율",
    "부고", "부음", "별세", "모친상", "부친상", "빙부상", "빙모상", "궂긴",
    "인사]", "[인사", "승진", "취업박람회", "job festival", "채용박람회",
    "주간증시", "특징주", "상한가", "하한가", "공모주",
]


def has_risk_signal(title):
    t = (title or "").lower()
    if any(b.lower() in t for b in BOILERPLATE):
        return False
    return any(w.lower() in t for w in RISK_WORDS)


# 추적용 파라미터는 같은 기사 판정에서 무시한다. 그 외 쿼리스트링(기사번호 등)은 보존한다.
# 예: articleView.html?idxno=1234 의 idxno 가 잘리면 그 사이트의 모든 기사가 같은 주소가 된다.
_TRACK_PARAMS = {"oc", "ved", "ei", "fbclid", "gclid", "ref", "from", "mkt_tok"}


def url_key(u):
    try:
        sp = urllib.parse.urlsplit(u or "")
        q = sorted((k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in _TRACK_PARAMS)
        host = sp.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host + sp.path + ("?" + urllib.parse.urlencode(q) if q else "")
    except Exception:
        return u or ""


# ---------------------------------------------------------------
# 전문지 사이트 자체 RSS
# ---------------------------------------------------------------
# 구글 뉴스 RSS로 받은 기사는 원문 주소가 없어(google:매체명) 리포트에서
# "원문 링크 미확인"이 된다. 전문지는 사이트 자체 RSS를 쓰면 원문 주소가
# 그대로 들어오고 구글의 100건 상한도 없다. 열리지 않는 곳은 기존 구글 훑기를 쓴다.
DIRECT_FEED_DOMAINS = ["impacton.net", "esgeconomy.com", "dnews.co.kr", "labortoday.co.kr"]
DIRECT_FEED_PATHS = ["/rss/allArticle.xml", "/rss/rss.xml", "/rss.xml", "/feed/", "/rss/"]
DIRECT_UA = {"User-Agent": "Mozilla/5.0 (compatible; news-pool/1.0)"}
# 날짜 문자열 끝에 시간대 표기(+0900, GMT, Z, KST 등)가 있는지 본다.
_TZ_RE = re.compile(r"(?:[+-]\d{2}:?\d{2}|Z|GMT|UTC|UT|[ECMP][SD]T|KST)\s*$")


def _direct_one(domain):
    """한 매체의 자체 RSS를 찾는다. 성공하면 (기사목록, 사용한 주소), 실패하면 ([], 사유).
    https 인증서 오류(SSLError)가 난 사이트만 마지막에 http 주소를 한 번 더 시도한다.
    공개 헤드라인을 읽을 뿐 로그인·키를 보내지 않으며, 사용하면 로그에 표시된다."""
    last = "후보 주소 모두 실패"
    bases = [f"https://www.{domain}", f"https://{domain}"]
    ssl_failed, extended, i = False, False, 0
    while i < len(bases):
        base = bases[i]
        i += 1
        for path in DIRECT_FEED_PATHS:
            url = base + path
            try:
                r = requests.get(url, headers=DIRECT_UA, timeout=6)
            except requests.exceptions.SSLError:
                last, ssl_failed = "접속 오류(SSLError)", True
                break                      # 같은 주소의 다른 경로도 같은 오류이므로 건너뛴다
            except Exception as e:
                last = f"접속 오류({type(e).__name__})"
                continue
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                continue
            feed = feedparser.parse(r.content)
            ents = [e for e in feed.entries if (e.get("link") or "").startswith("http")]
            if len(ents) < 3:
                last = "RSS 형식이 아니거나 기사 3건 미만"
                continue
            items = []
            for e in ents:
                pub, naive = None, False
                pp = e.get("published_parsed") or e.get("updated_parsed")
                if pp:
                    raw = (e.get("published") or e.get("updated") or "").strip()
                    if _TZ_RE.search(raw):
                        t = dt.datetime(*pp[:6], tzinfo=dt.timezone.utc).astimezone(KST)
                    else:
                        # 시간대 표기가 없는 날짜는 사이트가 쓴 한국 시각 그대로로 본다.
                        t = dt.datetime(*pp[:6], tzinfo=KST)
                        naive = True
                    pub = t.isoformat()
                items.append({
                    "title": html.unescape(re.sub(r"<[^>]+>", "", e.get("title", "") or "")).strip(),
                    "media": domain,
                    "published": pub,
                    "url": e["link"],
                    "tags": {"sw"},
                    "confirmed": True,
                    "risk": False,
                    "naive_tz": naive,
                })
            return items, url
        if i == len(bases) and ssl_failed and not extended:
            extended = True
            bases += [f"http://www.{domain}", f"http://{domain}"]
    return [], last


def fetch_direct_feeds(domains, end):
    """전문지 자체 RSS를 병렬로 가져온다. 결과는 로그에 매체별로 남긴다."""
    if not domains:
        return []
    with ThreadPoolExecutor(max_workers=len(domains)) as ex:
        results = list(ex.map(_direct_one, domains))
    out = []
    for d, (items, info) in zip(domains, results):
        if not items:
            print(f"  direct {d}: 사용 불가 ({info}) -> 구글 훑기로 대체")
            continue
        dated, naive, oldest = 0, 0, None
        for a in items:
            if a.pop("naive_tz", False):
                naive += 1
            if not a["published"]:
                continue
            dated += 1
            t = dt.datetime.fromisoformat(a["published"])
            if oldest is None or t < oldest:
                oldest = t
        old_txt = f"{oldest:%m-%d %H:%M}" if oldest else "날짜없음"
        note = " [http 사용: https 인증서 오류]" if info.startswith("http://") else ""
        print(f"  direct {d}: +{len(items)}건 (날짜 {dated}건, 가장 오래된 {old_txt}, "
              f"시간대표기없음 {naive}건은 한국시각으로 해석) <- {info}{note}")
        out += items
    return out


# ---------------------------------------------------------------
# 직전 수집본과 겹치는 기사 제외
# ---------------------------------------------------------------
# 수집 기간이 "직전 영업일 00:00 ~ 지금"이라 날마다 앞 시간대가 겹친다. 정상일에는 4~9%이지만
# 휴일에 수집이 돈 뒤의 월요일이나 늦게 돈 실행 다음 날에는 30%를 넘는다.
# 기간을 줄이면 직전 수집 이후에 뒤늦게 색인된 기사를 놓치므로 기간은 그대로 두고,
# "직전 날짜의 정기 수집본에 이미 실린 기사"만 뺀다. 세트별로 따로 관리한다.
# 수동 실행은 기준을 바꾸지 않는다. 시험 삼아 돌린 결과가 다음 날 제외 기준을 오염시키면
# 루틴이 읽지 않은 기사가 빠져 누락이 생기기 때문이다. (중복은 허용해도 누락은 안 된다)
STATE_FILE = "state.json"
STATE_SETS = ("a", "b", "c")


def _h(text):
    # 비교용 짧은 키. 암호학적 강도는 필요 없고, 두 체크섬을 이어 붙여 충돌 확률을 무시할 수 있게 한다.
    b = (text or "").encode("utf-8")
    return f"{zlib.crc32(b):08x}{zlib.adler32(b):08x}"


def article_keys(a):
    ks = {_h(norm(a.get("title", "")))}
    u = a.get("url") or ""
    if u.startswith("http") and "news.google.com" not in u:
        ks.add(_h(url_key(u)))
    return ks


def _state_path(root):
    return os.path.join(root, "pool", STATE_FILE)


def load_state(root):
    try:
        with open(_state_path(root), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and "date" in d else None
    except Exception:
        return None


def pool_collected_date(root):
    try:
        with open(os.path.join(root, "pool", "index.txt"), encoding="utf-8") as f:
            m = re.match(r"collected:\s*(\d{4}-\d{2}-\d{2})", f.readline())
            return m.group(1) if m else None
    except Exception:
        return None


def keys_from_pool_files(root):
    """state.json 이 아직 없을 때의 초기값. 저장소에 남아 있는 직전 수집본에서 키를 만든다."""
    out = {k: set() for k in STATE_SETS}
    for k in STATE_SETS:
        for i in range(1, 12):
            path = os.path.join(root, "pool", f"set-{k}-{i}.txt")
            if not os.path.exists(path):
                break
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.rstrip("\n").split(" | ")
                    if len(parts) >= 4:
                        out[k] |= article_keys({"title": parts[2], "url": parts[3]})
    return out


def exclusion_sets(root, state, today):
    """오늘 실행이 제외 기준으로 삼을 키 집합과 출처 설명을 돌려준다."""
    if state:
        if state["date"] >= today:      # 오늘 이미 정기 수집이 있었다 -> 그 이전 날짜 기준을 계속 쓴다
            src, keys = "직전 날짜 정기 수집본", state.get("prev_keys") or {}
        else:
            src, keys = f"{state['date']} 정기 수집본", state.get("keys") or {}
        return {k: set(keys.get(k, [])) for k in STATE_SETS}, src
    d = pool_collected_date(root)           # 첫 실행: 저장소에 남은 수집본이 오늘 것이 아니면 그것을 기준으로
    if d and d < today:
        return keys_from_pool_files(root), f"{d} 수집본(초기값)"
    return {k: set() for k in STATE_SETS}, "없음(첫 실행)"


def advance_state():
    """정기(schedule) 실행이거나 ADVANCE_STATE=true 일 때만 다음 날의 제외 기준을 갱신한다."""
    return (os.environ.get("GITHUB_EVENT_NAME") == "schedule"
            or os.environ.get("ADVANCE_STATE", "").lower() == "true")


def save_state(root, today, kept, base):
    d = {"date": today,
         "keys": {k: sorted(kept.get(k, set())) for k in STATE_SETS},
         "prev_keys": {k: sorted(base.get(k, set())) for k in STATE_SETS}}
    with open(_state_path(root), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
