"""가려진 창에서 여러 단계를 순서대로 처리하는 골격.

'설치 마법사처럼 다음 → 다음 → 완료' 같은 흐름을, 사용자가 다른 창에서 일하는
동안 뒤에서 진행하는 패턴입니다.

    python examples/background_workflow.py "설치"
"""

import sys
import time

from claude_hands import attach
from claude_hands.actions import ActionFailedError


def run(title: str) -> int:
    app = attach(title=title)
    print("연결:", app.info.describe())

    steps = [
        # (기다릴 요소, 누를 요소)
        ("다음", "다음"),
        ("동의", "동의"),
        ("설치", "설치"),
        ("완료", "완료"),
    ]

    for wait_for_name, click_name in steps:
        try:
            found = app.wait_for(wait_for_name, timeout=30, enabled=True)
            print("발견:", found.describe())
        except ActionFailedError as exc:
            print("건너뜀:", exc)
            continue

        matches = app.find(click_name, limit=1)
        if not matches:
            print(f"{click_name!r} 없음 — 중단")
            return 1

        result = app.click(matches[0].ref)
        print("클릭:", result.describe())
        time.sleep(0.5)

    print("\n최종 상태:")
    print(app.snapshot(interactive_only=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else "설치"))
