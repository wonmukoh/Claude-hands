"""메모장이 최소화된 상태 그대로 글을 쓰고 저장하는 예제.

실행 전에 메모장을 하나 열어 두고 **최소화**해 두세요. 실행하는 동안 다른 일을
계속 해도 됩니다 — 마우스 커서도, 키보드 포커스도 건드리지 않습니다.

    python examples/minimized_notepad.py
"""

from claude_hands import attach, windows

def main() -> None:
    for info in windows(process="notepad"):
        print("찾은 창:", info.describe())

    notepad = attach(process="notepad.exe")
    print("\n연결:", notepad.info.describe())
    print("현재 상태:", notepad.info.state, "(minimized 여도 아래 동작은 그대로 됩니다)")

    # 1) UI 트리 읽기 — 화면 캡처가 아니라 UI Automation 이라 최소화 상태에서도 정확합니다.
    print("\n--- 조작 가능한 요소 ---")
    print(notepad.snapshot(interactive_only=True))

    # 2) 편집 영역을 찾아 글자 넣기
    editors = notepad.find("텍스트 편집기", role="document") or notepad.find("", role="document")
    if not editors:
        editors = notepad.find("", role="edit")
    if not editors:
        print("편집 영역을 찾지 못했습니다. 위 스냅샷에서 ref 를 골라 직접 지정하세요.")
        return

    editor = editors[0]
    result = notepad.type(editor.ref, "claude-hands 가 최소화된 창에 쓴 문장입니다.\n")
    print("\n입력:", result.describe())

    # 3) 저장 단축키 — 창 메시지로 보내므로 내 키보드는 그대로 쓸 수 있습니다.
    print("저장:", notepad.keys("ctrl+s").describe())

    # 4) 저장 대화상자가 뜨는지 확인 (별도 창이므로 새로 attach)
    try:
        dialog = attach(title="저장")
        print("\n--- 저장 대화상자 ---")
        print(dialog.snapshot(interactive_only=True))
    except Exception as exc:  # noqa: BLE001 - 이미 저장된 파일이면 대화상자가 없습니다
        print("저장 대화상자 없음:", exc)


if __name__ == "__main__":
    main()
