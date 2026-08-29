import pytest

from claude_hands.cli import build_parser


def test_every_subcommand_is_registered():
    parser = build_parser()
    for command in [
        "windows", "snapshot", "find", "click", "type",
        "keys", "scroll", "text", "shot", "menu", "state", "doctor",
    ]:
        args = parser.parse_args([command] + _minimal_args(command))
        assert args.command == command
        assert callable(args.func)


def _minimal_args(command: str) -> list[str]:
    return {
        "find": ["저장"],
        "type": ["안녕하세요"],
        "keys": ["ctrl+s"],
        "menu": ["파일 > 저장"],
        "state": ["info"],
    }.get(command, [])


def test_click_accepts_query_instead_of_ref():
    args = build_parser().parse_args(["click", "--title", "메모장", "--query", "저장"])
    assert args.title == "메모장"
    assert args.query == "저장"
    assert args.ref is None


def test_click_rejects_unknown_button():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["click", "--button", "quadruple"])


def test_snapshot_flags():
    args = build_parser().parse_args(["snapshot", "--hwnd", "123", "--interactive", "--depth", "5"])
    assert args.hwnd == 123
    assert args.interactive is True
    assert args.depth == 5
