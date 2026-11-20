#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""잘려 온 기사 제목 복원 (collect.py 가 불러 씁니다).

네이버 검색 결과는 긴 제목을 약 70바이트(한글 약 35자)에서 「...」로 줄여 보냅니다.
이 모듈은 그런 기사(직접 주소 + 제목이 ...·… 로 끝남)의 페이지를 열어 제목을 되찾습니다.

- 후보: og:title → twitter:title → <title>. 후보가 잘린 제목의 앞부분으로 시작할 때만(공백·구두점 무시) 바꿉니다.
  맞지 않으면 그대로 둡니다(엉뚱한 제목으로 바꾸지 않는 것이 원칙).
- 안전장치: 병렬 WORKERS, 전체 시간 TOTAL_BUDGET, 성공 없이 연속 실패·도메인별 실패 시 중단, 같은 주소는 한 번만 받음.
- 어떤 오류도 수집을 멈추지 않습니다(실패하면 원래 제목 유지). 제외 기준 키는 주소 키가 함께 있어 제목이 바뀌어도 유지됩니다.
- 기사 제목은 공개 정보이지만 로그에는 개수만 출력합니다.
환경변수 TITLE_RESTORE = on(기본) | off
"""
import html
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

WORKERS = 8
TOTAL_BUDGET = 150        # 초. 세 세트 합계
ABORT_FAILS = 30          # 성공이 한 번도 없이 연속 실패가 이만큼이면 중단
DOMAIN_FAILS = 3          # 한 도메인에서 성공 없이 이만큼 실패하면 그 도메인은 건너뜀
MAX_LEN = 200
MIN_STEM = 8              # 비교에 쓰는 앞부분이 이보다 짧으면(정규화 후) 복원하지 않음

_ELL = re.compile(r"\s*(?:\.{2,}|…|⋯)\s*$")
_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"([\w:.-]+)\s*=\s*(\"[^\"]*\"|'[^']*')")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
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


def _host(u):
    m = re.match(r"https?://([^/:?#]+)", u or "")
    return (m.group(1).lower() if m else "")


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
    if _st["t0"] is None:
        _st["t0"] = time.time()
    n_ok = n_fail = n_skip = 0
    i = 0
    while i < len(todo):
        if time.time() - _st["t0"] > TOTAL_BUDGET:
            n_skip += len(todo) - i
            _st["why"]["시간 초과"] += 1
            break
        chunk = todo[i:i + WORKERS * 2]
        i += WORKERS * 2
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            res = list(ex.map(lambda a: _one(a, fetch), chunk))
        for a, (new, why) in zip(chunk, res):
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
                    _st["streak"] += 1                     # 접속 자체가 실패한 경우만 연속 실패로 센다
        if _st["ok"] == 0 and _st["streak"] >= ABORT_FAILS:
            _st["off"] = f"접속 실패가 연속 {_st['streak']}건"
            n_skip += len(todo) - i
            break
    _st["skipped"] += n_skip
    top = ", ".join(f"{k} {c}" for k, c in _st["why"].most_common(3))
    print(f"      [제목복원] {label}: 잘린 제목 {len(todo)}건 → 복원 {n_ok}건 · 실패 {n_fail}건 · 건너뜀 {n_skip}건"
          + (f" ({top})" if n_fail or n_skip else "") + (f" — {_st['off']}으로 중단" if _st["off"] else ""))
    return items


def restore_titles(items, label="", fetch=None):
    """items 의 잘린 제목을 복원해 같은 목록을 돌려준다. 어떤 오류도 밖으로 던지지 않는다."""
    try:
        if fetch is None:
            from body_check import fetch as _f
            fetch = _f
        return _restore(items, label, fetch)
    except Exception as e:  # noqa — 복원은 부가 기능, 수집은 반드시 계속한다
        print(f"      [제목복원] {label}: 건너뜀({type(e).__name__})")
        return items
