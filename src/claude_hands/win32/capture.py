"""Capture a single window's pixels without bringing it to the front.

Two things make this work where a normal screen grab fails:

* ``PrintWindow(..., PW_RENDERFULLCONTENT)`` asks the window to redraw itself
  into our bitmap. Because the request goes to the window, not the screen, an
  occluded window renders its real content rather than whatever is on top.
* A minimised window has no backing surface at all, so it is briefly restored
  *off the visible desktop* with ``SW_SHOWNOACTIVATE`` and put straight back.
  The user's focus is never taken; the window never appears on screen.
"""

from __future__ import annotations

import ctypes
import io
import time
from dataclasses import dataclass

from .defs import (
    BI_RGB,
    DIB_RGB_COLORS,
    PW_RENDERFULLCONTENT,
    SRCCOPY,
    ClaudeHandsError,
    require_windows,
)
from .windows import (
    Rect,
    get_placement,
    is_minimized,
    minimize,
    move_window,
    send_to_bottom,
    set_placement,
    show_without_activating,
    virtual_screen_rect,
    window_rect,
)


class CaptureError(ClaudeHandsError):
    """Raised when a window refuses to render into our bitmap."""


@dataclass
class Capture:
    """Raw BGRA pixels plus the screen rect they came from."""

    width: int
    height: int
    pixels: bytes  # BGRA, top-down
    rect: Rect
    restored_offscreen: bool = False
    blank: bool = False

    def to_image(self):
        from PIL import Image

        return Image.frombuffer(
            "RGBA", (self.width, self.height), self.pixels, "raw", "BGRA", 0, 1
        ).convert("RGB")

    def to_png(self, *, max_side: int | None = 1600, quality_scale: float = 1.0) -> bytes:
        image = self.to_image()
        if quality_scale and quality_scale != 1.0:
            image = image.resize(
                (
                    max(1, int(image.width * quality_scale)),
                    max(1, int(image.height * quality_scale)),
                )
            )
        if max_side and max(image.size) > max_side:
            ratio = max_side / max(image.size)
            image = image.resize(
                (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    def crop(self, rect: Rect) -> "Capture":
        """Crop to a screen-coordinate rect (used for element screenshots)."""

        left = max(0, rect.left - self.rect.left)
        top = max(0, rect.top - self.rect.top)
        right = min(self.width, rect.right - self.rect.left)
        bottom = min(self.height, rect.bottom - self.rect.top)
        if right <= left or bottom <= top:
            raise CaptureError("잘라낼 영역이 캡처 범위 밖입니다.")
        image = self.to_image().crop((left, top, right, bottom)).convert("RGBA")
        return Capture(
            width=image.width,
            height=image.height,
            pixels=image.tobytes("raw", "BGRA"),
            rect=Rect(
                self.rect.left + left,
                self.rect.top + top,
                self.rect.left + right,
                self.rect.top + bottom,
            ),
            restored_offscreen=self.restored_offscreen,
            blank=self.blank,
        )


def _blank_ratio(pixels: bytes) -> float:
    """Fraction of sampled pixels that are pure black — PrintWindow's failure mode."""

    if not pixels:
        return 1.0
    step = max(4, (len(pixels) // 4 // 2000) * 4)
    sampled = 0
    blank = 0
    for offset in range(0, len(pixels) - 3, step):
        sampled += 1
        if pixels[offset] == 0 and pixels[offset + 1] == 0 and pixels[offset + 2] == 0:
            blank += 1
    return blank / sampled if sampled else 1.0


def _grab(hwnd: int, rect: Rect, *, use_bitblt: bool) -> bytes:
    from .defs import BITMAPINFO, gdi32, user32

    width, height = rect.width, rect.height
    if width <= 0 or height <= 0:
        raise CaptureError(f"창 크기가 유효하지 않습니다: {width}x{height}")

    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        raise CaptureError("GetWindowDC 실패 (창이 닫혔을 수 있습니다).")
    mem_dc = bitmap = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not mem_dc or not bitmap:
            raise CaptureError("호환 DC/비트맵 생성 실패")
        old = gdi32.SelectObject(mem_dc, bitmap)

        if use_bitblt:
            ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, window_dc, 0, 0, SRCCOPY)
        else:
            # PW_RENDERFULLCONTENT is what makes DirectComposition apps
            # (Chrome, Electron, WinUI) render instead of returning black.
            ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not ok:
                ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            raise CaptureError("PrintWindow/BitBlt 가 실패했습니다.")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative => top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
        )
        if copied == 0:
            raise CaptureError("GetDIBits 가 픽셀을 반환하지 않았습니다.")
        gdi32.SelectObject(mem_dc, old)
        return buffer.raw
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, window_dc)


def capture_window(
    hwnd: int,
    *,
    restore_if_minimized: bool = True,
    settle_seconds: float = 0.12,
) -> Capture:
    """Capture ``hwnd`` even if it is behind other windows or minimised.

    Returns a :class:`Capture`; ``restored_offscreen`` says whether the
    minimised-window workaround had to run.
    """

    require_windows()

    minimized = is_minimized(hwnd)
    if not minimized:
        rect = window_rect(hwnd)
        pixels = _grab(hwnd, rect, use_bitblt=False)
        if _blank_ratio(pixels) > 0.98:
            # Some GDI-era apps ignore PrintWindow; if the window happens to be
            # visible, a straight BitBlt of its own DC still gets real pixels.
            fallback = _grab(hwnd, rect, use_bitblt=True)
            if _blank_ratio(fallback) < 0.98:
                pixels = fallback
        return Capture(rect.width, rect.height, pixels, rect)

    if not restore_if_minimized:
        raise CaptureError(
            "창이 최소화되어 있어 픽셀이 존재하지 않습니다. "
            "restore_if_minimized=True 로 호출하거나, 화면 대신 snapshot(UI 트리)을 쓰세요."
        )

    # --- minimised: no pixels exist, so the window has to come back briefly ---
    #
    # It is restored *without activation* and pushed to the bottom of the
    # z-order, so it never takes focus and never covers what the user is doing.
    # Its size is never touched: a window's own restored geometry is measured
    # after it comes back and put back exactly, because the placement struct's
    # rcNormalPosition cannot be trusted while a window is minimised on every
    # platform.
    placement = get_placement(hwnd)
    restored_rect = None
    parked = False
    try:
        show_without_activating(hwnd)
        send_to_bottom(hwnd)
        time.sleep(settle_seconds)
        restored_rect = window_rect(hwnd, frame_bounds=False)

        rect = window_rect(hwnd)
        pixels = _grab(hwnd, rect, use_bitblt=False)

        if _blank_ratio(pixels) > 0.98:
            # Some compositors will not render a window sitting at the bottom
            # of the z-order. Moving it off the desktop — position only, never
            # size — gives a second chance.
            virtual = virtual_screen_rect()
            move_window(hwnd, virtual.right + 64, virtual.top)
            parked = True
            time.sleep(settle_seconds)
            retry_rect = window_rect(hwnd)
            retry_pixels = _grab(hwnd, retry_rect, use_bitblt=False)
            if _blank_ratio(retry_pixels) <= 0.98:
                rect, pixels = retry_rect, retry_pixels

        return Capture(
            rect.width,
            rect.height,
            pixels,
            rect,
            restored_offscreen=True,
            blank=_blank_ratio(pixels) > 0.98,
        )
    finally:
        # Put the window back exactly: original position first (size was never
        # changed), then the minimised state it was found in.
        if parked and restored_rect is not None:
            try:
                move_window(hwnd, restored_rect.left, restored_rect.top)
            except ClaudeHandsError:
                pass
        try:
            set_placement(hwnd, placement)
        except ClaudeHandsError:
            pass
        if not is_minimized(hwnd):
            minimize(hwnd)
