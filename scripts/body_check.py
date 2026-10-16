# -*- coding: utf-8 -*-
"""
본문 확인 (set_a 전용)
=====================
검색어가 제목에 없는데 리스크 단어 때문에 남은 기사(태그 끝에 '?')의 본문을
수집 단계에서 직접 열어 검색어가 실제로 있는지 확인한다.
루틴 환경은 뉴스 사이트가 막혀 있지만 GitHub Actions 는 막혀 있지 않다.

판정 (태그 하나마다):
  confirm   본문 문단에 검색어가 있음          -> '?' 제거 (확인된 기사)
  absent    페이지 어디에도 검색어가 없음       -> 그 태그 제거 (네이버의 느슨한 매칭 오탐)
                                                태그가 하나도 안 남으면 기사도 제외
  ambiguous 메뉴·관련기사 링크에만 검색어가 있음 -> '?' 유지 (사람이 판단)
  unknown   열람 실패·본문이 짧음·유료벽 등     -> '?' 유지 (사람이 판단)
실제 기사를 지우는 쪽이 놓치는 쪽보다 나쁘므로, 확실할 때만 제거한다.

환경변수 BODY_CHECK:  on(기본) | log(판정만 출력, 결과는 바꾸지 않음) | off
공개 저장소 로그에는 제목·검색어(회사명)를 출력하지 않고, 도메인과 판정 건수만 출력한다.
"""
import os
import re
import json
import time
import socket
import ipaddress
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests

from pool_extras import verify

MODE = os.environ.get("BODY_CHECK", "on").strip().lower()
MAX_ARTICLES = 60          # 한 세트에서 확인할 최대 기사 수
WORKERS = 4
TIMEOUT = 12               # 초
TOTAL_BUDGET = 150         # 전체 제한 시간(초). 넘으면 남은 기사는 '확인 못함'
MAX_BYTES = 2_000_000
MIN_BODY = 800             # 본문 글자 수가 이보다 짧으면 '없다'고 단정하지 않는다
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; esg-news-pool/1.0)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ko,en;q=0.8",
}


# ---------------------------------------------------------------
class _Text(HTMLParser):
    """페이지 전체 글자(full)와, 메뉴·링크·관련기사를 뺀 본문 문단(blocks)을 나눠 모은다."""
    SKIP_ALL = {"script", "style", "noscript", "template", "svg", "iframe"}
    SKIP_BODY = {"nav", "aside", "footer", "header", "form", "a", "button", "select"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6",
             "article", "section", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_all = 0
        self.skip_body = 0
        self.full, self.blocks, self.cur = [], [], []

    def _flush(self):
        if self.cur:
            t = " ".join(self.cur).strip()
            if len(t) >= 30:                 # 메뉴·버튼 같은 짧은 조각은 본문으로 보지 않는다
                self.blocks.append(t)
            self.cur = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_ALL:
            self.skip_all += 1
        elif tag in self.SKIP_BODY:
            self.skip_body += 1
        if tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in self.SKIP_ALL:
            self.skip_all = max(0, self.skip_all - 1)
        elif tag in self.SKIP_BODY:
            self.skip_body = max(0, self.skip_body - 1)
        if tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.skip_all:
            return
        d = data.strip()
        if not d:
            return
        self.full.append(d)
        if not self.skip_body:
            self.cur.append(d)


def extract(page_html):
    p = _Text()
    try:
        p.feed(page_html)
        p.close()
    except Exception:
        pass
    p._flush()
    body = "\n".join(p.blocks)
    return " ".join(p.full), body


# ---------------------------------------------------------------
def _public_host(host):
    """사설·루프백·링크로컬 주소로 가는 요청은 막는다(리다이렉트로 내부망을 두드리는 것 방지)."""
    if os.environ.get("BODY_CHECK_ALLOW_PRIVATE") == "1":      # 시험용
        return True
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


def _decode(buf, ctype):
    m = re.search(r"charset=([\w\-]+)", ctype or "", re.I) or \
        re.search(rb"charset=[\"']?([\w\-]+)", buf[:3000], re.I)
    enc = None
    if m:
        enc = m.group(1)
        enc = enc.decode("ascii", "ignore") if isinstance(enc, bytes) else enc
    for e in ([enc] if enc else []) + ["utf-8", "cp949"]:
        try:
            return buf.decode(e)
        except Exception:
            continue
    return buf.decode("utf-8", "ignore")


def fetch(url):
    """(html 또는 None, 사유). 리다이렉트는 직접 따라가며 매번 주소를 검사한다."""
    cur = url
    for _ in range(5):
        sp = urllib.parse.urlsplit(cur)
        if sp.scheme not in ("http", "https") or not sp.hostname or not _public_host(sp.hostname):
            return None, "blocked"
        try:
            r = requests.get(cur, headers=HEADERS, timeout=(5, TIMEOUT), stream=True, allow_redirects=False)
        except Exception:
            return None, "error"
        if 300 <= r.status_code < 400 and r.headers.get("Location"):
            cur = urllib.parse.urljoin(cur, r.headers["Location"])
            r.close()
            continue
        if r.status_code != 200:
            r.close()
            return None, f"http{r.status_code}"
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and "text" not in ctype.lower():
            r.close()
            return None, "nonhtml"
        buf = b""
        try:
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > MAX_BYTES:
                    break
        except Exception:
            r.close()
            return None, "error"
        r.close()
        return _decode(buf, ctype), "ok"
    return None, "redirects"


_ARTICLE_MARK = re.compile(
    r"<article|og:type[\"']?\s+content=[\"']article|article:published_time|"
    r"articleBody|(?:id|class)=[\"'][^\"']*article", re.I)


def looks_like_article(page_html):
    """동의 안내·오류·검색 결과 같은 '기사가 아닌 긴 페이지'를 걸러내기 위한 확인."""
    return bool(_ARTICLE_MARK.search(page_html or ""))


def judge(query, full, body, is_article=True):
    if verify(query, body):
        return "confirm"
    if len(body) < MIN_BODY:
        return "unknown"           # 짧은 글·유료벽·스크립트 페이지: 없다고 단정하지 않는다
    if verify(query, full):
        return "ambiguous"         # 본문 밖(메뉴·관련기사)에만 있음
    if not is_article:
        return "unknown"           # 기사 페이지로 보이지 않으면 '없음'으로 단정하지 않는다
    return "absent"


def _check_one(a, qmap, deadline):
    weak = [t for t in (a.get("tags") or ()) if t.endswith("?")]
    url = a.get("url") or ""
    host = urllib.parse.urlsplit(url).netloc.replace("www.", "")
    if time.time() > deadline:
        return {t: "unknown" for t in weak}, "timeout", host, 0
    if not url.startswith("http") or "news.google.com" in url:
        return {t: "unknown" for t in weak}, "nolink", host, 0
    page, why = fetch(url)
    if page is None:
        return {t: "unknown" for t in weak}, why, host, 0
    full, body = extract(page)
    art = looks_like_article(page)
    res = {}
    for t in weak:
        q = qmap.get(t[:-1])
        res[t] = judge(q, full, body, art) if q else "unknown"
    return res, "ok", host, len(body)



def load_extra_queries():
    """보조 Secret EXTRA_QUERIES_JSON (["검색어", ...]) -> set_a 네이버 검색어에 덧붙인다. 없으면 빈 목록."""
    raw = os.environ.get("EXTRA_QUERIES_JSON", "").strip()
    try:
        return [q.strip() for q in json.loads(raw) if isinstance(q, str) and q.strip()] if raw else []
    except Exception as e:
        print(f"[WARN] EXTRA_QUERIES_JSON 형식 오류 -> 무시: {type(e).__name__}")
        return []


def apply_body_check(items, qmap):
    """items 의 '?' 태그를 본문 확인 결과로 정리해 돌려준다. qmap: {'a7': '검색어', ...}"""
    if MODE == "off":
        return items
    targets = [a for a in items if any(t.endswith("?") for t in (a.get("tags") or ()))]
    if not targets:
        print("    본문 확인: 대상 없음")
        return items
    skipped = max(0, len(targets) - MAX_ARTICLES)
    deadline = time.time() + TOTAL_BUDGET
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        out = list(ex.map(lambda a: _check_one(a, qmap, deadline), targets[:MAX_ARTICLES]))
    results = {id(a): r for a, r in zip(targets, out)}

    # 판정 집계 (태그 단위)
    cnt = Counter(st for res, _, _, _ in out for st in res.values())
    fails = Counter(f"{host or '-'}:{why}" for res, why, host, _ in out if why != "ok")
    checked = sum(cnt.values())
    drop_ok = True
    # 안전장치: '없음' 판정이 비정상적으로 많으면(사이트 구조 변경·추출 실패 신호) 제거를 중단한다.
    if cnt["absent"] >= 4 and cnt["absent"] >= 0.5 * max(checked, 1):
        drop_ok = False
        print(f"    [경고] '없음' 판정이 {cnt['absent']}/{checked}건으로 비정상적으로 많아 이번 회차는 제거하지 않습니다")

    # 판정 근거(운영 초기에 오판 여부를 점검하기 위한 기록): 도메인·태그 번호·판정·본문 글자 수
    for res, why, host, blen in out[:40]:
        print("      " + f"{host or '-'} " + " ".join(f"{t}->{st}" for t, st in res.items()) + f" (본문 {blen}자)")
    if MODE == "log":
        print(f"    본문 확인(판정만, 결과 미적용): 확인 {cnt['confirm']} / 없음 {cnt['absent']} / "
              f"애매 {cnt['ambiguous']} / 불가 {cnt['unknown']}")
        if fails:
            print("      열람 실패: " + ", ".join(f"{k} x{v}" for k, v in fails.most_common(6)))
        return items

    kept, removed_art, removed_tag, confirmed = [], 0, 0, 0
    for a in items:
        r = results.get(id(a))
        if r is None:
            kept.append(a)
            continue
        res = r[0]
        new = set()
        for t in (a.get("tags") or ()):
            st = res.get(t)
            if t.endswith("?") and st == "confirm":
                new.add(t[:-1])
                confirmed += 1
            elif t.endswith("?") and st == "absent" and drop_ok:
                removed_tag += 1
            else:
                new.add(t)
        if not new:
            removed_art += 1
            continue
        a["tags"] = new
        kept.append(a)
    print(f"    본문 확인: 대상 {len(targets)}건(확인 시도 {len(targets) - skipped}건) -> "
          f"확인됨 {confirmed}태그 / 오탐 제거 {removed_tag}태그(기사 {removed_art}건 제외) / "
          f"애매 {cnt['ambiguous']} / 열람 불가 {cnt['unknown']}")
    if fails:
        print("      열람 실패: " + ", ".join(f"{k} x{v}" for k, v in fails.most_common(6)))
    if skipped:
        print(f"      한도 초과로 확인하지 못한 기사 {skipped}건은 '?'로 유지")
    return kept
