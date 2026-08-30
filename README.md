# claude-hands

**윈도우 프로그램을 창 하나 단위로 다루는 도구.** 데스크톱 전체를 보지 않고 지정한 창(HWND)에만 붙어서,
그 창이 **최소화되어 있거나 다른 창에 완전히 가려져 있어도** 읽고 조작합니다.
Claude in Chrome 이 탭 하나를 다루듯, 프로그램 하나를 다룹니다.

```
list_windows  →  attach_window  →  snapshot (ref 발급)  →  click / type / keys / scroll ...
```

---

## 왜 기존 방식으로는 안 되는가

| | 일반 Computer Use / Windows-Use | claude-hands |
|---|---|---|
| 보는 대상 | 데스크톱 전체 스크린샷 | 지정한 **창 하나**의 UI 트리 |
| 조작 방법 | 실제 마우스 커서 이동 + 실제 키 입력 | 대상 프로세스에 **직접 명령**(UIA 패턴) / 창 메시지 |
| 창이 가려지면 | 안 보이니 조작 불가 | 그대로 동작 |
| 창이 최소화되면 | 불가 | **그대로 동작** |
| 사용자가 동시에 다른 일 | 커서·포커스를 빼앗겨 불가능 | 가능 (커서·포커스 안 건드림) |
| 좌표가 밀리면 | 엉뚱한 곳 클릭 | 요소를 이름으로 지정하므로 무관 |

핵심은 **좌표에 클릭을 흉내내지 않는다**는 점입니다. `SendInput` 으로 진짜 마우스를 움직이는 대신,
Windows UI Automation 으로 "저 버튼의 Invoke 를 실행해라" 라고 애플리케이션에 직접 요청합니다.
앱 입장에서는 사용자가 누른 것과 똑같은 처리 경로를 타고, 화면에 보이는지 여부는 상관이 없습니다.

---

## 동작 원리 — 3단 전략

모든 동작은 배경 친화적인 순서대로 내려갑니다. 결과에 **어떤 방식이 쓰였는지** 항상 표시됩니다.

| 단계 | 방식 | 포커스 필요 | 최소화 상태 | 비고 |
|---|---|---|---|---|
| 1 | **UIA 패턴** — `Invoke` / `SetValue` / `Toggle` / `Select` / `Scroll` | 불필요 | 동작 | 기본 경로 |
| 2 | **창 메시지** — 해당 컨트롤 HWND 에 `PostMessage` | 불필요 | 대체로 동작 | 우클릭·더블클릭·휠 |
| 3 | **포커스 입력** — 포커스 후 `WM_CHAR` | 필요 | 창이 잠깐 뜰 수 있음 | 명시적으로 허용할 때만 |

읽기는 두 가지 방법이 있습니다.

* `snapshot` — UI Automation 트리. **최소화 상태에서도 정확**하고 빠르며, 요소 이름·값·상태를 그대로 줍니다. 기본 수단.
* `screenshot_window` — `PrintWindow(PW_RENDERFULLCONTENT)` 로 창에게 "직접 그려 달라" 고 요청합니다.
  다른 창에 가려져 있어도 **실제 내용**이 찍힙니다. 최소화된 창은 픽셀 자체가 없으므로,
  화면 **바깥 좌표로 잠깐 복원했다가 원래 상태로 되돌리는** 방식으로 캡처합니다(사용자 포커스는 그대로).

---

## 설치

```bash
git clone https://github.com/wonmukoh/Claude-hands
cd Claude-hands
pip install -e .
```

Windows 10/11 + Python 3.10 이상. 의존성은 `comtypes`(UIA), `pillow`(캡처), `mcp`(서버)뿐입니다.

설치 후 환경 점검:

```bash
claude-hands doctor
```

```
claude-hands 진단
  플랫폼          : win32
  DPI 인식        : per-monitor-v2
  comtypes        : 설치됨
  UI Automation   : 초기화 성공
  컨트롤 타입     : 40종 인식
  관리자 권한     : 아니오
  보이는 창       : 12개
```

---

## Claude 에 연결하기

**Claude Code**

```bash
claude mcp add claude-hands -- claude-hands-mcp
```

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "claude-hands": {
      "command": "claude-hands-mcp"
    }
  }
}
```

읽기만 허용하려면 `--read-only` 를 붙이거나 `CLAUDE_HANDS_READONLY=1` 을 설정하세요.
조작 도구는 막히고 창 목록·스냅샷·캡처만 남습니다.

### 도구 목록

| 도구 | 하는 일 |
|---|---|
| `list_windows` | 열린 창 목록 (최소화된 창 포함) |
| `attach_window` | 조작할 창 지정 (앞으로 꺼내지 않음) |
| `snapshot` | UI 트리를 읽고 각 요소에 `ref` 발급 |
| `find_elements` | 이름·ID·값으로 요소 검색 |
| `read_text` | 요소/창의 텍스트 읽기 |
| `screenshot_window` | 창(또는 요소) 픽셀 캡처 |
| `click_element` | 클릭 (좌/우/더블/수식키) |
| `type_text` | 입력란에 글자 넣기 |
| `press_keys` | 단축키 (`ctrl+s`, `alt+f4` …) |
| `scroll` | 스크롤 (단계 또는 퍼센트) |
| `set_element_state` | 선택·체크·펼치기·포커스·값 설정 |
| `menu_select` | `"파일 > 다른 이름으로 저장"` 처럼 메뉴 따라가기 |
| `wait_for_element` | 요소가 나타나거나 사라질 때까지 대기 |
| `control_window` | 최소화·복원·최대화·이동·닫기 |
| `detach_window` | 연결 해제 |

### 실제 대화 흐름

```
사용자: 최소화해 둔 메모장에 회의록 초안 좀 넣어줘

Claude: list_windows(process="notepad")
        → hwnd=853214 [minimized] notepad.exe (pid 9120) — 제목 없음 - 메모장

        attach_window(hwnd=853214)
        → 연결됨 (최소화 상태 유지)

        snapshot(interactive_only=True)
        → [e4] document "텍스트 편집기" (id=15)
          [e7] menuitem "파일"

        type_text(ref="e4", text="## 회의록\n1. ...")
        → type 완료 — document "텍스트 편집기" (방식: uia:value.SetValue)

        press_keys(keys="ctrl+s")
        → keys 완료 (방식: message:WM_KEYDOWN)
```

창은 끝까지 최소화된 채였고, 사용자의 마우스와 키보드는 한 번도 뺏기지 않았습니다.

---

## 명령줄에서 쓰기

```bash
# 창 목록
claude-hands windows --process chrome

# UI 트리 (ref 발급)
claude-hands snapshot --title 메모장 --interactive

# 이름으로 찾아 바로 클릭 (ref 몰라도 됨)
claude-hands click --title "다른 이름으로 저장" --query 저장

# 입력
claude-hands type --title 메모장 --query "텍스트 편집기" "안녕하세요"

# 단축키
claude-hands keys --title 메모장 "ctrl+s"

# 가려진 창 캡처
claude-hands shot --title 계산기 --out calc.png

# 메뉴 따라가기
claude-hands menu --title 메모장 "파일 > 다른 이름으로 저장"
```

## 파이썬에서 쓰기

```python
from claude_hands import attach, windows

for info in windows(process="excel"):
    print(info.describe())

app = attach(title="보고서.xlsx")

print(app.snapshot(interactive_only=True))   # 최소화 상태에서도 읽힘

app.type("e12", "2026년 1분기")              # UIA SetValue — IME 조합 문제 없음
app.click("e18")
app.keys("ctrl+s")
app.scroll(direction="down", amount=5)
app.toggle("e22", to=True)                   # 체크박스를 '켬' 상태로
app.menu("파일 > 인쇄")

print(app.text("e30"))                       # 텍스트 읽기
app.screenshot().to_png()                    # 가려져 있어도 실제 픽셀
```

`ref` 는 스냅샷이 발급하는 짧은 손잡이입니다. UI 가 바뀌어 `ref` 가 가리키던 요소가 이동하면,
런타임 ID → (역할 + 이름 + 자동화 ID) → (역할 + 이름) 순으로 **자동 재연결**합니다.
정말 사라졌을 때만 오류를 냅니다.

---

## 프로그램별 지원 범위

| 종류 | 예 | 읽기 | 조작 | 비고 |
|---|---|---|---|---|
| Win32 / WinForms | 메모장, 계산기, 대부분의 사내 프로그램 | ◎ | ◎ | 가장 잘 맞습니다 |
| WPF / WinUI | 설정, 최신 MS 앱 | ◎ | ◎ | UIA 네이티브 |
| UWP / 스토어 앱 | 계산기(신), 사진 | ○ | ○ | 최소화 시 앱이 정지될 수 있음 |
| Electron / Chromium | VS Code, Slack, Chrome | ○ | ○ | 접근성 트리가 켜져 있어야 함(대개 자동) |
| Java (Swing) | 구형 업무 프로그램 | △ | △ | Java Access Bridge 필요 |
| Qt | 여러 크로스플랫폼 앱 | ○ | ○ | Qt 접근성 플러그인 의존 |
| 게임 / 캔버스 렌더링 | Unity, 그림판 캔버스 | ✕ | △ | UIA 트리가 없어 좌표 클릭만 가능 |

`◎` 확실 · `○` 대체로 동작 · `△` 앱마다 다름 · `✕` 불가

---

## 한계와 주의점

* **관리자 권한.** 관리자 권한으로 실행 중인 프로그램은, 이 도구도 관리자 권한이어야 조작됩니다. (Windows 의 UIPI 규칙)
* **최소화된 UWP 앱.** 일부 스토어 앱은 최소화되면 프로세스가 정지(suspend)되어 반응이 느리거나 없습니다. `control_window(action="restore")` 로 활성화 없이 복원한 뒤 다루세요.
* **최소화 상태 캡처.** 픽셀이 없어 화면 밖에서 잠깐 복원합니다. 이 순간 작업 표시줄 표시가 잠깐 바뀔 수 있습니다. `restore_if_minimized=False` 로 끄면 `snapshot` 만 쓰게 됩니다.
* **`type_text` 의 마지막 폴백.** `Value` 패턴이 없는 컨트롤은 포커스를 잡고 한 글자씩 보냅니다. 이때만 창이 잠깐 앞으로 나올 수 있고, 결과 메시지에 그 사실이 표시됩니다.
* **좌표 클릭 폴백.** UIA 패턴이 없는 요소는 창 메시지로 좌표 클릭합니다. Chrome/Electron 처럼 자체 렌더링을 하는 앱은 이 경로가 무시될 수 있으니, 되도록 UIA 가 잡히는 요소를 고르세요.
* **DPI.** 프로세스 시작 시 per-monitor v2 로 설정합니다. 고DPI 다중 모니터에서도 좌표가 어긋나지 않습니다.

---

## 실기검증

실제 PowerPoint 창으로 이 도구의 핵심 주장을 확인하는 하네스를 넣어 뒀습니다.
PowerPoint 를 하나 열어 두고 실행하세요.

```bash
python examples/verify_pptx.py            # 읽기 전용 — 문서를 건드리지 않음
python examples/verify_pptx.py --write    # 입력까지 검증 (끝나면 ctrl+z 로 되돌림)
```

검증 항목:

1. 최소화된 PowerPoint 창의 UI 트리를 읽는가
2. 이름으로 리본 탭·슬라이드·개체틀을 찾는가
3. 가려지거나 최소화된 창의 실제 픽셀을 캡처하는가
4. 최소화 상태 그대로 슬라이드에 글자를 넣고 되읽는가 (`--write`)
5. **그동안 사용자의 마우스 커서와 전경 창을 한 번도 빼앗지 않는가**

5번이 이 도구의 존재 이유라, 매 단계마다 `GetCursorPos` 와 `GetForegroundWindow`
를 확인해서 어긋나면 그 단계를 실패로 기록합니다. 결과는 PASS/FAIL 표로 나옵니다.

```
[3] 최소화 상태에서 snapshot 읽기
  [PASS] 최소화 상태에서 UI 요소를 읽음
         조작 가능한 요소 24개, 0.4초 소요

  --- 읽어낸 요소 (앞 15줄) ---
    [e3] tabitem "홈" @0,60 60x30
    [e9] edit "제목 개체틀" value="" @300,200 600x120
...
====================================================================
결과: 11/11 통과
커서 이동     : 없음
전경 창 변경  : 없음
====================================================================
```

## 개발

```bash
pip install -e ".[dev]"
pytest -q
```

93개 테스트가 **Linux/macOS 에서도 전부 돌아갑니다.** COM/ctypes 경계만 대역으로
바꾸고 그 아래 로직은 실제로 실행하므로, 어떤 동작이 어떤 전략을 고르는지·패턴이
거절할 때 무엇으로 내려가는지·창에 무엇이 실제로 전달되는지까지 검증합니다
(`tests/test_action_chains.py`). 검증 하네스 자체도 가짜 PowerPoint 로 테스트합니다
(`tests/test_verify_harness.py`).

Windows COM 호출과 `PrintWindow` 실제 픽셀 경로는 이 테스트로 덮이지 않으므로,
`claude-hands doctor` 와 위 실기검증 스크립트로 확인하세요.

```
src/claude_hands/
  win32/defs.py      ctypes 바인딩 · DPI · LPARAM 패킹
  win32/windows.py   창 열거 · 비활성 상태 제어 · 자식 창 탐색
  win32/capture.py   PrintWindow 캡처 · 최소화 창 화면밖 복원
  win32/input.py     PostMessage 마우스/키 · 키 문자열 파서
  uia/core.py        COM(MTA) 워커 · 요소 래퍼 · 캐시 트리 구축
  elements.py        트리 모델 · 가지치기 · 렌더링 · 검색   (플랫폼 무관)
  session.py         창 세션 · ref 레지스트리 · 자동 재연결
  actions.py         클릭/입력/스크롤 … 3단 전략 체인
  api.py             파이썬 파사드
  server.py          MCP 서버 (도구 15개)
  cli.py             명령줄
```

## 라이선스

MIT
