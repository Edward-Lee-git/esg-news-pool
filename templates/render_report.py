#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESG 모니터링 리포트 렌더러 (부정기사 · 규제동향 · 일반동향 공통)
==============================================================
자료(JSON) -> 게시용 HTML 한 장. 서체·색·크기·열 구조는 이 파일 안의 CSS 한 벌로 고정되어
리포트마다, 날마다 모양이 달라지지 않는다. 표준 라이브러리만 사용한다.

사용:   python render_report.py 자료.json 결과.html [--theme light|dark]
        python render_report.py --css          (CSS만 출력)
--theme 은 미리보기용이다. 생략하면 보는 기기의 라이트/다크 설정을 따른다(prefers-color-scheme).

자료(JSON) 형식 (자세한 설명은 schema.md):
{
  "title": "...", "kicker": "...", "meta": "...",
  "pills":  [{"label": "환경 E", "value": "12 / 10", "cat": "e"}, {"label": "링크 없음", "auto": "nolink"}],
  "issues": [{"title": "...", "body": "..."}],
  "legend": "사업영역 관련(그룹 맨 위에 표시)", "mark_label": "사업 관련",   # 선택(일반동향)
  "groups": [{"cat": "e|s|g|n|p|a", "title": "...", "num": "01", "count": "12건",
              "head_chips": [{"text": "...", "kind": "alert|sub"}],   # 선택
              "cols": "mark|plain", "open": true,
              "rows": [{"title": "...", "url": "https://..."|null, "summary": "...", "source": "...",
                        "date": "YYYY-MM-DD", "tags": [{"text": "...", "kind": "sub|biz"}],
                        "needs_check": false, "mark": false}],
              "note": "...", "more": {"label": "...", "text": "..."}}],
  "footer": {"limit": "...", "verify": "..."}
}
규칙(렌더러가 강제):  링크가 없는 기사(url 이 null)는 자동으로 '링크 없음' 점선 태그가 붙고 제목은 클릭되지 않는다.
  분류 칸의 순서는 항상 [분류 칩들] -> [확인 필요] -> [링크 없음]. 모든 글자는 HTML 이스케이프되고
  url 은 http/https 만 허용된다. cols 가 mark 인 그룹은 mark=true 인 기사가 맨 위로 온다(그 안의 순서는 유지).
"""
import sys
import json
import html
import urllib.parse

# ---------------------------------------------------------------- 확정된 양식 값 (design_tokens.json 과 동일)
TOKENS = {
  "light": {
    "base": {
      "page": "#F6F5F0",
      "surface": "#FFFFFF",
      "ink": "#18201C",
      "muted": "#55625B",
      "rule": "#E2E0D6",
      "accent": "#0F766E",
      "chipSub": {
        "bg": "#EFEDE4",
        "fg": "#3A3731"
      },
      "chipBiz": {
        "bg": "#D8EDE8",
        "fg": "#0B4F49"
      },
      "dashedTag": {
        "border": "#8E958F",
        "bg": "#FFFFFF",
        "fg": "#55625B"
      }
    },
    "categories": {
      "E": {
        "dot": "#0F766E",
        "headerStrip": "#E1F0EC",
        "linkTitle": "#0A5A53",
        "bodyText": "#2B3631",
        "areaBg": "#F6FAF9"
      },
      "S": {
        "dot": "#1D4ED8",
        "headerStrip": "#E3EAFB",
        "linkTitle": "#1C3FA3",
        "bodyText": "#2B3631",
        "areaBg": "#F6F8FE"
      },
      "G": {
        "dot": "#B45309",
        "headerStrip": "#F7EAD6",
        "linkTitle": "#8A4508",
        "bodyText": "#2B3631",
        "areaBg": "#FDF9F2"
      },
      "N": {
        "dot": "#475467",
        "headerStrip": "#ECEFF3",
        "linkTitle": "#2B3441",
        "bodyText": "#2B3631",
        "areaBg": "#F8F9FA"
      },
      "P": {
        "dot": "#7E22CE",
        "headerStrip": "#F1E8FB",
        "linkTitle": "#5B1C99",
        "bodyText": "#2B3631",
        "areaBg": "#FAF6FE"
      },
      "A": {
        "dot": "#C2410C",
        "headerStrip": "#FBE8DF",
        "linkTitle": "#9A2F0B",
        "bodyText": "#2B3631",
        "areaBg": "#FEF8F4"
      }
    }
  },
  "dark": {
    "base": {
      "page": "#121614",
      "surface": "#1A1F1D",
      "ink": "#E7ECE9",
      "muted": "#9BA8A1",
      "rule": "#2D3430",
      "accent": "#3CC4B5",
      "chipSub": {
        "bg": "#2A312D",
        "fg": "#D2D8D4"
      },
      "chipBiz": {
        "bg": "#14504A",
        "fg": "#BFF0E9"
      },
      "dashedTag": {
        "border": "#6D7770",
        "bg": "#1A1F1D",
        "fg": "#9BA8A1"
      }
    },
    "categories": {
      "E": {
        "dot": "#3CC4B5",
        "headerStrip": "#173430",
        "linkTitle": "#72E3D5",
        "bodyText": "#D5DCD8",
        "areaBg": "#151E1C"
      },
      "S": {
        "dot": "#7C9BFF",
        "headerStrip": "#1B2850",
        "linkTitle": "#A5BAFF",
        "bodyText": "#D5DCD8",
        "areaBg": "#161B2B"
      },
      "G": {
        "dot": "#EBA64E",
        "headerStrip": "#3D2C13",
        "linkTitle": "#F3BE72",
        "bodyText": "#D5DCD8",
        "areaBg": "#201A11"
      },
      "N": {
        "dot": "#9AA5B5",
        "headerStrip": "#262C33",
        "linkTitle": "#D3DAE3",
        "bodyText": "#D5DCD8",
        "areaBg": "#1B2024"
      },
      "P": {
        "dot": "#B98BEA",
        "headerStrip": "#2D1F44",
        "linkTitle": "#CFA9F5",
        "bodyText": "#D5DCD8",
        "areaBg": "#1C1726"
      },
      "A": {
        "dot": "#F08A5D",
        "headerStrip": "#43241A",
        "linkTitle": "#F5A07A",
        "bodyText": "#D5DCD8",
        "areaBg": "#221612"
      }
    }
  }
}

FONT_LINK = "https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap"
CATS = ["e", "s", "g", "n", "p", "a"]
CAT_KEYS = {"e": "E", "s": "S", "g": "G", "n": "N", "p": "P", "a": "A"}


def _vars(theme):
    b = theme["base"]
    out = [("page", b["page"]), ("surface", b["surface"]), ("ink", b["ink"]), ("muted", b["muted"]), ("rule", b["rule"]),
           ("accent", b["accent"]), ("text", theme["categories"]["E"]["bodyText"]),
           ("chip-bg", b["chipSub"]["bg"]), ("chip-fg", b["chipSub"]["fg"]),
           ("biz-bg", b["chipBiz"]["bg"]), ("biz-fg", b["chipBiz"]["fg"]), ("dash", b["dashedTag"]["border"])]
    for c in CATS:
        t = theme["categories"][CAT_KEYS[c]]
        out += [(f"{c}-dot", t["dot"]), (f"{c}-strip", t["headerStrip"]), (f"{c}-title", t["linkTitle"]), (f"{c}-area", t["areaBg"])]
    return "".join(f"--{k}:{v};" for k, v in out)


def _icon(paths):
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' "
           "stroke-linecap='round' stroke-linejoin='round'>" + paths + "</svg>")
    return "url(\"data:image/svg+xml," + urllib.parse.quote(svg, safe="/:=") + "\")"


ICON_NOLINK = _icon("<path d='M10 14a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7L11.5 6.8'/>"
                    "<path d='M14 10a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 0 0 5.7 5.7l1.1-1.1'/><path d='M3 3l18 18'/>")
ICON_CHECK = _icon("<path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'/><path d='M14 3v5h5'/>"
                   "<path d='M9 13h6'/><path d='M9 17h6'/>")


def build_css():
    light, dark = _vars(TOKENS["light"]), _vars(TOKENS["dark"])
    cat_rules = "".join(
        f".cat-{c}{{--dot:var(--{c}-dot);--strip:var(--{c}-strip);--title:var(--{c}-title);--area:var(--{c}-area)}}" for c in CATS)
    return f"""
:root{{--font:'Noto Sans KR','Malgun Gothic',sans-serif;{light}}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{{dark}}}}}
:root[data-theme="dark"]{{{dark}}}
:root{{box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}}
html{{scroll-padding-top:env(safe-area-inset-top,0px);background:var(--page);-webkit-text-size-adjust:100%}}
*,*::before,*::after{{box-sizing:inherit}}
body{{margin:0;background:var(--page);color:var(--ink);font:14px/22px var(--font)}}
{cat_rules}
.wrap{{max-width:1040px;margin:0 auto;padding:40px 32px 64px}}
.kicker{{font-size:12px;line-height:16px;font-weight:500;color:var(--muted)}}
h1{{margin:6px 0 0;font-size:24px;line-height:32px;font-weight:700}}
.meta{{margin-top:4px;font-size:13px;line-height:20px;color:var(--muted);font-variant-numeric:tabular-nums}}
.pills{{margin-top:20px;display:flex;flex-wrap:wrap;gap:8px}}
.pill{{display:inline-flex;align-items:baseline;gap:8px;padding:6px 12px;border:1px solid var(--rule);border-radius:8px;background:var(--surface);font-size:13px;line-height:20px;color:var(--muted)}}
.pill[class*="cat-"]{{background:var(--strip)}}
.pill .pdot{{width:8px;height:8px;border-radius:50%;background:var(--dot);flex:none;align-self:center}}
.pill strong{{font-size:16px;line-height:20px;font-weight:700;color:var(--ink);font-variant-numeric:tabular-nums}}
.card{{margin-top:24px;background:var(--surface);border:1px solid var(--rule);border-radius:12px;padding:12px 20px 8px}}
.card h2{{margin:4px 0 6px;font-size:16px;line-height:24px;font-weight:700}}
.card ol{{margin:0;padding:0}}
.card li{{display:flex;gap:12px;padding:9px 0;border-top:1px solid var(--rule);list-style:none}}
.card .n{{flex:none;width:20px;font-size:13px;line-height:21px;font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}}
.card .t{{font-size:13.5px;line-height:21px}}
.card .t strong{{font-weight:700}}
.card .t span{{color:var(--text)}}
.legend{{margin-top:24px;display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px;line-height:18px;color:var(--muted)}}
.legend .ldot{{width:8px;height:8px;border-radius:50%;background:var(--accent)}}
details.group{{margin-top:20px;background:var(--area);border:1px solid var(--rule);border-radius:12px;overflow:hidden}}
summary{{list-style:none;cursor:pointer}}
summary::-webkit-details-marker{{display:none}}
summary.gh{{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 20px;background:var(--strip)}}
summary.gh::after,summary.more-h::after{{content:"";flex:none;width:7px;height:7px;margin:0 4px 3px 0;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg)}}
details[open]>summary.gh::after,details[open]>summary.more-h::after{{transform:rotate(-135deg);margin-bottom:-3px}}
.gl{{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;min-width:0}}
.gdot{{width:10px;height:10px;border-radius:50%;background:var(--dot);flex:none}}
.gnum{{font-size:13px;line-height:20px;font-weight:700;color:var(--title);font-variant-numeric:tabular-nums}}
.gtitle{{margin:0;font-size:16px;line-height:24px;font-weight:700;color:var(--title)}}
.gcount{{font-size:13px;line-height:20px;color:var(--muted);font-variant-numeric:tabular-nums}}
.tbl-scroll{{overflow-x:auto}}
.tbl-in{{min-width:760px;padding:0 20px 4px}}
.cols-mark{{--cols:28px minmax(0,1fr) 96px 88px 176px}}
.cols-plain{{--cols:minmax(0,1fr) 96px 88px 200px}}
.th,.tr{{display:grid;grid-template-columns:var(--cols);column-gap:12px}}
.th{{padding:10px 0 8px;font-size:12px;line-height:16px;font-weight:500;color:var(--muted)}}
.tr{{padding:12px 0;border-top:1px solid var(--rule);align-items:start}}
.c-main{{min-width:0}}
.title{{font-size:14.5px;line-height:22px;font-weight:600}}
a.title{{color:var(--title);text-decoration:none;cursor:pointer}}
a.title:hover{{text-decoration:underline}}
a.title:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
span.title.nolink{{color:var(--ink);cursor:default}}
.sum{{margin-top:2px;font-size:13.5px;line-height:21px;color:var(--text)}}
.c-src{{font-size:13px;line-height:22px;font-weight:600;color:var(--ink)}}
.c-date{{font-size:13px;line-height:22px;color:var(--muted);font-variant-numeric:tabular-nums}}
.c-tags{{display:flex;flex-wrap:wrap;gap:4px}}
.mk{{display:inline-block;width:8px;height:8px;margin-top:8px;border-radius:50%;background:var(--accent)}}
.chip{{display:inline-block;padding:0 7px;border-radius:4px;background:var(--chip-bg);color:var(--chip-fg);font-size:12px;line-height:18px;font-weight:500}}
.chip.biz{{background:var(--biz-bg);color:var(--biz-fg)}}
.chip.alert{{background:var(--a-strip);color:var(--a-title)}}
.tag-dash{{display:inline-flex;align-items:center;gap:4px;padding:0 7px;border-radius:4px;border:1px dashed var(--dash);background:var(--surface);color:var(--muted);font-size:12px;line-height:16px;font-weight:500;cursor:default}}
.tag-dash::before{{content:"";width:13px;height:13px;flex:none;background:currentColor;-webkit-mask:var(--ic) center/contain no-repeat;mask:var(--ic) center/contain no-repeat}}
.tag-dash.nolink{{--ic:{ICON_NOLINK}}}
.tag-dash.check{{--ic:{ICON_CHECK}}}
.note{{padding:14px 20px 18px;font-size:13.5px;line-height:21px;color:var(--text)}}
details.more{{margin:4px 20px 14px;border-top:1px solid var(--rule)}}
summary.more-h{{display:flex;align-items:center;gap:6px;padding:10px 0;font-size:13px;line-height:20px;font-weight:500;color:var(--n-title)}}
details.more p{{margin:0 0 4px;font-size:13.5px;line-height:21px;color:var(--text)}}
.foot{{margin-top:32px;padding-top:12px;border-top:1px solid var(--rule);font-size:12px;line-height:18px;color:var(--muted)}}
.foot .verify{{margin-top:4px;font-variant-numeric:tabular-nums}}
"""


# ---------------------------------------------------------------- 렌더링
def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def safe_url(u):
    if u is None or u == "":
        return None
    p = urllib.parse.urlsplit(str(u).strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"url 은 http/https 만 허용됩니다: {u!r}")
    return str(u).strip()


def chip(c):
    kind = c.get("kind", "sub")
    if kind not in ("sub", "biz", "alert"):
        raise ValueError(f"chip kind 오류: {kind!r}")
    return f'<span class="chip{"" if kind == "sub" else " " + kind}">{esc(c["text"])}</span>'


def row(r, with_mark, mark_label):
    url = safe_url(r.get("url"))
    if url:
        title = f'<a class="title" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(r["title"])}</a>'
    else:
        title = f'<span class="title nolink">{esc(r["title"])}</span>'
    summ = f'<div class="sum">{esc(r["summary"])}</div>' if r.get("summary") else ""
    tags = "".join(chip(c) for c in r.get("tags", []))
    if r.get("needs_check"):
        tags += '<span class="tag-dash check">확인 필요</span>'
    if not url:
        tags += '<span class="tag-dash nolink">링크 없음</span>'          # 항상 맨 뒤
    mark = ('<div class="c-mark">' + (f'<i class="mk" title="{esc(mark_label)}"></i>' if r.get("mark") else "") + "</div>") if with_mark else ""
    return (f'<div class="tr">{mark}<div class="c-main">{title}{summ}</div><div class="c-src">{esc(r.get("source"))}</div>'
            f'<div class="c-date">{esc(r.get("date"))}</div><div class="c-tags">{tags}</div></div>')


def group(g, mark_label):
    cat = g.get("cat", "n")
    if cat not in CATS:
        raise ValueError(f"cat 은 {CATS} 중 하나여야 합니다: {cat!r}")
    rows = list(g.get("rows", []))
    cols = g.get("cols", "plain")
    if cols not in ("mark", "plain"):
        raise ValueError(f"cols 오류: {cols!r}")
    if cols == "mark":
        rows.sort(key=lambda r: 0 if r.get("mark") else 1)            # 사업 관련 기사가 맨 위(안의 순서는 유지)
    count = g.get("count") or (f"{len(rows)}건" if rows else "")
    num = f'<span class="gnum">{esc(g["num"])}</span>' if g.get("num") else ""
    hc = "".join(chip(c) for c in g.get("head_chips", []))
    head = (f'<summary class="gh"><span class="gl"><span class="gdot"></span>{num}<h2 class="gtitle">{esc(g["title"])}</h2>{hc}'
            f'<span class="gcount">{esc(count)}</span></span></summary>')
    body = ""
    if rows:
        th = ('<div class="c-mark">주요</div>' if cols == "mark" else "") + "<div>제목 · 요약</div><div>매체</div><div>게재일</div><div>분류</div>"
        body = (f'<div class="tbl-scroll"><div class="tbl-in cols-{cols}"><div class="th">{th}</div>'
                + "".join(row(r, cols == "mark", mark_label) for r in rows) + "</div></div>")
    elif g.get("note"):
        body = f'<div class="note">{esc(g["note"])}</div>'
    more = ""
    if g.get("more") and rows:
        m = g["more"]
        more = (f'<details class="more"><summary class="more-h">{esc(m["label"])}</summary><p>{esc(m["text"])}</p></details>')
    elif g.get("more"):
        body += f'<div class="note">{esc(g["more"]["text"])}</div>'
    return f'<details class="group cat-{cat}"{" open" if g.get("open", True) else ""}>{head}{body}{more}</details>'


def count_nolink(data):
    return sum(1 for g in data.get("groups", []) for r in g.get("rows", []) if not safe_url(r.get("url")))


def pill(p, data):
    value = p.get("value")
    if p.get("auto") == "nolink":
        value = f"{count_nolink(data)}건"
    cat = p.get("cat")
    cls = f"pill cat-{cat}" if cat else "pill"
    dot = '<span class="pdot"></span>' if cat else ""
    return f'<span class="{cls}">{dot}{esc(p["label"])}<strong>{esc(value)}</strong></span>'


def render(data, theme=None):
    theme_attr = f' data-theme="{theme}"' if theme in ("light", "dark") else ""
    pills = "".join(pill(p, data) for p in data.get("pills", []))
    issues = "".join(
        f'<li><span class="n">{i}</span><div class="t"><strong>{esc(x["title"])}</strong>'
        + (f' <span>— {esc(x["body"])}</span>' if x.get("body") else "") + "</div></li>"
        for i, x in enumerate(data.get("issues", []), 1))
    legend = (f'<div class="legend"><span class="ldot"></span>{esc(data["legend"])}</div>' if data.get("legend") else "")
    groups = "".join(group(g, data.get("mark_label", "사업 관련")) for g in data.get("groups", []))
    f = data.get("footer", {})
    foot = (f'<footer class="foot"><div>{esc(f.get("limit"))}</div><div class="verify">{esc(f.get("verify"))}</div></footer>')
    return f"""<!doctype html>
<html lang="ko"{theme_attr}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(data["title"])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="{FONT_LINK}">
<style>{build_css()}</style>
</head>
<body>
<div class="wrap">
<div class="kicker">{esc(data.get("kicker"))}</div>
<h1>{esc(data["title"])}</h1>
<div class="meta">{esc(data.get("meta"))}</div>
<div class="pills">{pills}</div>
<section class="card"><h2>핵심 이슈</h2><ol>{issues}</ol></section>
{legend}
{groups}
{foot}
</div>
</body>
</html>
"""


def main(argv):
    if len(argv) >= 2 and argv[1] == "--css":
        print(build_css())
        return 0
    if len(argv) < 3:
        print(__doc__)
        return 2
    theme = argv[argv.index("--theme") + 1] if "--theme" in argv else None
    with open(argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    out = render(data, theme)
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(out)
    print(f"wrote {argv[2]} ({len(out.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
