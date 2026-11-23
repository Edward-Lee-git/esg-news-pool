# -*- coding: utf-8 -*-
"""robots.txt 확인(title_restore.py·body_check.py 가 함께 쓴다) — 사이트가 자동 접속을 허용하지 않는 주소는 읽지 않는다.

- 호스트마다 robots.txt 를 한 번만 읽어 이번 실행 동안 기억한다(동시에 여러 요청이 와도 한 번).
- 우리 수집기 이름은 esg-news-pool 이다(요청 헤더의 User-Agent 도 같은 이름). 「User-agent: *」가 막으면 우리도 막힌 것으로 본다.
- robots.txt 가 없거나(404 등) 읽지 못하면 제한 없음으로 본다.
- 이 파일이 없어도 title_restore.py·body_check.py 는 예전처럼 동작한다(확인만 하지 않음).
"""
import threading
import urllib.parse
import urllib.robotparser

import requests

UA_NAME = "esg-news-pool"
_cache = {}
_locks = {}
_glock = threading.Lock()


def _load(base, headers):
    try:
        r = requests.get(base + "/robots.txt", headers=headers or {}, timeout=(4, 5), allow_redirects=True)
        if r.status_code != 200:
            return None
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp
    except Exception:  # noqa — 읽지 못하면 제한 없음
        return None


def allowed(url, headers=None):
    """그 주소를 읽어도 되는지. 어떤 오류도 밖으로 던지지 않는다(확인하지 못하면 True)."""
    try:
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return True
        host = p.netloc.lower()
        with _glock:
            lk = _locks.setdefault(host, threading.Lock())
        with lk:
            if host not in _cache:
                _cache[host] = _load(f"{p.scheme}://{host}", headers)
        rp = _cache[host]
        return True if rp is None else bool(rp.can_fetch(UA_NAME, url))
    except Exception:  # noqa
        return True
