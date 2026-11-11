"""Check a built bundle the way a teacher's PC would meet it.

    py packaging/smoke.py dist/claude-hands

The bundle folder is copied into an empty folder and started there with PATH cut down
to Windows' own folders, every PYTHON* variable removed, and APPDATA /
LOCALAPPDATA / TEMP pointed at that folder — so it cannot lean on an installed
Python, on site-packages, or on a comtypes cache this PC built earlier.

Then, over stdio as Claude would: MCP initialize, the 16 tools listed, and real
work through UI Automation against ``examples/list_target.py`` (started far off
screen): attach, find an item, choose it. The foreground must not move to the
target, and the target must report the selection. Finally ``doctor`` and
a plain script importing claude_hands (as the 기안 skill's fill_draft.py does)
are run with the same bundled python.exe. Nothing may be written to APPDATA or
into the bundle: on a teacher's PC the folder may not be writable.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "list_windows", "attach_window", "detach_window", "snapshot", "find_elements", "read_text",
    "screenshot_window", "click_element", "type_text", "press_keys", "scroll", "set_element_state",
    "call_control_method", "menu_select", "wait_for_element", "control_window",
}
user32 = ctypes.windll.user32


def clean_env(home: Path) -> dict:
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "VIRTUAL_ENV", "CONDA"))}
    env.update(
        PATH=os.pathsep.join([os.path.join(windir, "System32"), windir,
                              os.path.join(windir, "System32", "WindowsPowerShell", "v1.0")]),
        APPDATA=str(home / "appdata"), LOCALAPPDATA=str(home / "localappdata"),
        TEMP=str(home / "temp"), TMP=str(home / "temp"),
    )
    for key in ("APPDATA", "LOCALAPPDATA", "TEMP"):
        Path(env[key]).mkdir(exist_ok=True)
    return env


class Server:
    def __init__(self, command: list, env: dict, cwd: Path):
        # stderr to a file: the server logs every request there, and a full pipe
        # nobody reads would stall it.
        self.errors = open(cwd / "server-stderr.txt", "wb")
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errors, env=env, cwd=cwd)
        self.next_id = 0

    def send(self, method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if notify:
            return None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                self.errors.flush()
                tail = Path(self.errors.name).read_bytes().decode("utf-8", "replace")[-2000:]
                raise RuntimeError("서버가 끝났습니다: " + tail)
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply

    def call(self, name, **arguments):
        reply = self.send("tools/call", {"name": name, "arguments": arguments})
        result = reply["result"]
        text = "".join(c.get("text", "") for c in result["content"])
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text}")
        return text

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.errors.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    bundle_src = Path(sys.argv[1]).resolve()
    results: list[tuple[bool, str]] = []

    def check(label, ok, detail=""):
        results.append((bool(ok), label))
        print(("통과" if ok else "실패"), label, detail)

    with tempfile.TemporaryDirectory(prefix="hands-smoke-") as tmp:
        home = Path(tmp)
        bundle = home / bundle_src.name
        shutil.copytree(bundle_src, bundle)
        python = str(bundle / "python" / "python.exe")
        env = clean_env(home)

        started = time.monotonic()
        server = Server([python, "-m", "claude_hands.server"], env, home)
        try:
            init = server.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "smoke", "version": "1"}})
            check("initialize", init["result"]["serverInfo"]["name"] == "claude-hands",
                  f"({time.monotonic() - started:.1f}초)")
            server.send("notifications/initialized", notify=True)
            names = {t["name"] for t in server.send("tools/list")["result"]["tools"]}
            check("도구 16개", names == TOOLS, f"{len(names)}개")
            check("창 목록", "hwnd" in server.call("list_windows").lower())

            log = home / "target.log"
            target = subprocess.Popen([sys.executable, str(ROOT / "examples" / "list_target.py"), str(log)],
                                      stdout=subprocess.PIPE, text=True)
            hwnd = int(target.stdout.readline())
            time.sleep(0.5)
            try:
                fg0 = user32.GetForegroundWindow()
                attached = server.call("attach_window", hwnd=hwnd, engine="uia")
                check("UIA 로 연결", "uia" in attached.lower(), attached.splitlines()[0][:80])
                found = server.call("find_elements", query="배", role="listitem")
                ref = re.search(r"\b(e\d+)\b", found)
                check("요소 찾기", ref, found.splitlines()[0][:80] if found else "")
                done = server.call("click_element", ref=ref.group(1)) if ref else ""
                time.sleep(0.6)
                check("목록 고르기", "LB_SETCURSEL" in done, done.splitlines()[0][:80] if done else "")
                check("대상이 알림 받음", "WM_COMMAND id=101 code=1" in log.read_text(encoding="utf-8"))
                fg1 = user32.GetForegroundWindow()
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(fg1, ctypes.byref(pid))
                check("전경을 대상에 넘기지 않음", pid.value != target.pid,
                      "(전경 그대로)" if fg1 == fg0 else "(사람이 다른 창으로 옮김)")
            finally:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)
                target.wait(5)
        finally:
            server.close()

        doctor = subprocess.run([python, "-m", "claude_hands.cli", "doctor"], capture_output=True, env=env,
                                cwd=home, timeout=120)
        out = doctor.stdout.decode("utf-8", "replace")
        check("doctor", doctor.returncode == 0 and "초기화 성공" in out)

        # As the 기안 skill's fill_draft.py does: a plain script that imports claude_hands.
        script = home / "uses_hands.py"
        script.write_text("import sys, claude_hands\nsys.stdout.reconfigure(encoding='utf-8')\n"
                          "print('hands', claude_hands.__version__, sys.argv[1])\n", encoding="utf-8")
        ran = subprocess.run([python, str(script), "인자"], capture_output=True, env=env, cwd=home, timeout=120)
        said = ran.stdout.decode("utf-8", "replace").strip()
        check("claude_hands 를 쓰는 스크립트", ran.returncode == 0 and "인자" in said,
              said or ran.stderr.decode("utf-8", "replace")[-300:])

        written = [str(p.relative_to(home)) for p in home.rglob("*")
                   if p.is_file() and p.is_relative_to(home / "appdata")]
        check("APPDATA 에 아무것도 안 씀", not written, str(written[:5]))
        added = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*")
                       if p.is_file() and not (bundle_src / p.relative_to(bundle)).exists())
        check("묶음 폴더에 새로 쓴 파일 없음", not added, str(added[:5]))

    size = sum(f.stat().st_size for f in bundle_src.rglob("*") if f.is_file())
    print(f"\n{sum(ok for ok, _ in results)}/{len(results)} 통과, 크기 {size / 2**20:.1f} MB")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
