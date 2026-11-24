# -*- coding: utf-8 -*-
"""실행 로그를 pool/run_log.txt 로 남긴다 (pool_stats.py 맨 위에서 import 한다. collect.py 는 실행 전에 pool_stats 를 불러오므로 collect.py 를 고치지 않아도 된다).

GitHub 는 Actions 로그 본문을 로그인 없이는 보여 주지 않는다. 점검하는 쪽(Code·Claude)이 같은 내용을 읽을 수 있도록,
화면에 나오는 로그를 그대로 복사해 두었다가 프로그램이 끝날 때(오류로 끝나도) pool/run_log.txt 한 파일로 쓴다.
워크플로가 pool 폴더 전체를 저장하므로 다른 설정은 필요 없다.

- 기사 본문 조각이 든 「요약 근거」 줄은 뺀다. 비밀값은 출력하지 않는 코드이고, 환경변수는 실행 종류(GITHUB_EVENT_NAME·ADVANCE_STATE)만 적는다.
- 파일 맨 위 한 줄에 시작·종료·소요 시간(KST)과 실행 종류를 적는다.
- 백업 실행처럼 「[SKIP]」으로 끝난 실행은 아무 파일도 쓰지 않는다. run_log.txt 를 덮어쓰면 07:30 정기 수집 로그가 지워지고, 다른 파일을 쓰면 07:55 에 불필요한 저장 커밋이 생겨 「마지막 저장 시각」이 07:55 로 보이기 때문이다(예전처럼 SKIP 실행은 커밋을 만들지 않는다).
- 어떤 오류도 수집을 막지 않는다. 끄려면 환경변수 RUN_LOG=off.
"""
import atexit
import datetime as dt
import os
import sys
import time

MAX_BYTES = 140000
_KST = dt.timezone(dt.timedelta(hours=9))
_lines = []
_T0 = time.time()
_START = dt.datetime.now(_KST)
_done = False


class _Tee:
    def __init__(self, stream):
        self._s = stream
        self._buf = ""

    def write(self, text):
        try:
            return self._s.write(text)
        finally:
            try:
                self._buf += text
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    _lines.append(line)
            except Exception:  # noqa
                pass

    def flush(self):
        try:
            self._s.flush()
        except Exception:  # noqa
            pass

    def rest(self):
        if self._buf:
            _lines.append(self._buf)
            self._buf = ""

    def __getattr__(self, name):
        return getattr(self._s, name)


def _write():
    global _done
    if _done:
        return
    _done = True
    try:
        for s in (sys.stdout, sys.stderr):
            if isinstance(s, _Tee):
                s.rest()
        end = dt.datetime.now(_KST)
        head = (f"# run_log | 시작 {_START:%Y-%m-%d %H:%M} KST | 종료 {end:%H:%M} KST | 소요 {int(time.time() - _T0)}초 "
                f"| event={os.environ.get('GITHUB_EVENT_NAME', '-')} | advance={os.environ.get('ADVANCE_STATE', '-') or '-'}")
        body = [ln for ln in _lines if "요약 근거" not in ln]
        text = head + "\n" + "\n".join(body) + "\n"
        data = text.encode("utf-8")
        if len(data) > MAX_BYTES:                       # 너무 길면 앞 60%·뒤 40% 만 남긴다
            cut = int(MAX_BYTES * 0.6)
            tail = MAX_BYTES - cut
            data = data[:cut].decode("utf-8", "ignore").encode("utf-8") + "\n...(중간 생략)...\n".encode("utf-8") + data[-tail:].decode("utf-8", "ignore").encode("utf-8")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = os.path.join(root, "pool")
        os.makedirs(out, exist_ok=True)
        if any(ln.lstrip().startswith("[SKIP]") for ln in body):    # 건너뛴 실행: 아무것도 쓰지 않는다(정기 로그 보존, 불필요한 커밋 방지)
            return
        tmp = os.path.join(out, "run_log.txt.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(out, "run_log.txt"))
    except Exception:  # noqa — 로그 저장 실패는 수집에 영향을 주지 않는다
        pass


if os.environ.get("RUN_LOG", "on").lower() != "off":
    try:
        sys.stdout = _Tee(sys.stdout)
        sys.stderr = _Tee(sys.stderr)
        atexit.register(_write)
    except Exception:  # noqa
        pass
