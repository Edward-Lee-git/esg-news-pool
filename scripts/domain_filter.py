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


# ─────────────────────────────────────────────────────────────────────────────
# 훑기(구글 site: 검색)에 섞여 들어오는 계열 매체 제외
# - 훑기 결과에서만 뺀다(검색어로 찾은 기사에는 적용하지 않는다).
# - 목록(config/affiliate_media.json)에 있는 것만 뺀다. names = 구글이 알려 주는 표시 이름, hosts = 출처 도메인(하위 도메인 포함).
# - 로그에 사이트별 매체명 분포를 찍는다: ✕ = 제외, ? = 지정한 매체의 이름이 아니면서 목록에도 없는 이름(새 계열 후보).
# - 파일이 없거나 깨져 있어도 수집은 그대로 진행한다(아무것도 빼지 않고, 분포 로그만 남긴다).
# ─────────────────────────────────────────────────────────────────────────────
import html as _html

AFF_PATH = os.path.join(ROOT, "config", "affiliate_media.json")
_DOMLIKE = re.compile(r"^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+$")


def _nname(s):
    return re.sub(r"\s+", " ", _html.unescape(str(s or "")).strip().lower())


def _host_of(u):
    m = re.match(r"https?://([^/:?#]+)", (u or "").strip(), re.I)
    return _norm(m.group(1)) if m else ""


def affiliates(path=None):
    p = path or AFF_PATH
    if ("aff", p) in _cache:
        return _cache[("aff", p)]
    out = {"names": set(), "hosts": []}
    try:
        d = json.load(open(p, encoding="utf-8"))
        out["names"] = {_nname(x) for x in d.get("names", []) if isinstance(x, str) and _nname(x)}
        out["hosts"] = [h for h in (_norm(x) for x in d.get("hosts", []) if isinstance(x, str)) if h and "." in h and "/" not in h]
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa
        print(f"      [훑기 계열] {os.path.basename(p)} 를 읽지 못했습니다({type(e).__name__}) — 제외 없이 계속")
    _cache[("aff", p)] = out
    return out


def _is_aff(a, cfg):
    """계열 매체이면 걸린 이름·도메인, 아니면 None."""
    med = _nname(a.get("media"))
    if med and med in cfg["names"]:
        return med
    cands = [_host_of(a.get("src"))]
    if med and _DOMLIKE.match(med):
        cands.append(_norm(med))
    for h in cands:
        if h and any(h == d or h.endswith("." + d) for d in cfg["hosts"]):
            return h
    return None


def _expected(site):
    """그 사이트 훑기에서 나와야 할 매체 이름(도메인 형태·접두어 변형·이름 표의 한글 이름)."""
    site = _norm(site)
    exp = {site, "www." + site, "m." + site, "view." + site, "biz." + site, "news." + site}
    try:
        from media_names import table
        for k, v in table().items():
            if k == site or k.endswith("." + site):
                exp.add(_nname(v))
                exp.add(_nname(k))
    except Exception:  # noqa
        pass
    return exp


def filter_sweep(items, site="", path=None):
    """구글 사이트 훑기 결과에서 계열 매체를 빼고 사이트별 매체명 분포를 로그로 남긴다. 어떤 오류도 밖으로 던지지 않는다."""
    try:
        cfg = affiliates(path)
        exp = _expected(site)
        cnt, dropped, kept = {}, 0, []
        for a in items:
            hit = _is_aff(a, cfg)
            nm = (a.get("media") or "").strip() or "(이름 없음)"
            key = (nm, "x" if hit else ("" if (_nname(nm) in exp) else "?"))
            cnt[key] = cnt.get(key, 0) + 1
            if hit:
                dropped += 1
            else:
                kept.append(a)
        if cnt:
            top = sorted(cnt.items(), key=lambda kv: -kv[1])
            show = " · ".join(f"{n} {c}" + {"x": "✕", "?": "?", "": ""}[m] for (n, m), c in top[:8])
            more = f" 외 {len(top) - 8}종" if len(top) > 8 else ""
            hosts = {}
            for a in items:
                h = _host_of(a.get("src"))
                if h:
                    hosts[h] = hosts.get(h, 0) + 1
            hs = " | 출처주소 " + ", ".join(f"{h} {c}" for h, c in sorted(hosts.items(), key=lambda kv: -kv[1])[:2]) if hosts else " | 출처주소 없음"
            pubs = sorted(str(a.get("published"))[5:16].replace("T", " ") for a in items if a.get("published"))
            rng = f" | 기간 {pubs[0]}~{pubs[-1]}" if pubs else ""
            print(f"      [훑기 매체명] {site}: {show}{more}{hs}{rng}" + (f" → 계열 {dropped}건 제외" if dropped else ""))
        return kept
    except Exception as e:  # noqa
        print(f"      [훑기 계열] {site}: 건너뜀({type(e).__name__})")
        return items


# 사이트별 훑기 기간(일). 지정한 사이트는 수집 기간보다 길게 가져온다(더벨: 유료 기사가 7~12일 뒤 무료로 풀리고 구글은 그보다 앞선 시각을 붙이므로).
SWEEP_DAYS = {"thebell.co.kr": 10}


def sweep_days(site, days):
    """그 사이트의 훑기 기간(일). 지정이 없으면 수집 기간 그대로, 있으면 둘 중 큰 값."""
    try:
        return max(int(days), int(SWEEP_DAYS.get(_norm(site), 0)))
    except Exception:  # noqa
        return days
