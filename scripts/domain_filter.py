#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""수집 제외 사이트 (collect.py 가 불러 씁니다).

회사 규정상 접속이 차단된 사이트의 기사는 읽을 수 없으므로 수집 단계에서 아예 뺍니다.
- 목록: config/blocked_domains.json 의 "domains" (도메인 또는 상위 도메인, 하위 도메인까지 포함).
- 판정: 네이버 기사는 주소의 도메인, 구글 중계 기사는 매체 칸(구글이 알려 준 출처)으로 판정합니다.
- 중복 제거·상한 계산 이전에 빼서, 차단 사이트 기사가 자리를 차지하거나 다른 사이트의 같은 기사를 밀어내지 않게 합니다.
- 파일이 없거나 깨져 있어도 수집은 그대로 진행합니다(아무것도 빼지 않음). 로그에는 건수만 출력합니다.
"""
import json
import os
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config", "blocked_domains.json")
_cache = {}


def _norm(h):
    return re.sub(r"^www\.", "", (h or "").strip().lower().rstrip("."))


def blocked_domains(path=None):
    p = path or PATH
    if p in _cache:
        return _cache[p]
    out = []
    try:
        d = json.load(open(p, encoding="utf-8"))
        raw = d.get("domains", []) if isinstance(d, dict) else d
        out = [x for x in (_norm(v) for v in raw if isinstance(v, str)) if x and "." in x and "/" not in x]
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa — 깨진 파일 때문에 수집이 멈추면 안 된다
        print(f"      [수집 제외] {os.path.basename(p)} 를 읽지 못했습니다({type(e).__name__}) — 제외 없이 계속")
    _cache[p] = out
    return out


def _hit(h, doms):
    h = _norm(h)
    for d in doms:
        if h == d or h.endswith("." + d):
            return d
    return None


def which_blocked(a, doms):
    """차단 도메인에 걸리면 그 도메인, 아니면 None."""
    u = a.get("url") or ""
    if u.startswith("http") and "news.google.com" not in u:
        m = re.match(r"https?://([^/:?#]+)", u)
        if m and _hit(m.group(1), doms):
            return _hit(m.group(1), doms)
    med = (a.get("media") or "").strip()
    if med and " " not in med and "." in med:               # 구글 중계 기사의 매체 칸은 출처 도메인(예: klikmx.com)인 경우가 많다
        return _hit(med, doms)
    return None


def drop_blocked(items, label="", path=None):
    """차단 사이트 기사를 뺀 목록을 돌려준다. 어떤 오류도 밖으로 던지지 않는다."""
    try:
        doms = blocked_domains(path)
        if not doms:
            return items
        kept, cnt = [], Counter()
        for a in items:
            d = which_blocked(a, doms)
            if d:
                cnt[d] += 1
            else:
                kept.append(a)
        if cnt:
            print(f"      [수집 제외] {label}: 접속 차단 사이트 기사 {sum(cnt.values())}건 제외 ("
                  + ", ".join(f"{k} {v}" for k, v in cnt.most_common()) + ")")
        return kept
    except Exception as e:  # noqa
        print(f"      [수집 제외] {label}: 건너뜀({type(e).__name__})")
        return items
