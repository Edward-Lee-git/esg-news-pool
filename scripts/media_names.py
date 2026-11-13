# -*- coding: utf-8 -*-
"""언론사 도메인 -> 한글 매체명 변환 (표준 라이브러리만 필요).

수집기(scripts/)와 보관 도구(tools/)가 같은 파일을 쓴다. 표는 media_names.json 에 있다.
  - 같은 폴더의 media_names.json 을 먼저 찾고, 없으면 ../config/media_names.json 을 쓴다.
  - 표를 못 읽어도 오류를 내지 않는다(변환 없이 원래 값을 그대로 돌려준다) — 수집이 이 파일 때문에 멈추면 안 된다.
규칙(루틴 프롬프트의 규칙과 같다): 도메인이 정확히 일치하는 항목을 먼저 찾고, 없으면 앞의 www.·m.·view.·biz.·news. 접두어를 뗀 도메인으로 한 번 더 찾는다.
표에 없는 도메인, 이미 한글인 이름, 도메인이 아닌 값(예: Reuters)은 그대로 돌려준다. 이름을 추측하지 않는다.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
_PREFIXES = ("www.", "m.", "view.", "biz.", "news.")
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+$")
_TABLE = None


def table():
    global _TABLE
    if _TABLE is None:
        _TABLE = {}
        for p in (os.path.join(HERE, "media_names.json"), os.path.join(HERE, "..", "config", "media_names.json")):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                _TABLE = {k.strip().lower(): v.strip() for k, v in d.items()
                          if not k.startswith("_") and isinstance(v, str) and v.strip()}
                break
            except Exception:  # noqa
                continue
    return _TABLE


def is_domain(s):
    return bool(_DOMAIN.match((s or "").strip()))


def media_name(m):
    s = (m or "").strip()
    if not is_domain(s):
        return m
    t = table()
    if s in t:
        return t[s]
    for pf in _PREFIXES:
        if s.startswith(pf) and s[len(pf):] in t:
            return t[s[len(pf):]]
    return m
