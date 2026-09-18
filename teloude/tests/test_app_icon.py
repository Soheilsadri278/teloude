# teloude/tests/test_app_icon.py
"""The official icon: assets, multi-resolution .ico, and runtime wiring.

The logo itself (assets/icon-2.png) is the user's file and is never modified;
these tests pin that the *derivatives* stay faithful to it - the .ico frames
decode to the same design, the rounded corners survive, the small sizes are
present so Windows never stretches a blurry substitute - and that the
application actually loads the asset (window/taskbar/tray), not a placeholder.
"""
import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGO_PATH = REPO_ROOT / "assets" / "icon-2.png"
ICO_PATH = REPO_ROOT / "assets" / "icon.ico"
PNG_PATH = REPO_ROOT / "assets" / "icon.png"
SPEC_PATH = REPO_ROOT / "teloude.spec"
ISS_PATH = REPO_ROOT / "installer" / "teloude.iss"

# The official logo's fingerprint. A deliberately new logo updates this hash
# AND regenerates assets/icon.ico / assets/icon.png in the same commit.
LOGO_SHA256 = (
    "0e30a9bcb3ffd72838bcac6e272a081406f6c9c84ba326db8b487f994be7010d"
)


def _ico_frames(path: Path) -> list:
    """Parses the .ico directory: [(width, height, bits, format, bytes)]."""
    data = path.read_bytes()
    _, kind, count = struct.unpack("<HHH", data[:6])
    assert kind == 1, "not an .ico file"
    frames = []
    offset = 6
    for _ in range(count):
        w, h, _, _, _, bpp, size, _ = struct.unpack("<BBBBHHII", data[offset:offset + 16])
        start = struct.unpack("<I", data[offset + 12:offset + 16])[0]
        blob = data[start:start + size]
        fmt = "png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "bmp"
        frames.append((w or 256, h or 256, bpp, fmt, blob))
        offset += 16
    return frames


class TestTheOfficialLogo:
    def test_the_logo_file_is_untouched(self):
        import hashlib

        data = LOGO_PATH.read_bytes()
        assert hashlib.sha256(data).hexdigest() == LOGO_SHA256, (
            "assets/icon-2.png changed - if the logo really changed, update "
            "LOGO_SHA256 and regenerate the .ico/.png derivatives"
        )

    def test_the_logo_is_a_256_rgba_png(self):
        image = _load(LOGO_PATH)
        assert image.size == (256, 256) and image.mode == "RGBA"


def _load(path: Path):
    from PIL import Image

    return Image.open(path).convert("RGBA")


class TestTheWindowsIcon:
    """assets/icon.ico: what the EXE, the installer and the shortcuts show."""

    def test_every_windows_size_is_present(self):
        sizes = {(w, h) for w, h, _, _, _ in _ico_frames(ICO_PATH)}
        assert sizes == {(16, 16), (24, 24), (32, 32), (48, 48),
                         (64, 64), (128, 128), (256, 256)}

    def test_the_largest_frame_is_the_logo_pixel_for_pixel(self):
        from PIL import Image
        import io

        source = _load(LOGO_PATH)
        frame = next(f for f in _ico_frames(ICO_PATH) if f[0] == 256)
        assert frame[3] == "png", "the 256px frame must be lossless"
        decoded = Image.open(io.BytesIO(frame[4])).convert("RGBA")
        assert decoded.tobytes() == source.tobytes(), (
            "the icon altered the logo design"
        )

    def test_the_rounded_corners_survive(self):
        frame = next(f for f in _ico_frames(ICO_PATH) if f[0] == 256)
        from PIL import Image
        import io

        decoded = Image.open(io.BytesIO(frame[4])).convert("RGBA")
        assert decoded.getpixel((0, 0))[3] == 0, "corner must stay transparent"
        assert decoded.getpixel((128, 128))[3] == 255

    def test_small_frames_are_not_flat_placeholder_art(self):
        """The 16px frame must carry the real design, not a solid colour.

        A solid fill (or an old logo) fails: the Teloude sheet is a white-to-
        blue gradient with dark text, so its horizontal colour spread within
        one row is large; a placeholder's is near zero.
        """
        from PIL import Image
        import io

        frame = next(f for f in _ico_frames(ICO_PATH) if f[0] == 16)
        decoded = Image.open(io.BytesIO(frame[4])).convert("RGBA")
        row = [decoded.getpixel((x, 12)) for x in range(16)]
        reds = [p[0] for p in row]
        assert max(reds) - min(reds) >= 40, "the small frame lost the design"

    def test_the_official_png_matches_the_logo(self):
        assert _load(PNG_PATH).tobytes() == _load(LOGO_PATH).tobytes()


class TestTheBuildAssetsReferenceTheIcon:
    def test_the_spec_bundles_the_icon_and_stamps_the_exe(self):
        text = SPEC_PATH.read_text(encoding="utf-8")
        assert '("assets/icon.png", "assets")' in text
        assert '("assets/icon.ico", "assets")' in text, (
            "the window/taskbar icon must ship inside the bundle"
        )
        assert 'icon="assets/icon.ico"' in text, "the EXE icon"

    def test_the_installer_uses_the_icon_for_setup_and_shortcuts(self):
        text = ISS_PATH.read_text(encoding="utf-8")
        assert "SetupIconFile=..\\assets\\icon.ico" in text
        # The shortcuts inherit the installed EXE's icon (no IconFilename
        # override), and the uninstall entry points at the EXE as well.
        assert "UninstallDisplayIcon={app}" in text
        assert "IconFilename" not in text, (
            "a hardcoded override would bypass the official icon"
        )


class TestRuntimeIconWiring:
    """The app and the tray load the real asset (Qt, offscreen)."""

    @pytest.fixture(scope="class")
    def qt_app(self):
        qt = pytest.importorskip("PySide6.QtWidgets")
        return qt.QApplication.instance() or qt.QApplication([])

    def test_application_icon_loads_the_multi_resolution_asset(self, qt_app):
        from PySide6 import QtCore

        from teloude.ui.app import application_icon

        icon = application_icon()
        assert not icon.isNull()
        sizes = {icon.actualSize(QtCore.QSize(size, size)).width()
                 for size in (16, 32, 48, 256)}
        assert {16, 32, 48} <= sizes, f"icon lacks the Windows sizes: {sizes}"

    def test_the_tray_carries_the_same_official_icon(self, qt_app):
        from teloude.ui.tray import _default_icon

        icon = _default_icon()
        assert not icon.isNull()
        assert not icon.pixmap(32).isNull()

    def test_a_missing_asset_degrades_to_null_without_crashing(self, qt_app, monkeypatch):
        from teloude.ui import app as app_module

        monkeypatch.setattr(app_module, "_icon_search_bases",
                            lambda: [Path("/nonexistent-teloude-assets")])
        icon = app_module.application_icon()
        assert icon.isNull(), "no asset means no icon - never a fake one"
