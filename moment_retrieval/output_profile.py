"""Typed, local-only output and caption profiles.

Profiles deliberately describe rendering choices only.  They never carry a
source path, transcript text, or an editor history, so they can be included in
an artifact manifest without exposing private input locations.
"""
from __future__ import annotations

import ctypes
import os
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from ctypes import wintypes


CanvasMode = Literal["source", "portrait_blur", "portrait_crop"]
CaptionPreset = Literal["standard", "large", "boxed"]
CaptionPosition = Literal["top", "center", "bottom"]


@dataclass(frozen=True)
class CaptionProfile:
    """A constrained caption style which can safely be previewed and saved."""

    preset: CaptionPreset = "standard"
    position: CaptionPosition = "bottom"
    enabled: bool = True
    font_name: str = "Yu Gothic UI"
    # These are fractions of the target canvas, rather than preview pixels.
    safe_margin_horizontal: float = 0.065
    safe_margin_vertical: float = 0.105
    max_chars: int = 10
    minimum_part_ms: int = 500

    def validate(self) -> "CaptionProfile":
        if self.preset not in {"standard", "large", "boxed"}:
            raise ValueError("caption preset must be standard, large, or boxed")
        if self.position not in {"top", "center", "bottom"}:
            raise ValueError("caption position must be top, center, or bottom")
        if not self.font_name or any(ch in self.font_name for ch in ",\r\n"):
            raise ValueError("caption font name is invalid")
        for value in (self.safe_margin_horizontal, self.safe_margin_vertical):
            if not 0.0 <= float(value) < 0.5:
                raise ValueError("caption safe margins must be between 0 and 0.5")
        if int(self.max_chars) < 4:
            raise ValueError("caption max_chars must be at least 4")
        if int(self.minimum_part_ms) <= 0:
            raise ValueError("caption minimum_part_ms must be positive")
        return self

    def with_preset(self, preset: CaptionPreset) -> "CaptionProfile":
        return replace(self, preset=preset).validate()

    def to_manifest(self) -> dict[str, object]:
        self.validate()
        return {
            "preset": self.preset,
            "position": self.position,
            "enabled": self.enabled,
            "font_name": self.font_name,
            "safe_margin_horizontal": self.safe_margin_horizontal,
            "safe_margin_vertical": self.safe_margin_vertical,
            "max_chars": self.max_chars,
            "minimum_part_ms": self.minimum_part_ms,
        }


@dataclass(frozen=True)
class AudioProfile:
    """Reserved output-audio contract; processing is intentionally deferred."""

    normalize_source: bool = False

    def to_manifest(self) -> dict[str, object]:
        return {"normalize_source": self.normalize_source}


@dataclass(frozen=True)
class OutputProfile:
    """Immutable rendering settings, independent from the edit plan/history."""

    profile_id: str = "source-standard"
    canvas_mode: CanvasMode = "source"
    width: int | None = None
    height: int | None = None
    caption: CaptionProfile = CaptionProfile()
    audio: AudioProfile = AudioProfile()
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    crf: int = 20
    encoding_preset: str = "veryfast"
    profile_version: int = 1

    def validate(self) -> "OutputProfile":
        if not self.profile_id or any(ch in self.profile_id for ch in "\r\n"):
            raise ValueError("output profile_id is invalid")
        if self.canvas_mode not in {"source", "portrait_blur", "portrait_crop"}:
            raise ValueError("output canvas mode is invalid")
        if self.canvas_mode == "source":
            if self.width is not None or self.height is not None:
                raise ValueError("source output profiles must not set a canvas size")
        else:
            if self.width is None or self.height is None:
                raise ValueError("portrait output profiles require width and height")
            if self.width <= 0 or self.height <= 0 or self.width >= self.height:
                raise ValueError("portrait output profile requires positive portrait dimensions")
            if self.width % 2 or self.height % 2:
                raise ValueError("portrait output profile requires even dimensions")
        if self.video_codec != "libx264" or self.pixel_format != "yuv420p":
            raise ValueError("only the local H.264/yuv420p output profile is supported")
        if not 0 <= int(self.crf) <= 51:
            raise ValueError("output CRF must be between 0 and 51")
        if not self.encoding_preset:
            raise ValueError("output encoding preset is required")
        self.caption.validate()
        return self

    @classmethod
    def source(cls, *, caption: CaptionProfile | None = None) -> "OutputProfile":
        return cls(caption=caption or CaptionProfile()).validate()

    @classmethod
    def portrait(
        cls, width: int = 1080, height: int = 1920, *, layout: Literal["blur", "crop"] = "blur",
        caption: CaptionProfile | None = None,
    ) -> "OutputProfile":
        mode: CanvasMode = "portrait_blur" if layout == "blur" else "portrait_crop"
        return cls(
            profile_id=f"portrait-{layout}-{width}x{height}", canvas_mode=mode,
            width=width, height=height, caption=caption or CaptionProfile(),
        ).validate()

    def to_manifest(self) -> dict[str, object]:
        self.validate()
        return {
            "profile_version": self.profile_version,
            "profile_id": self.profile_id,
            "canvas_mode": self.canvas_mode,
            "width": self.width,
            "height": self.height,
            "caption": self.caption.to_manifest(),
            "audio": self.audio.to_manifest(),
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "crf": self.crf,
            "encoding_preset": self.encoding_preset,
        }


@dataclass(frozen=True)
class CaptionStyle:
    """Resolved ASS style values for one canvas; not persisted as user state."""

    font_name: str
    font_size: int
    outline: int
    shadow: int
    border_style: int
    alignment: int
    margin_left: int
    margin_right: int
    margin_vertical: int
    primary_colour: str = "&H00FFFFFF"
    secondary_colour: str = "&H000000FF"
    outline_colour: str = "&H00000000"
    back_colour: str = "&H78000000"
    bold: int = -1


def caption_style_for_canvas(
    profile: CaptionProfile, width: int, height: int,
) -> CaptionStyle:
    """Resolve a preset once so ASS wrapping and styling share safe margins."""
    profile.validate()
    if width <= 0 or height <= 0:
        raise ValueError("caption canvas dimensions must be positive")
    # Standard intentionally matches the legacy short-caption defaults.
    scale = {"standard": 0.041, "large": 0.052, "boxed": 0.045}[profile.preset]
    font_size = max(32, round(height * scale))
    outline = max(2, round(height * (0.0035 if profile.preset == "large" else 0.003)))
    margin_h = max(40, round(width * profile.safe_margin_horizontal))
    margin_v = max(64, round(height * profile.safe_margin_vertical))
    alignment = {"bottom": 2, "center": 5, "top": 8}[profile.position]
    if profile.preset == "boxed":
        return CaptionStyle(
            profile.font_name, font_size, 0, 0, 3, alignment, margin_h, margin_h, margin_v,
            back_colour="&H50000000",
        )
    return CaptionStyle(
        profile.font_name, font_size, outline, 2, 1, alignment,
        margin_h, margin_h, margin_v,
    )


@dataclass(frozen=True)
class FontValidation:
    """Privacy-safe result: no filesystem paths are exposed."""

    requested_font: str
    resolved_font: str | None
    font_available: bool | None
    glyphs_supported: bool | None
    missing_glyph_count: int
    warning: str | None = None


class FontInspector(Protocol):
    def resolve_face(self, font_name: str) -> str | None: ...

    def missing_glyphs(self, font_name: str, text: str) -> tuple[str, ...] | None: ...


class WindowsGdiFontInspector:
    """Small Windows-only GDI adapter, isolated from profile/domain logic."""

    def _with_font(self, font_name: str, callback):
        if os.name != "nt":
            return None
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # Explicit pointer return types are essential on 64-bit Python.  The
        # defaults are ``c_int`` and would truncate GDI handles.
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateFontW.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPCWSTR,
        ]
        gdi32.CreateFontW.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.GetTextFaceW.argtypes = [wintypes.HDC, ctypes.c_int, wintypes.LPWSTR]
        gdi32.GetTextFaceW.restype = ctypes.c_int
        gdi32.GetGlyphIndicesW.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ushort), wintypes.DWORD,
        ]
        gdi32.GetGlyphIndicesW.restype = wintypes.DWORD
        hdc = user32.GetDC(None)
        # Normal weight and a small logical height are enough for font lookup.
        hfont = gdi32.CreateFontW(
            -32, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, font_name,
        )
        if not hdc or not hfont:
            if hdc:
                user32.ReleaseDC(None, hdc)
            return None
        old = gdi32.SelectObject(hdc, hfont)
        try:
            return callback(gdi32, hdc)
        finally:
            gdi32.SelectObject(hdc, old)
            gdi32.DeleteObject(hfont)
            user32.ReleaseDC(None, hdc)

    def resolve_face(self, font_name: str) -> str | None:
        def read_face(gdi32, hdc):
            value = ctypes.create_unicode_buffer(128)
            length = gdi32.GetTextFaceW(hdc, len(value), value)
            return value.value if length else None

        return self._with_font(font_name, read_face)

    def missing_glyphs(self, font_name: str, text: str) -> tuple[str, ...] | None:
        text = "".join(dict.fromkeys(
            char for char in str(text or "")
            if unicodedata.category(char)[0] in {"L", "N"}
        ))[:128]
        if not text:
            return ()

        def read_glyphs(gdi32, hdc):
            values = (ctypes.c_ushort * len(text))()
            count = gdi32.GetGlyphIndicesW(hdc, text, len(text), values, 0)
            if count == 0xFFFFFFFF:
                return None
            return tuple(char for char, value in zip(text, values) if value == 0xFFFF)

        return self._with_font(font_name, read_glyphs)


def validate_font_glyphs(
    font_name: str, text: str, *, inspector: FontInspector | None = None,
) -> FontValidation:
    """Check a requested face and cue text without reading or logging paths."""
    if not font_name or any(ch in font_name for ch in ",\r\n"):
        return FontValidation(font_name, None, False, None, 0, "FONT_NAME_INVALID")
    inspector = inspector or WindowsGdiFontInspector()
    resolved = inspector.resolve_face(font_name)
    if resolved is None:
        return FontValidation(font_name, None, None, None, 0, "FONT_CHECK_UNAVAILABLE")
    available = resolved.casefold() == font_name.casefold()
    missing = inspector.missing_glyphs(font_name, text)
    if missing is None:
        return FontValidation(
            font_name, resolved, available, None, 0, "FONT_GLYPH_CHECK_UNAVAILABLE",
        )
    return FontValidation(
        font_name, resolved, available, not missing, len(missing),
        "FONT_FALLBACK_REQUIRED" if not available or missing else None,
    )
