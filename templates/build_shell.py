# -*- coding: utf-8 -*-
"""shell_head.html / shell_tail.html 생성기. 머리 파일의 __TITLE__ 은 조립할 때 리포트 제목으로 바꾼다(아티팩트 이름은 발행 시점의 <title>로 정해지기 때문). CSS 는 render_report.py 와 같은 것을 쓴다(양식이 한 곳에서만 정의되도록)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_report as R

HEAD = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="__FONT__">
<style>__CSS__</style>
<style>.warn{margin:16px 0 0;padding:10px 14px;border:1px solid var(--a-dot);border-radius:8px;background:var(--a-strip);color:var(--a-title);font-size:13px;line-height:20px}.warn b{font-weight:700}</style>
</head>
<body>
<div id="app"></div>
<noscript><div class="wrap"><p>이 리포트는 스크립트가 켜져 있어야 보입니다.</p></div></noscript>
<script type="application/json" id="report-data">"""

TAIL = r"""</script>
<script>
(function () {
  'use strict';
  var CATS = ['e', 's', 'g', 'n', 'p', 'a'];
  var warns = [];
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null && text !== '') e.textContent = String(text);
    return e;
  }
  function put(parent) {
    for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]);
    return parent;
  }
  function safeUrl(u) {
    if (u === undefined || u === null || u === '') return null;
    try {
      var s = String(u).trim(), p = new URL(s);
      if (p.protocol === 'http:' || p.protocol === 'https:') return s;
    } catch (e) {}
    warns.push('링크 형식 오류(링크 없음으로 표시함): ' + u);
    return null;
  }
  function chip(c) {
    var k = c.kind || 'sub';
    if (['sub', 'biz', 'alert'].indexOf(k) < 0) { warns.push('chip kind 오류(sub로 표시함): ' + k); k = 'sub'; }
    return el('span', 'chip' + (k === 'sub' ? '' : ' ' + k), c.text);
  }
  function row(r, withMark, markLabel) {
    var url = safeUrl(r.url), tr = el('div', 'tr');
    if (withMark) {
      var m = el('div', 'c-mark');
      if (r.mark) { var i = el('i', 'mk'); i.title = markLabel; m.appendChild(i); }
      tr.appendChild(m);
    }
    var main = el('div', 'c-main'), t;
    if (url) { t = el('a', 'title', r.title); t.href = url; t.target = '_blank'; t.rel = 'noopener noreferrer'; }
    else { t = el('span', 'title nolink', r.title); }
    put(main, t, r.summary ? el('div', 'sum', r.summary) : null);
    var tags = el('div', 'c-tags');
    (r.tags || []).forEach(function (c) { tags.appendChild(chip(c)); });
    if (r.needs_check) tags.appendChild(el('span', 'tag-dash check', '확인 필요'));
    if (!url) tags.appendChild(el('span', 'tag-dash nolink', '링크 없음'));   // 항상 맨 뒤
    return put(tr, main, el('div', 'c-src', r.source), el('div', 'c-date', r.date), tags);
  }
  function group(g, markLabel) {
    var cat = g.cat || 'n';
    if (CATS.indexOf(cat) < 0) { warns.push('cat 오류(n으로 표시함): ' + cat); cat = 'n'; }
    var rows = (g.rows || []).slice(), cols = g.cols || 'plain';
    if (cols !== 'mark' && cols !== 'plain') { warns.push('cols 오류(plain으로 표시함): ' + cols); cols = 'plain'; }
    if (cols === 'mark') {   // 사업 관련 기사가 맨 위(안의 순서는 유지)
      rows = rows.filter(function (r) { return r.mark; }).concat(rows.filter(function (r) { return !r.mark; }));
    }
    var count = g.count || (rows.length ? rows.length + '건' : '');
    var d = el('details', 'group cat-' + cat);
    if (g.open !== false) d.open = true;
    var gl = el('span', 'gl');
    put(gl, el('span', 'gdot'), g.num ? el('span', 'gnum', g.num) : null, el('h2', 'gtitle', g.title));
    (g.head_chips || []).forEach(function (c) { gl.appendChild(chip(c)); });
    gl.appendChild(el('span', 'gcount', count));
    var sm = el('summary', 'gh'); sm.appendChild(gl); d.appendChild(sm);
    if (rows.length) {
      var th = el('div', 'th');
      if (cols === 'mark') th.appendChild(el('div', 'c-mark', '주요'));
      put(th, el('div', null, '제목 · 요약'), el('div', null, '매체'), el('div', null, '게재일'), el('div', null, '분류'));
      var inn = el('div', 'tbl-in cols-' + cols); inn.appendChild(th);
      rows.forEach(function (r) { inn.appendChild(row(r, cols === 'mark', markLabel)); });
      d.appendChild(put(el('div', 'tbl-scroll'), inn));
    } else if (g.note) {
      d.appendChild(el('div', 'note', g.note));
    }
    if (g.more && rows.length) {
      var mo = el('details', 'more'); mo.appendChild(el('summary', 'more-h', g.more.label)); mo.appendChild(el('p', null, g.more.text));
      d.appendChild(mo);
    } else if (g.more) {
      d.appendChild(el('div', 'note', g.more.text));
    }
    return d;
  }
  function nolinkCount(data) {
    var n = 0;
    (data.groups || []).forEach(function (g) { (g.rows || []).forEach(function (r) { if (!safeUrl(r.url)) n++; }); });
    return n;
  }
  function pill(p, data) {
    var value = p.value;
    if (p.auto === 'nolink') value = nolinkCount(data) + '건';
    var cat = p.cat, s = el('span', cat ? 'pill cat-' + cat : 'pill');
    if (cat) s.appendChild(el('span', 'pdot'));
    s.appendChild(document.createTextNode(p.label === undefined || p.label === null ? '' : String(p.label)));
    s.appendChild(el('strong', null, value));
    return s;
  }
  function render(data) {
    if (data.theme === 'light' || data.theme === 'dark') document.documentElement.setAttribute('data-theme', data.theme);
    if (data.title) document.title = data.title;
    var markLabel = data.mark_label || '사업 관련';
    var wrap = el('div', 'wrap');
    put(wrap, el('div', 'kicker', data.kicker), el('h1', null, data.title), el('div', 'meta', data.meta));
    var pills = el('div', 'pills');
    (data.pills || []).forEach(function (p) { pills.appendChild(pill(p, data)); });
    wrap.appendChild(pills);
    var card = el('section', 'card'); card.appendChild(el('h2', null, '핵심 이슈'));
    var ol = el('ol');
    (data.issues || []).forEach(function (x, i) {
      var li = el('li'), t = el('div', 't');
      t.appendChild(el('strong', null, x.title));
      if (x.body) { t.appendChild(document.createTextNode(' ')); t.appendChild(el('span', null, '— ' + x.body)); }
      put(li, el('span', 'n', i + 1), t); ol.appendChild(li);
    });
    card.appendChild(ol); wrap.appendChild(card);
    if (data.legend) { var lg = el('div', 'legend'); put(lg, el('span', 'ldot')); lg.appendChild(document.createTextNode(data.legend)); wrap.appendChild(lg); }
    (data.groups || []).forEach(function (g) { wrap.appendChild(group(g, markLabel)); });
    var f = data.footer || {};
    wrap.appendChild(put(el('footer', 'foot'), el('div', null, f.limit), el('div', 'verify', f.verify)));
    if (warns.length) {
      var w = el('div', 'warn'); w.appendChild(el('b', null, '자료 점검: '));
      w.appendChild(document.createTextNode(warns.join(' / ')));
      wrap.insertBefore(w, wrap.children[3]);
    }
    var app = document.getElementById('app'); app.textContent = ''; app.appendChild(wrap);
  }
  try {
    render(JSON.parse(document.getElementById('report-data').textContent));
  } catch (e) {
    var app = document.getElementById('app'), w = el('div', 'wrap'), m = el('div', 'warn');
    m.appendChild(el('b', null, '자료를 읽지 못했습니다: ')); m.appendChild(document.createTextNode(String(e && e.message || e)));
    app.textContent = ''; app.appendChild(put(w, m));
  }
})();
</script>
</body>
</html>
"""


def build(outdir):
    head = HEAD.replace("__FONT__", R.FONT_LINK).replace("__CSS__", R.build_css())
    open(os.path.join(outdir, "shell_head.html"), "w", encoding="utf-8").write(head)
    open(os.path.join(outdir, "shell_tail.html"), "w", encoding="utf-8").write(TAIL)
    return len(head.encode()), len(TAIL.encode())


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    h, t = build(out)
    print(f"shell_head.html {h}바이트, shell_tail.html {t}바이트")
