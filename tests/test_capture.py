"""Pixel handling for window captures — no Windows needed for these paths."""

import io

import pytest

from claude_hands.win32.capture import Capture, CaptureError, _blank_ratio
from claude_hands.win32.windows import Rect

pytest.importorskip("PIL", reason="pillow not installed")
from PIL import Image  # noqa: E402


def make_capture(width=8, height=6, colour=(10, 20, 30), origin=(100, 50)) -> Capture:
    """Build a capture the way PrintWindow hands us pixels: BGRA, top-down."""

    blue, green, red = colour
    pixels = bytes([blue, green, red, 255]) * (width * height)
    rect = Rect(origin[0], origin[1], origin[0] + width, origin[1] + height)
    return Capture(width, height, pixels, rect)


def test_bgra_is_decoded_in_the_right_channel_order():
    capture = make_capture(colour=(10, 20, 30))  # B=10 G=20 R=30
    image = capture.to_image()
    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (30, 20, 10)


def test_to_png_produces_a_real_png():
    data = make_capture().to_png()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(io.BytesIO(data)).size == (8, 6)


def test_to_png_downscales_past_max_side():
    data = make_capture(width=400, height=200).to_png(max_side=100)
    assert Image.open(io.BytesIO(data)).size == (100, 50)


def test_to_png_keeps_small_images_untouched():
    data = make_capture(width=40, height=20).to_png(max_side=100)
    assert Image.open(io.BytesIO(data)).size == (40, 20)


def test_crop_uses_screen_coordinates_and_survives_the_round_trip():
    capture = make_capture(width=20, height=20, colour=(10, 20, 30), origin=(100, 50))
    cropped = capture.crop(Rect(105, 55, 115, 65))
    assert (cropped.width, cropped.height) == (10, 10)
    assert cropped.rect.left == 105 and cropped.rect.top == 55
    # Channel order must still be BGRA after the crop re-encode.
    assert cropped.to_image().getpixel((0, 0)) == (30, 20, 10)


def test_crop_clamps_to_the_captured_area():
    capture = make_capture(width=20, height=20, origin=(100, 50))
    cropped = capture.crop(Rect(110, 60, 500, 500))
    assert (cropped.width, cropped.height) == (10, 10)


def test_crop_outside_the_capture_is_an_error():
    capture = make_capture(width=20, height=20, origin=(100, 50))
    with pytest.raises(CaptureError):
        capture.crop(Rect(1000, 1000, 1010, 1010))


def test_blank_ratio_detects_printwindow_returning_black():
    black = bytes([0, 0, 0, 255]) * 4000
    assert _blank_ratio(black) == 1.0

    coloured = bytes([1, 2, 3, 255]) * 4000
    assert _blank_ratio(coloured) == 0.0

    assert _blank_ratio(b"") == 1.0


# --------------------------------------------------------------------------
# Putting a minimised window back after a capture
# --------------------------------------------------------------------------


@pytest.mark.parametrize("on_monitor, placement_restored", [(True, True), (False, False)])
def test_a_window_off_every_monitor_is_not_put_back_through_its_placement(
    monkeypatch, on_monitor, placement_restored
):
    # Found live 2026-11-11: SetWindowPlacement moves a window that sits wholly
    # outside every monitor onto one — a window parked at (9000, 9000) came
    # back on the user's screen after a minimised capture.
    from claude_hands.win32 import capture as cap

    state = {"minimized": True}
    calls = []
    where = Rect(9000, 9000, 9460, 9260)
    monkeypatch.setattr(cap, "is_minimized", lambda hwnd: state["minimized"])
    monkeypatch.setattr(cap, "get_placement", lambda hwnd: "placement")
    monkeypatch.setattr(cap, "show_without_activating", lambda hwnd: state.update(minimized=False))
    monkeypatch.setattr(cap, "send_to_bottom", lambda hwnd: None)
    monkeypatch.setattr(cap, "window_rect", lambda hwnd, **kw: where)
    monkeypatch.setattr(cap, "_grab_best", lambda hwnd, rect: (bytes([1, 2, 3, 255]) * 4, False))
    monkeypatch.setattr(cap, "_blank_ratio", lambda pixels: 0.0)
    monkeypatch.setattr(cap, "on_any_monitor", lambda rect: on_monitor)
    monkeypatch.setattr(cap, "set_placement",
                        lambda hwnd, placement: (calls.append("set_placement"), state.update(minimized=True)))
    monkeypatch.setattr(cap, "minimize", lambda hwnd: (calls.append("minimize"), state.update(minimized=True)))

    cap._capture(1, restore_if_minimized=True, settle_seconds=0)

    assert ("set_placement" in calls) is placement_restored
    assert state["minimized"]  # either way it ends minimised again
    if not on_monitor:
        assert calls == ["minimize"]
