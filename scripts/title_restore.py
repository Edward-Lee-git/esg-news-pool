#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""잘려 온 기사 제목 복원 v2 (collect.py 가 불러 씁니다).

네이버 검색 결과는 긴 제목을 약 70바이트(한글 약 35자)에서 「...」로 줄여 보냅니다.
이 모듈은 그런 기사(직접 주소 + 제목이 ...·… 로 끝남)의 페이지 앞부분(<head>)을 읽어 제목을 되찾습니다.

v1 에서 바꾼 점(2026-11-20 첫 수동 실행에서 150초 안에 33건밖에 못 처리한 원인 수정):
- 페이지를 통째로(최대 2MB) 받지 않고 </head> 까지만(최대 256KB) 받습니다. 제목은 앞부분에 있습니다.
- 주소 하나에 쓰는 전체 시간을 PER_URL 초로 제한하고(느린 사이트가 오래 붙잡지 못함), 리다이렉트는 3번까지만 따라갑니다.
- 16건씩 묶어 기다리지 않고, 끝나는 대로 다음 주소를 시작합니다(느린 한 곳이 전체를 막지 않음).
- 세트별로 시간을 나눠 부정기사·규제동향이 먼저 처리되고 일반동향도 시간을 받습니다(전체 TOTAL_BUDGET 초).

안전 원칙(그대로): 후보가 잘린 제목의 앞부분으로 시작할 때만(공백·구두점 무시) 바꾸고, 안 맞으면 그대로 둡니다.
구글 중계 기사·잘리지 않은 제목은 건드리지 않습니다. 어떤 오류도 수집을 멈추지 않습니다. 로그에는 개수·소요 시간만 출력합니다.
환경변수 TITLE_RESTORE = on(기본) | off
"""
import html
import os
import re
import time
import threading
import urllib.parse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

try:                                       # 기존 본문 확인 모듈의 접속 안전장치(내부망 주소 차단·요청 헤더)를 재사용
    import requests
    from body_check import HEADERS, _public_host
except Exception:  # noqa
    requests = None
    HEADERS = None

WORKERS = 12
TOTAL_BUDGET = 200        # 초. 세 세트 합계
SHARE = {"a": 45, "b": 75}  # 세트별 상한(초). 나머지(일반동향)는 남은 전체 시간을 씀
PER_URL = 8.0             # 초. 주소 하나에 쓰는 전체 시간(응답 대기 한 번은 최대 READ_T 초 더 걸릴 수 있음)
CONNECT_T, READ_T = 4, 5
MAX_HEAD = 262144         # 바이트. 이만큼 읽어도 </head> 가 없으면 거기까지로 판단
MAX_HOPS = 3
ABORT_FAILS = 30          # 성공이 한 번도 없이 접속 실패가 연속 이만큼이면 중단
DOMAIN_FAILS = 3          # 한 도메인에서 성공 없이 이만큼 접속 실패하면 그 도메인은 건너뜀
MAX_LEN = 200
MIN_STEM = 8              # 비교에 쓰는 앞부분이 이보다 짧으면(정규화 후) 복원하지 않음

_ELL = re.compile(r"\s*(?:\.{2,}|…|⋯)\s*$")
_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"([\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([\w\-]+)", re.I)
_SEPS = (" - ", " | ", " :: ", " : ", " – ", " — ", " ㅣ ", " · ")

_memo = {}
_st = {"t0": None, "ok": 0, "fail": 0, "streak": 0, "skipped": 0, "off": "",
       "dfail": Counter(), "dok": Counter(), "why": Counter()}


def _norm(s):
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower())


def _clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    return re.sub(r"\s+", " ", t.replace("\u200b", "")).strip()


def is_truncated(a):
    u = a.get("url") or ""
    return bool(u.startswith("http") and "news.google.com" not in u and _ELL.search(a.get("title") or ""))


def _candidates(page):
    metas = {}
    for tag in _META.findall(page or ""):
        at = {k.lower(): v[1:-1] for k, v in _ATTR.findall(tag)}
        key = (at.get("property") or at.get("name") or "").lower()
        if key in ("og:title", "twitter:title") and at.get("content"):
            metas.setdefault(key, at["content"])
    out = [_clean(metas[k]) for k in ("og:title", "twitter:title") if k in metas]
    m = _TITLE.search(page or "")
    if m:
        out.append(_clean(m.group(1)))
    return [c for c in out if c]


def pick(stem, page):
    """잘린 제목의 앞부분(stem)과 맞는 가장 좋은 전체 제목을 돌려준다(없으면 None)."""
    sn = _norm(stem)
    if len(sn) < MIN_STEM:
        return None
    for c in _candidates(page):
        cn = _norm(c)
        if not cn.startswith(sn) or len(cn) <= len(sn):
            continue                                        # 앞부분이 다르거나, 더 길지 않으면(잘리지 않은 제목) 채택하지 않음
        for sep in _SEPS:                                   # 끝의 「 - 매체명」 같은 꼬리표는 짧고 앞부분을 해치지 않을 때만 뗀다
            p = c.rfind(sep)
            if p > 0 and len(c) - p <= 26 and _norm(c[:p]).startswith(sn) and len(_norm(c[:p])) > len(sn):
                c = c[:p].rstrip()
                break
        if 0 < len(c) <= MAX_LEN:
            return c
    return None


def _decode(buf, ctype):
    """앞부분만 받은 바이트를 글자로 바꾼다. 끝에서 잘린 글자는 무시하고, 선언된 문자 집합(없으면 utf-8 → cp949)을 쓴다."""
    enc = None
    m = _CHARSET.search((ctype or "").encode("ascii", "ignore")) or _CHARSET.search(buf[:4096])
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower()
    for e in ([enc] if enc else []) + ["utf-8"]:
        try:
            return buf.decode(e, "ignore" if e == "utf-8" else "strict")
        except Exception:
            continue
    try:
        return buf.decode("cp949", "ignore")
    except Exception:
        return buf.decode("utf-8", "ignore")


def _read_some(r, n=16384):
    """도착한 만큼만(최대 n바이트) 읽어 돌아온다. 한 글자씩 느리게 오는 사이트에서도 시간 확인을 계속할 수 있다."""
    raw = r.raw
    if hasattr(raw, "read1"):
        return raw.read1(n, decode_content=True)
    return raw.read(n, decode_content=True)               # 구버전 urllib3: 16KB 가 찰 때까지 기다릴 수 있음(아래 보조 장치가 막아 준다)


def _host(u):
    m = re.match(r"https?://([^/:?#]+)", u or "")
    return (m.group(1).lower() if m else "")


def _fetch_head(url):
    """(페이지 앞부분 글자 또는 None, 사유). 주소 하나에 PER_URL 초 안팎만 쓴다."""
    if requests is None or HEADERS is None:
        return None, "error"
    t_end = time.time() + PER_URL
    cur = url
    for _ in range(MAX_HOPS + 1):
        sp = urllib.parse.urlsplit(cur)
        if sp.scheme not in ("http", "https") or not sp.hostname or not _public_host(sp.hostname):
            return None, "blocked"
        left = t_end - time.time()
        if left <= 0.5:
            return None, "timeout"
        try:
            r = requests.get(cur, headers=HEADERS, timeout=(min(CONNECT_T, left), min(READ_T, left)),
                             stream=True, allow_redirects=False)
        except requests.exceptions.Timeout:
            return None, "timeout"
        except Exception:
            return None, "error"
        try:
            if 300 <= r.status_code < 400 and r.headers.get("Location"):
                cur = urllib.parse.urljoin(cur, r.headers["Location"])
                continue
            if r.status_code != 200:
                return None, f"http{r.status_code}"
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype.lower() and "text" not in ctype.lower():
                return None, "nonhtml"
            buf, tail, got_head = b"", b"", False
            try:
                while True:
                    chunk = _read_some(r)
                    if not chunk:
                        break
                    buf += chunk
                    tail = (tail + chunk.lower())[-(len(chunk) + 8):]
                    if b"</head" in tail:
                        got_head = True
                        break
                    if len(buf) >= MAX_HEAD or time.time() > t_end:
                        break
            except Exception:
                if not buf:
                    return None, "timeout"
            return _decode(buf, ctype), ("ok" if got_head or buf else "error")
        finally:
            r.close()
    return None, "redirects"


def fetch_head(url):
    """_fetch_head 를 별도 스레드에서 돌리고 PER_URL + READ_T + 1 초까지만 기다린다. 어떤 이유로든 막히면 포기하고 넘어간다
    (남은 스레드는 데몬이라 수집 종료를 막지 않는다)."""
    res = {}

    def run():
        try:
            res["v"] = _fetch_head(url)
        except Exception:
            res["v"] = (None, "error")

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(PER_URL + READ_T + 1)
    return res.get("v", (None, "timeout"))


def _one(a, fetch):
    """(복원된 제목 또는 None, 사유). 사유: ok·memo·domain·nomatch(열렸지만 제목이 안 맞음)·그 밖은 접속 실패 사유."""
    u = a["url"]
    if u in _memo:
        return _memo[u], "memo"
    h = _host(u)
    if _st["dfail"][h] >= DOMAIN_FAILS and not _st["dok"][h]:
        return None, "domain"                              # 이 도메인은 성공 없이 실패만 반복 → 건너뜀
    try:
        page, why = fetch(u)
    except Exception:
        page, why = None, "error"
    new = pick(_ELL.sub("", a["title"]), page) if page else None
    if new:
        _st["dok"][h] += 1
        why = "ok"
    elif page:
        why = "nomatch"                                    # 페이지는 열렸지만 맞는 제목이 없음(접속 실패로 세지 않음)
    else:
        _st["dfail"][h] += 1
    _memo[u] = new
    return new, why


def _restore(items, label, fetch):
    if os.environ.get("TITLE_RESTORE", "on").lower() == "off" or _st["off"]:
        return items
    todo = [a for a in items if is_truncated(a)]
    if not todo:
        return items
    now = time.time()
    if _st["t0"] is None:
        _st["t0"] = now
    end = min(_st["t0"] + TOTAL_BUDGET, now + SHARE.get(label, TOTAL_BUDGET))
    n_ok = n_fail = 0
    it = iter(todo)
    inflight = {}
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        while True:
            while len(inflight) < WORKERS * 2 and time.time() < end and not _st["off"]:
                a = next(it, None)
                if a is None:
                    break
                inflight[ex.submit(_one, a, fetch)] = a
            if not inflight:
                break
            done, _ = wait(list(inflight), timeout=max(0.05, end - time.time()), return_when=FIRST_COMPLETED)
            if not done:
                break                                     # 시간 소진
            for f in done:
                a = inflight.pop(f)
                try:
                    new, why = f.result()
                except Exception:
                    new, why = None, "error"
                if new:
                    a["title"] = new
                    a["title_restored"] = True
                    n_ok += 1
                    _st["ok"] += 1
                    _st["streak"] = 0
                else:
                    n_fail += 1
                    _st["fail"] += 1
                    _st["why"][why] += 1
                    if why not in ("nomatch", "memo", "domain"):
                        _st["streak"] += 1                 # 접속 자체가 실패한 경우만 연속 실패로 센다
            if _st["ok"] == 0 and _st["streak"] >= ABORT_FAILS:
                _st["off"] = f"접속 실패가 연속 {_st['streak']}건"
                break
            if time.time() >= end and not inflight:
                break
    finally:
        for f in inflight:
            f.cancel()
        ex.shutdown(wait=True, cancel_futures=True)       # 이미 시작한 요청은 PER_URL 안팎에 끝난다
    n_skip = len(todo) - n_ok - n_fail
    _st["skipped"] += n_skip
    if n_skip:
        _st["why"]["시간 초과"] += 1
    top = ", ".join(f"{k} {c}" for k, c in _st["why"].most_common(3))
    print(f"      [제목복원] {label}: 잘린 제목 {len(todo)}건 → 복원 {n_ok}건 · 실패 {n_fail}건 · 건너뜀 {n_skip}건"
          f" · 소요 {time.time() - now:.0f}초" + (f" ({top})" if n_fail or n_skip else "")
          + (f" — {_st['off']}으로 중단" if _st["off"] else ""))
    return items


def restore_titles(items, label="", fetch=None):
    """items 의 잘린 제목을 복원해 같은 목록을 돌려준다. 어떤 오류도 밖으로 던지지 않는다."""
    try:
        return _restore(items, label, fetch or fetch_head)
    except Exception as e:  # noqa — 복원은 부가 기능, 수집은 반드시 계속한다
        print(f"      [제목복원] {label}: 건너뜀({type(e).__name__})")
        return items
