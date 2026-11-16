# -*- coding: utf-8 -*-
"""
임팩트온 유료(구독자 전용) 기사 제외
====================================
유료 구독자만 읽을 수 있는 임팩트온 기사는 수집 목록에서 뺀다. 같은 폴더의 collect.py 가 부르며,
이 파일이 없거나 깨져 있어도 수집은 그대로 진행된다(제외만 하지 않는다).

판정 방법: 기사 페이지를 로그인 없이 열어, 본문 자리에 「유료 기사 전문은 회원가입+유료회원에게만 제공됩니다」가
들어 있으면 유료로 본다(분류·제목·주소로는 구분되지 않는다. 2026-11-16 확인).
기사 페이지를 여는 것은 body_check.py 의 fetch() 를 그대로 쓴다(내부 주소 차단·크기·시간 제한이 이미 들어 있다).

안전장치(모두 「그대로 통과」 쪽으로 실패한다)
  · 접속이 안 되면(성공 없이 연속 5건 실패) 중단하고 아무것도 제외하지 않는다.
  · 확인한 기사 8건 이상에서 유료 비율이 60%를 넘으면 규칙이 잘못 걸린 것으로 보고 아무것도 제외하지 않는다(하루 유료는 보통 3건 안팎).
  · 페이지 글자가 800자 미만이면(차단·안내 페이지일 수 있음) 판정하지 않는다.
  · 확인은 한 번에 최대 120건, 전체 180초. 넘으면 남은 기사는 확인 없이 통과한다.
  · 어떤 오류가 나도 수집은 계속하고, 그 사실을 pool/paywall.txt 에 적는다.
같은 기사가 구글 중계 주소로만 들어온 경우(원문 주소를 몰라 직접 확인 불가)는, 같은 제목이 유료로 확인되었을 때만 함께 뺀다.

동작 모드: 환경변수 PAYWALL_FILTER 또는 config/paywall_filter.json 의 {"mode": ...}
  on(기본) 제외한다 | log 판정만 기록하고 제외하지 않는다 | off 끈다
결과 기록: pool/paywall.txt (제외한 기사 목록 포함 — 루틴은 읽지 않고, /check 가 읽는다)
다른 매체를 더하려면 RULES 에 한 줄을 넣는다.
"""
import os
import re
import json
import time
import html
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import urllib.parse

from body_check import fetch    # (html 또는 None, 사유)

WORKERS = 4
MAX_CHECKS = 120
TOTAL_BUDGET = 180
ABORT_FAILS = 5
MIN_TEXT = 800
SUSPICIOUS_MIN = 8
SUSPICIOUS_RATIO = 0.6
MODES = ("on", "log", "off")

# 매체별 규칙: 도메인(끝이 일치) · 구글 뉴스에서 쓰는 매체 이름 · 유료 기사에만 나오는 문구(태그가 단어 사이에 끼어도 되도록 공백을 느슨하게)
RULES = [
    {"name": "임팩트온", "domain": "impacton.net",
     "media": ("임팩트온", "IMPACT ON(임팩트온)", "impacton.net"),
     "paid": re.compile(r"유료[\s*]*기사[\s*]*전문은.{0,40}?유료[\s*]*회원[\s*]*에게만")},
]


def rules():
    """시험용: 환경변수 PAYWALL_TEST_DOMAINS(쉼표 구분)의 도메인도 첫 규칙으로 검사한다."""
    out = list(RULES)
    for d in filter(None, (os.environ.get("PAYWALL_TEST_DOMAINS", "")).split(",")):
        out.append({"name": "시험", "domain": d.strip(), "media": RULES[0]["media"], "paid": RULES[0]["paid"]})
    return out


def _host(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:  # noqa
        return ""


def rule_for_url(url):
    h = _host(url)
    for r in rules():
        if h == r["domain"] or h.endswith("." + r["domain"]):
            return r
    return None


def rule_for_media(media):
    m = (media or "").strip()
    for r in rules():
        if m in r["media"]:
            return r
    return None


def page_text(raw):
    """script·style·주석·태그를 걷어 내고 눈에 보이는 글자만 남긴다(코드 안의 문구를 잘못 찾지 않도록)."""
    h = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", raw or "")
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def _tk(title):
    """제목 비교용 열쇠(공백·기호 제거, 앞 24자). 구글 뉴스는 제목 뒤에 매체명을 붙이므로 앞부분만 본다."""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", title or "")[:24]


def load_mode(root):
    m = os.environ.get("PAYWALL_FILTER", "").strip().lower()
    if m in MODES:
        return m
    try:
        with open(os.path.join(root, "config", "paywall_filter.json"), encoding="utf-8") as f:
            m = str(json.load(f).get("mode", "on")).strip().lower()
    except Exception:  # noqa
        m = "on"
    return m if m in MODES else "on"


class PaywallFilter:
    def __init__(self, root=".", mode=None, log=print):
        self.mode = mode or load_mode(root)
        self.log = log
        self.t0 = time.time()
        self.memo = {}              # 주소 -> True(유료) / False(무료) / None(판정 못 함)
        self.paid_titles = {}       # 제목 열쇠 -> 제목
        self.excluded = []          # (세트, 제목, 주소)
        self.paid = self.free = self.failed = self.unknown = self.skipped = 0
        self.streak = 0
        self.reasons = Counter()
        self.disabled = ""
        self.suspicious = ""

    # ---------------------------------------------------------- 확인
    def _one(self, url):
        try:
            page, why = fetch(url)
        except Exception:  # noqa
            return None, "error"
        if page is None:
            return None, why
        txt = page_text(page)
        r = rule_for_url(url)
        if r and r["paid"].search(txt):
            return True, "paid"
        if len(txt) < MIN_TEXT:
            return None, "short"
        return False, "free"

    def _record(self, url, v, why):
        self.memo[url] = v
        self.reasons[why] += 1
        if v is True:
            self.paid += 1
            self.streak = 0
        elif v is False:
            self.free += 1
            self.streak = 0
        elif why == "short":
            self.unknown += 1
        else:
            self.failed += 1
            self.streak += 1

    def _batch(self, urls):
        todo = [u for u in dict.fromkeys(urls) if u not in self.memo]
        if not todo or self.disabled:
            return
        room = MAX_CHECKS - len(self.memo)
        if room <= 0:
            self.skipped += len(todo)
            return
        if len(todo) > room:
            self.skipped += len(todo) - room
            todo = todo[:room]
        i = 0
        while i < len(todo):
            if time.time() - self.t0 > TOTAL_BUDGET:
                self.skipped += len(todo) - i
                self.reasons["시간 초과"] += 1
                break
            chunk = todo[i:i + WORKERS]
            i += WORKERS
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                res = list(ex.map(self._one, chunk))
            for u, (v, why) in zip(chunk, res):
                self._record(u, v, why)
            if self.paid + self.free == 0 and self.streak >= ABORT_FAILS:
                self.disabled = f"기사 사이트 접속 실패가 연속 {self.streak}건이라 중단했습니다({', '.join(f'{k} {c}' for k, c in self.reasons.most_common(2))})"
                self.skipped += len(todo) - i
                self.log(f"      [paywall] {self.disabled} — 제외 없이 계속")
                break

    def _urls(self, items):
        return [a.get("url") for a in items if a.get("url") and rule_for_url(a.get("url"))]

    def _titles(self, items):
        for a in items:
            if self.memo.get(a.get("url")) is True:
                self.paid_titles[_tk(a.get("title"))] = a.get("title", "")

    def _guard(self):
        n = self.paid + self.free
        if not self.suspicious and n >= SUSPICIOUS_MIN and self.paid / n > SUSPICIOUS_RATIO:
            self.suspicious = f"확인 {n}건 중 유료 {self.paid}건({self.paid * 100 // n}%)으로 비정상적으로 높아 규칙 오작동으로 보고 제외하지 않았습니다"
            self.log(f"      [paywall] {self.suspicious}")

    # ---------------------------------------------------------- 사용
    def precheck(self, items):
        """직접 RSS·훑기로 들어온 임팩트온 기사를 먼저 확인해 둔다(제목 목록 확보 + 이상 여부 점검)."""
        if self.mode == "off":
            return
        try:
            self._batch(self._urls(items))
            self._titles(items)
            self._guard()
        except Exception as e:  # noqa
            self.disabled = self.disabled or f"확인 준비 중 오류({type(e).__name__})로 중단했습니다"
            self.log(f"      [paywall] {self.disabled} — 제외 없이 계속")

    def _paid_title(self, title):
        """같은 제목이 유료로 확인되었는가. 한쪽이 다른 쪽의 앞부분이면(구글이 붙인 꼬리표) 같은 기사로 본다. 너무 짧은 열쇠는 오탐 방지를 위해 제외."""
        k = _tk(title)
        if len(k) < 10:
            return False
        for p in self.paid_titles:
            if len(p) >= 10 and (k == p or k.startswith(p) or p.startswith(k)):
                return True
        return False

    def _is_paid(self, a):
        u = a.get("url") or ""
        if rule_for_url(u):
            return self.memo.get(u) is True
        if rule_for_media(a.get("media")) and self._paid_title(a.get("title")):
            return True            # 구글 중계 주소로만 들어온 같은 기사
        return False

    def drop(self, items, label=""):
        if self.mode == "off":
            return items
        try:
            if not self.disabled:
                self._batch(self._urls(items))
                self._titles(items)
                self._guard()
            out, n = [], 0
            for a in items:
                if self._is_paid(a):
                    n += 1
                    self.excluded.append((label, a.get("title", ""), a.get("url", "") if rule_for_url(a.get("url", "")) else "(구글 중계 주소)"))
                    if self.mode == "on" and not self.disabled and not self.suspicious:
                        continue
                out.append(a)
            if n:
                verb = "제외" if (self.mode == "on" and not self.disabled and not self.suspicious) else "발견(제외하지 않음)"
                self.log(f"      임팩트온 유료 기사 {verb}: {n}건")
            return out
        except Exception as e:  # noqa
            self.disabled = self.disabled or f"오류({type(e).__name__})로 중단했습니다"
            self.log(f"      [paywall] {self.disabled} — 제외 없이 계속")
            return items

    # ---------------------------------------------------------- 기록
    def status(self):
        if self.mode == "off":
            return "off"
        if self.disabled:
            return "disabled: " + self.disabled
        if self.suspicious:
            return "suspicious: " + self.suspicious
        return "ok"

    def summary_line(self):
        if self.mode == "off":
            return "임팩트온 유료 제외: 꺼짐"
        acted = self.mode == "on" and not self.disabled and not self.suspicious
        uniq = len({(t, u) for _, t, u in self.excluded})
        return (f"임팩트온 유료 제외({self.mode}): 확인 {self.paid + self.free + self.unknown + self.failed}건 "
                f"(유료 {self.paid} · 무료 {self.free} · 판정불가 {self.unknown + self.failed}) → "
                f"{'제외' if acted else '발견'} {uniq}건 · 상태 {self.status().split(':')[0]}")

    def write_report(self, outdir, end):
        acted = self.mode == "on" and not self.disabled and not self.suspicious    # 실제로 목록에서 뺐는가
        uniq = {}
        for lbl, t, u in self.excluded:
            uniq.setdefault((t, u), set()).add(lbl.replace("set_", ""))
        by_set = Counter(lbl.replace("set_", "") for lbl, _, _ in self.excluded) if acted else Counter()
        if acted:
            ex_line = (f"excluded: {len(uniq)}건 (세트별 a {by_set.get('a', 0)} · b {by_set.get('b', 0)} · c {by_set.get('c', 0)}"
                       " — 같은 기사가 여러 세트에 있으면 겹쳐 센다)")
            head = "# 제외한 기사(매체 | 제목 | 주소 | 세트)"
        else:
            ex_line = f"excluded: 0건 (유료로 확인했지만 제외하지 않은 기사 {len(uniq)}건 — 모드 {self.mode} · 상태 {self.status().split(':')[0]})"
            head = ("# 유료로 확인된 기사(모드가 log 라 제외하지는 않음)" if self.mode == "log"
                    else "# 유료로 확인되었으나 제외하지 않은 기사(매체 | 제목 | 주소 | 세트)")
        L = [
            "# 임팩트온 유료(구독자 전용) 기사 제외 결과 — 수집기가 자동으로 만든다(루틴은 읽지 않고 /check 가 읽는다)",
            f"collected: {end:%Y-%m-%d %H:%M} KST",
            f"mode: {self.mode}",
            f"status: {self.status()}",
            f"checked: {self.paid + self.free + self.unknown + self.failed} (유료 {self.paid} · 무료 {self.free} · 판정불가 {self.unknown + self.failed}"
            f"{' · 시간/상한으로 확인 못 함 ' + str(self.skipped) if self.skipped else ''})",
            ex_line,
            f"reasons: {', '.join(f'{k} {c}' for k, c in self.reasons.most_common()) or '-'}",
            "",
            head,
        ]
        for (t, u), sets in uniq.items():
            L.append(f"임팩트온 | {t.replace('|', '/')} | {u} | {''.join(sorted(sets))}")
        with open(os.path.join(outdir, "paywall.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
