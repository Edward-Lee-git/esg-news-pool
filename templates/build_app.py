#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""통합 모니터링 앱 조립 도구.

세 리포트(부정기사·규제동향·일반동향)의 HTML에는 `<script type="application/json" id="report-data">` 안에
리포트 자료(JSON)가 들어 있다. 이 도구는 그 자료와 스타일을 꺼내 app_shell.html 틀에 넣어
탭이 있는 한 페이지(app.html)를 만든다. 리포트 틀(shell_head/tail)이 바뀌어도 자료 모양이 같으면 그대로 동작한다.

사용:
  python3 build_app.py --neg 부정기사.html --reg 규제동향.html --gen 일반동향.html [--queries 검색어정리.html] \
          --shell app_shell.html --out app.html [--today 2026-10-26] [--require-fresh]
검색어 탭: --queries 로 검색어 정리 페이지(HTML)를 주면 거기 내장된 자료(`<script id="data">`)에서 검색어 목록을 꺼내 읽기 전용으로 보여 준다.
주지 않거나 읽지 못해도 조립은 성공하고, 검색어 탭에는 안내 문구와 편집 페이지 링크만 나온다.
종료 코드: 0 = 조립 성공(오래된 리포트가 있어도 성공, 화면에 '미갱신' 표시), 2 = 조립 불가(스타일이나 자료를 하나도 못 읽음)
마지막 줄에 JSON 한 줄 요약을 출력한다.
"""
import argparse
import datetime as dt
import json
import re
import sys

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:                       # zoneinfo가 없는 환경 대비
    KST = dt.timezone(dt.timedelta(hours=9))

DATA_RE = re.compile(r'<script type="application/json" id="report-data">(.*?)</script>', re.S)
STYLE_RE = re.compile(r'<style[^>]*>.*?</style>', re.S)
RUN_RE = re.compile(r'오늘\(한국시각\)\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})')
COL_RE = re.compile(r'수집일\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})')
QDATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)
DEFAULT_EDIT_URL = "https://claude.ai/artifact/4STKFwFVkUdT8eMGzmVg1k"


def load_report(path):
    """(자료 dict 또는 None, 스타일 목록, 메시지)"""
    if not path:
        return None, [], "경로 없음"
    try:
        html = open(path, encoding="utf-8").read()
    except Exception as e:
        return None, [], f"파일을 읽지 못함: {e}"
    styles = STYLE_RE.findall(html)
    m = DATA_RE.search(html)
    if not m:
        return None, styles, "report-data 블록 없음"
    try:
        return json.loads(m.group(1)), styles, ""
    except Exception as e:
        return None, styles, f"자료(JSON) 해석 실패: {e}"


def load_queries(path, edit_url):
    """검색어 정리 페이지에서 읽기 전용 목록에 필요한 필드만 꺼낸다. 실패하면 (None, 사유)."""
    if not path:
        return None, "경로 없음"
    try:
        html = open(path, encoding="utf-8").read()
        m = QDATA_RE.search(html)
        if not m:
            return None, "data 블록 없음"
        d = json.loads(m.group(1))
        keep = ("tag", "q", "kind", "rec", "cat", "sub", "ord", "rep")
        sets = [{"key": s.get("key"), "title": s.get("title"), "rows": [{k: r.get(k) for k in keep} for r in s.get("rows", [])]}
                for s in d.get("sets", [])]
        if len(sets) != 2 or not all(s["rows"] for s in sets):
            return None, "규제동향·일반동향 검색어가 비어 있음"
        seta = [{"tag": r.get("tag"), "q": r.get("q"), "kind": r.get("kind")} for r in d.get("seta", [])]
        basis = d.get("basis", "") or ""
        basis = re.split(r"\s*\(규제동향", basis)[0].strip()      # 편집 페이지용 뒷 문장은 떼고 '기준: …목록입니다.'까지만 쓴다
        if len(basis) > 140:
            basis = basis[:140].rstrip() + "…"
        return {"basis": basis, "cats": d.get("cats", []), "catlabel": d.get("catlabel", {}),
                "sets": sets, "seta": seta, "edit_url": edit_url}, ""
    except Exception as e:
        return None, f"해석 실패: {e}"


def status_of(data, today):
    """리포트의 바닥글(footer.verify)에서 작성·수집 시각을 읽어 오늘 갱신 여부를 판단한다."""
    if data is None:
        return {"status": "missing"}
    verify = ((data.get("footer") or {}).get("verify")) or ""
    r, c = RUN_RE.search(verify), COL_RE.search(verify)
    out = {"status": "unknown"}
    if r:
        out.update(run_date=r.group(1), run_time=r.group(2))
        out["status"] = "fresh" if r.group(1) == today else "stale"
    if c:
        out["collected"] = f"{c.group(1)[5:]} {c.group(2)}"
    return out


def js_json(obj):
    """<script> 안에 안전하게 넣을 JSON(여는 꺾쇠를 이스케이프)."""
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--neg"); ap.add_argument("--reg"); ap.add_argument("--gen")
    ap.add_argument("--queries", help="검색어 정리 페이지 HTML 경로(선택)")
    ap.add_argument("--queries-url", default=DEFAULT_EDIT_URL, help="검색어 탭의 '편집 페이지 열기' 링크")
    ap.add_argument("--shell", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--css", help="리포트 스타일을 읽지 못할 때 쓸 report.css 경로(선택)")
    ap.add_argument("--today", help="YYYY-MM-DD (기본: 한국 날짜)")
    ap.add_argument("--require-fresh", action="store_true", help="하나라도 오늘 갱신이 아니면 종료 코드 3")
    a = ap.parse_args()

    now = dt.datetime.now(KST)
    today = a.today or now.strftime("%Y-%m-%d")
    reports, styles, notes = {}, [], {}
    for key, path in (("neg", a.neg), ("reg", a.reg), ("gen", a.gen)):
        data, st, msg = load_report(path)
        reports[key] = data
        if st and not styles:
            styles = st
        if msg:
            notes[key] = msg
    if not styles and a.css:
        try:
            styles = [f"<style>{open(a.css, encoding='utf-8').read()}</style>"]
        except Exception:
            pass
    if not styles:
        print(json.dumps({"ok": False, "error": "리포트 스타일을 하나도 읽지 못함", "notes": notes}, ensure_ascii=False))
        return 2
    if all(v is None for v in reports.values()):
        print(json.dumps({"ok": False, "error": "리포트 자료를 하나도 읽지 못함", "notes": notes}, ensure_ascii=False))
        return 2

    qry, qmsg = load_queries(a.queries, a.queries_url)
    if qmsg and a.queries:
        notes["queries"] = qmsg
    meta = {"built_at": now.strftime("%Y-%m-%d %H:%M"), "today": today, "reports": {}, "queries": bool(qry)}
    collected = []
    for key, data in reports.items():
        st = status_of(data, today)
        if key in notes:
            st["note"] = notes[key]
        meta["reports"][key] = st
        if st.get("collected"):
            collected.append(st["collected"])
    if collected:
        meta["collected"] = max(collected)          # 가장 최근 수집 시각

    shell = open(a.shell, encoding="utf-8").read()
    for token in ("__REPORT_STYLES__", "__META__", "__NEG__", "__REG__", "__GEN__", "__QRY__"):
        if token not in shell:
            print(json.dumps({"ok": False, "error": f"틀에 {token} 자리가 없음"}, ensure_ascii=False))
            return 2
    html = (shell.replace("__REPORT_STYLES__", "\n".join(styles))
                 .replace("__META__", js_json(meta))
                 .replace("__NEG__", js_json(reports["neg"]))
                 .replace("__REG__", js_json(reports["reg"]))
                 .replace("__GEN__", js_json(reports["gen"]))
                 .replace("__QRY__", js_json(qry)))
    open(a.out, "w", encoding="utf-8").write(html)

    # 되읽기 검증: 출력 파일의 자료 블록이 모두 해석되는지
    back = open(a.out, encoding="utf-8").read()
    check = {}
    for key, blk in (("neg", "data-neg"), ("reg", "data-reg"), ("gen", "data-gen"), ("qry", "data-qry"), ("meta", "app-meta")):
        m = re.search(r'<script type="application/json" id="%s">(.*?)</script>' % blk, back, re.S)
        try:
            v = json.loads(m.group(1)) if m else None
            check[key] = "null" if v is None else "ok"
        except Exception:
            check[key] = "parse-error"
    summary = {"ok": all(v != "parse-error" for v in check.values()), "bytes": len(html.encode()), "today": today,
               "status": {k: v["status"] for k, v in meta["reports"].items()}, "blocks": check, "notes": notes}
    print(json.dumps(summary, ensure_ascii=False))
    if a.require_fresh and any(v["status"] != "fresh" for v in meta["reports"].values()):
        return 3
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
