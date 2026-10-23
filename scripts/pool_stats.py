# -*- coding: utf-8 -*-
"""수집 보조: 기간 판정, 검색어별 성과 기록·이력 저장, 상한 절단(관련도 우선), 근거 문구.
collect.py 가 가져다 쓴다(같은 scripts 폴더에 두어야 한다)."""
import os
import re
import csv
import datetime as dt

from pool_extras import DIRECT_FEED_DOMAINS


def in_window(a, start, end):
    p = a.get("published")
    if not p:
        return False   # 게재일시 불명은 제외 (기간 밖 기사 혼입 방지)
    try:
        return start <= dt.datetime.fromisoformat(p) <= end + dt.timedelta(hours=1)
    except Exception:
        return True


def context_snippet(desc, query, width=20):
    """요약에서 검색어 낱말이 나온 자리의 앞뒤 20자를 뽑는다. 제목에 낱말이 없는 확정 기사의 근거를 로그로 확인하는 용도."""
    d = desc or ""
    low = d.lower()
    for w in query.split():
        w = w.strip('"')
        if not w or w.lower() in ("or", "and"):
            continue
        i = low.find(w.lower())
        if i >= 0:
            a, b = max(0, i - width), min(len(d), i + len(w) + width)
            return ("…" if a > 0 else "") + d[a:i] + "[" + d[i:i + len(w)] + "]" + d[i + len(w):b] + ("…" if b < len(d) else "")
    return d[:40]


QSTATS = []     # 이번 실행의 검색어별 성과 (정기 실행이면 pool/query_stats.csv 에 누적)


def qstat(tag, text, got, start, end, show_text=True, cap=300, kind="naver", sname=""):
    """검색어 하나의 성과를 한 줄로 출력하고 이력에 기록한다. 부정기사(set_a)는 회사명이 들어 있어 태그만 출력한다."""
    win = [a for a in got if in_window(a, start, end)]
    conf = sum(1 for a in win if a.get("confirmed"))
    riskonly = sum(1 for a in win if not a.get("confirmed") and a.get("risk"))
    drop = len(win) - conf - riskonly
    pubs = [a["published"] for a in got if a.get("published")]
    old = min(pubs)[5:16].replace("T", " ") if pubs else "-"
    hit = bool(pubs) and len(got) >= cap and dt.datetime.fromisoformat(min(pubs)) > start
    label = f"{tag} {text}" if show_text else tag
    print(f"      · {label} | 원시 {len(got)} · 기간내 {len(win)} · 확정 {conf} · 위험어만 {riskonly} · 탈락 {drop}"
          f" | 가장 오래된 {old}{' ★' if hit else ''}")
    if show_text:   # 제목에 낱말이 없고 요약에만 있어 확정된 기사의 근거(낱말 앞뒤 20자)를 최대 2건 보여 준다
        for a in [x for x in win if x.get("ctx")][:2]:
            print(f"          ↳ 요약 근거: {a['ctx']}  | 제목: {(a.get('title') or '')[:28]}")
    QSTATS.append({"set": sname, "tag": tag, "kind": kind, "query": text if show_text else "",
                   "raw": len(got), "window": len(win), "confirmed": conf, "riskonly": riskonly,
                   "dropped": drop, "oldest": old, "capped": int(hit)})


STATS_FILE = "query_stats.csv"
STATS_KEEP_DAYS = 60
STATS_HEAD = ["date", "run", "set", "tag", "kind", "query", "raw", "window", "confirmed", "riskonly", "dropped", "oldest", "capped"]


def save_query_stats(outdir, end, stats):
    """정기 수집 때만 호출한다. 같은 날 이전 실행분은 교체하고 60일이 지난 행은 지운다.
    부정기사(set_a)의 검색어 칸은 비어 있다(공개 저장소라 회사명을 쓰지 않는다)."""
    path = os.path.join(outdir, STATS_FILE)
    old = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            for i, row in enumerate(csv.reader(f)):
                if i == 0 and row and row[0] == "date":
                    continue
                if len(row) == len(STATS_HEAD):
                    old.append(row)
    today = end.strftime("%Y-%m-%d")
    cutoff = (end - dt.timedelta(days=STATS_KEEP_DAYS)).strftime("%Y-%m-%d")
    keep = [r for r in old if r[0] >= cutoff and r[0] != today]
    run = end.strftime("%H:%M")
    for s in stats:
        keep.append([today, run, s["set"], s["tag"], s["kind"], s["query"], s["raw"], s["window"],
                     s["confirmed"], s["riskonly"], s["dropped"], s["oldest"], s["capped"]])
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(STATS_HEAD)
        w.writerows(keep)
    return len(keep)


GOOGLE_TAG = re.compile(r"[abc](r|gb|en|id|ro)\d+$")   # 구글 뉴스 RSS(한국판·해외 판) 검색어 태그


def keep_priority(a):
    """상한 절단 때 남기는 순서(작을수록 먼저).
    0: ESG 전문지 직접 수집, 구글 뉴스(해외·RSS) 확정 기사, 제목에 검색어 낱말이 있는 확정 기사(검색어당 상한 이내)
    1: 요약에만 낱말이 있는 확정 기사, 또는 검색어당 상한을 넘은 기사
    2: 약한(?) 검색어 태그만 있는 기사
    3: 그 밖의 매체 훑기(관련도 필터를 안 거친 기사)"""
    tags = a.get("tags") or set()
    real = [t for t in tags if t.rstrip("?") != "sw"]
    media = (a.get("media") or "").lower()
    if any(media.endswith(d) for d in DIRECT_FEED_DOMAINS):
        return 0
    confirmed = [t for t in real if not t.endswith("?")]
    if confirmed:
        if any(GOOGLE_TAG.match(t) for t in confirmed):
            return 0
        if a.get("th", True) and not a.get("over"):
            return 0
        return 1
    return 2 if real else 3


def apply_query_cap(items, cap):
    """한 검색어가 목록을 독차지하지 못하게, 제목에 낱말이 있는 확정 기사를 검색어마다 최신순 cap줄까지만
    0등급으로 두고 나머지는 1등급으로 내린다(버리지는 않는다). 내린 줄 수를 돌려준다."""
    cnt, n_over = {}, 0
    for a in sorted(items, key=lambda x: x.get("published") or "", reverse=True):
        tags = [t for t in (a.get("tags") or set())
                if not t.endswith("?") and t != "sw" and not GOOGLE_TAG.match(t)]
        if not tags or not a.get("th", True):
            continue
        ok = any(cnt.get(t, 0) < cap for t in tags)
        for t in tags:
            cnt[t] = cnt.get(t, 0) + 1
        if not ok:
            a["over"] = True
            n_over += 1
    return n_over


def _round_robin_by_date(items, limit):
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
    return picked


def balance_by_date(items, limit, prio=None):
    """건수 상한을 넘을 때, 날짜별로 골고루 남긴다.
    최신순으로 자르면 검색기간 첫날(월요일 기준 금요일) 기사가 통째로
    날아가므로, 각 날짜에서 돌아가며 뽑는다."""
    if len(items) <= limit:
        return items, False
    if prio is None:
        picked = _round_robin_by_date(items, limit)
    else:
        # 관련도 순(prio 값이 작을수록 먼저)으로 남기고, 한 등급이 남은 자리보다 많으면 날짜별로 골고루 뽑는다.
        tiers = {}
        for a in items:
            tiers.setdefault(prio(a), []).append(a)
        picked, info = [], []
        for p in sorted(tiers):
            room = limit - len(picked)
            part = tiers[p]
            take = part if len(part) <= room else _round_robin_by_date(part, max(room, 0))
            picked += take
            info.append(f"{p}등급 {len(take)}/{len(part)}")
        print("      절단 우선순위별 유지(0=전문지·해외·제목에 낱말이 있는 확정 기사, 1=요약에만 있거나 검색어당 상한 초과, 2=약한 검색어, 3=훑기): " + ", ".join(info))
    picked.sort(key=lambda a: (a.get("published") or ""), reverse=True)   # 절단 여부와 상관없이 최신순
    kept = {}
    for a in picked:
        kept.setdefault((a.get("published") or "")[:10], 0)
        kept[(a.get("published") or "")[:10]] += 1
    print(f"      날짜별 배분: " +
          ", ".join(f"{d[5:]} {n}건" for d, n in sorted(kept.items())))
    return picked, True
