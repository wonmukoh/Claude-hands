# claude-hands — 작업 맥락

이 파일은 이 저장소에서 작업을 이어받는 Claude 를 위한 것입니다. 클라우드 세션에서
여기까지 만들면서 **실제 실행으로만 드러난** 것들을 적어 둡니다. 다시 헤매지 마세요.

## 무엇을 만드는가

Windows 프로그램을 **창 하나 단위로** 조작하는 도구. 데스크톱 전체를 스크린샷 찍고
진짜 마우스를 움직이는 컴퓨터 유즈와 달리, 지정한 창(HWND)에만 붙어서 그 창이
**최소화되어 있거나 다른 창에 완전히 가려져 있어도** 읽고 조작합니다.

## 절대 어기면 안 되는 계약

이 셋이 무너지면 도구의 존재 이유가 사라집니다. 어떤 기능 추가도 이보다 우선하지
않습니다.

1. **사용자의 마우스 커서를 움직이지 않는다.** `SendInput` 금지. UIA 패턴 호출과
   `PostMessage` 만 씁니다.
2. **키보드 포커스와 전경 창을 빼앗지 않는다.** 창을 앞으로 꺼내지 않습니다.
   복원이 필요하면 `SW_SHOWNOACTIVATE`.
3. **사용자의 창 크기·위치·상태를 바꾸지 않는다.** 캡처를 위해 잠시 건드렸다면
   정확히 되돌립니다.

`examples/verify_live.py` 는 매 동작 **직전에** 커서와 전경 창을 다시 읽어서, 그
동작 중에 바뀌었으면 실패로 기록합니다. 이 검사를 약화시키지 마세요.

## 구조

```
src/claude_hands/
  win32/defs.py      ctypes 바인딩 · DPI · LPARAM 패킹 · 콘솔 인코딩
  win32/windows.py   창 열거 · 비활성 상태 제어 · 자식 창 히트테스트
  win32/capture.py   PrintWindow 캡처 · 최소화 창 복원 후 캡처
  win32/input.py     PostMessage 마우스/키 · 키 문자열 파서
  win32/controls.py  창 메시지 폴백 엔진 (HWND 컨트롤 → 패턴 프로토콜)
  win32/menus.py     실제 메뉴바 읽기 · WM_COMMAND 실행
  uia/core.py        COM 워커 스레드 · 요소 래퍼 · 캐시 트리 구축
  elements.py        트리 모델 · 가지치기 · 렌더링 · 검색   (플랫폼 무관)
  session.py         창 세션 · ref 레지스트리 · 자동 재연결 · 엔진 선택
  actions.py         클릭/입력/스크롤 … 전략 체인
  api.py             파이썬 파사드
  server.py          MCP 서버 (도구 15개)
  cli.py             명령줄
```

**엔진이 둘입니다.** `uia`(기본, 풍부함)와 `win32`(창 메시지만, COM 불필요).
`actions.py` 의 전략 체인 하나가 둘 다 굴립니다 — `win32/controls.py` 가 UIA 와
같은 패턴 프로토콜(`invoke`/`value`/`toggle`)을 창 메시지로 구현하기 때문입니다.
결과에는 실제로 어느 엔진이 처리했는지 표시됩니다(`win32:invoke` 처럼).

## 검증하는 법

```bash
pytest -q                                    # 134개, 어느 OS에서나
python examples/verify_live.py --process notepad --engine win32
python examples/verify_live.py --process POWERPNT --engine uia
```

테스트는 COM/ctypes 경계만 대역으로 바꾸고 그 아래 로직은 실제로 실행합니다.
`tests/test_action_chains.py` 는 어떤 동작이 어떤 전략을 고르는지, 패턴이 거절할 때
무엇으로 내려가는지, 창에 정확히 무엇이 전달되는지까지 검사합니다. 이 파일에
변이를 넣어(부호 뒤집기 등) 테스트가 잡는지 확인해 보면 신뢰도를 알 수 있습니다.

## 비싸게 배운 것들 — 반복하지 마세요

### 1. `SendMessage` 반환값으로 클릭 성공을 판정하면 안 됩니다

실측 결과 **효과와 반대**였습니다:

| 경우 | 반환값 | 실제 |
|---|---|---|
| 대화상자 열림 | 0 (ERROR_TIMEOUT) | 동작함 |
| 창 닫힘 | 0 (ERROR_ACCESS_DENIED) | 동작함 |
| 아무 일 없음 | 1 (성공) | 미동작 |

앱이 모달 루프에 들어가거나 창이 사라지면 타임아웃이 납니다. 즉 **성공했기 때문에**
실패로 보입니다. `_send()` 가 반환값을 무시하는 것은 의도된 것입니다. "고치지"
마세요.

클릭 결과는 "명령을 전달했다"는 뜻이지 "앱이 원하는 일을 했다"는 뜻이 아닙니다.
중요한 동작 뒤에는 `snapshot` 이나 `wait_for` 로 확인하세요.

### 2. DPI 인식은 반드시 임포트 시점에

`claude_hands/__init__.py` 에서 `enable_dpi_awareness()` 를 부릅니다. 늦게 켜면
그 전에 읽은 좌표가 전부 다른 좌표계입니다 — 200% 배율에서 정확히 절반. 실제로
PowerPoint 검증에서 커서가 `(698,492) → (1396,984)` 로 "움직인" 것처럼 보였는데,
커서는 가만히 있었고 좌표계가 바뀐 것이었습니다. 이 호출을 지연시키지 마세요.

### 3. COM 아파트먼트는 comtypes 임포트 **전에** 정해야 합니다

`comtypes` 는 임포트되는 순간 아파트먼트에 들어가고, 한 번 정해지면 못 바꿉니다
(`RPC_E_CHANGED_MODE`). `uia/core.py` 는 모듈 임포트 시점에 `prefer_mta()` 로
선호를 등록하고, 이미 정해진 아파트먼트는 실패로 취급하지 않습니다.

`doctor` 는 이 버그를 **가렸습니다** — 의존성 점검하느라 메인 스레드에서
`import comtypes` 를 먼저 해서요. doctor 만 초록불이고 나머지가 전부 죽은 적이
있습니다. doctor 통과를 UIA 가 동작한다는 증거로 삼지 마세요.

### 4. `win32` 엔진 호출을 COM 워커로 넘기면 안 됩니다

COM 아파트먼트 안에서 `SendMessage` 하면 `RPC_E_CANTCALLOUT_ININPUTSYNCCALL` 로
실패합니다. 게다가 UIA 를 못 쓰는 환경용 폴백인데 COM 을 강제 초기화하면 존재
이유가 무너집니다. `actions._call(element, ...)` 이 엔진을 보고 분기합니다.

### 5. 단축키(`ctrl+s` 등)는 근본적으로 안 먹습니다

단축키 테이블은 `GetKeyState` 로 **물리적** 키보드 상태를 확인합니다. `PostMessage`
는 그걸 바꾸지 않습니다. Enter·Tab·방향키·문자처럼 `WM_KEYDOWN`/`WM_CHAR` 를 직접
처리하는 키만 동작합니다. 명령 실행은 `menu_select`(실제 HMENU → `WM_COMMAND`)나
버튼 클릭을 쓰세요.

### 6. 컨트롤 이름의 `&` 는 제거해야 합니다

Windows 는 `Add appli&cation...` 을 `Add application...` 으로 그립니다. 안 벗기면
사용자가 보는 이름으로 검색해도 못 찾습니다. UIA 는 자동으로 벗겨 주지만 `win32`
엔진은 직접 해야 합니다 (`controls.strip_accelerator`).

### 7. 최소화된 창 캡처

픽셀이 존재하지 않으므로 잠깐 복원해야 합니다. 규칙:
- 크기는 **절대** 건드리지 않습니다 (`rcNormalPosition` 은 최소화 중에 믿을 수
  없습니다 — 예전에 사용자 창을 320x240 으로 영구히 줄인 적이 있습니다)
- `SW_SHOWNOACTIVATE` 로 복원하고 z-order 맨 뒤로 보냅니다
- 캡처 후 위치와 최소화 상태를 정확히 되돌립니다
- `PrintWindow` 가 검정을 주면 BitBlt 로 폴백하되 `degraded=True` 로 표시합니다
  (BitBlt 는 화면 픽셀을 읽으므로 위에 겹친 창이 섞일 수 있음)

### 8. Windows 콘솔 인코딩

한글 출력은 기본 코드페이지에서 `UnicodeEncodeError` 로 즉사합니다. 모든 진입점이
`force_utf8_output()` 을 부릅니다. 새 진입점을 만들면 이것도 부르세요.

## 지금까지 검증된 것

Wine 위 Windows CPython 으로 실행 중인 Win32 프로그램(메모장·winecfg) 상대,
**14/14 통과**: 최소화 상태에서 UI 트리 읽기(보임과 동일), 최소화 상태에서 한글
입력 후 바이트 단위 되읽기, 최소화 상태에서 버튼 클릭 → 실제 대화상자 열림, 그
대화상자를 찾아 닫기, 보임·최소화 양쪽 캡처, 창 기하 보존, 커서·전경 창 무간섭.

실제 Windows 11 + PowerPoint 에서 확인된 것: UIA 초기화 성공, DPI per-monitor-v2,
UI 트리 80개 요소(보임 0.20초 / 최소화 0.09초, 동일), 최소화 캡처 2138x1230 고유색
1700개 `degraded=False`(실제 Windows 에서는 PrintWindow 가 제대로 동작).

## 여기서 이어서 하세요

**PowerPoint UIA 검증을 다시 돌려야 합니다.** 마지막 실행은 DPI 버그와 검증기의
대상 선택 버그 때문에 오염된 결과였고, 둘 다 고쳤지만 재실행은 못 했습니다
(클라우드 세션이라 Windows 가 없었습니다).

```
python examples\verify_live.py --process POWERPNT --engine uia
```

FAIL 이 나오면 그게 진짜 버그입니다. 특히 볼 것:
- 편집 대상 선택이 슬라이드를 잡는지 (리본의 비활성 컨트롤이 아니라)
- 최소화 상태 입력이 PowerPoint 에서도 되는지 — 일부 앱은 최소화 중 명령을
  무시합니다 (실측: winecfg 의 Cancel)

그 다음 정리할 것: `list_windows` 가 `DWM Notification Window`, `OZADMsgWnd` 같은
사용자에게 안 보이는 보조 창을 걸러내지 못합니다.

## 스타일

- 주석은 **왜** 를 적습니다. 무엇을 하는지는 코드가 말합니다.
- 오류 메시지는 한국어로, 다음에 뭘 하면 되는지까지 적습니다.
- 새 동작을 추가하면 `tests/test_action_chains.py` 에 전략 선택 테스트를 같이
  넣으세요. 목만으로는 못 잡는 것이 많으니, 가능하면 실기검증도 돌리세요.
