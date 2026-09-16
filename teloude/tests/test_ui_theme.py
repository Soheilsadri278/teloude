# teloude/tests/test_ui_theme.py
"""Design-system tests: tokens, the 8pt grid, the glass surfaces, appearance.

These are UI-only checks. They never touch backup, restore, Telegram, the
database or the session layer; they assert what the screenshots would show -
that the grid holds, that the rail stays text-only, that the action bar lifts
only while a run is genuinely live, that the scrim is up behind a modal and that
light/dark both resolve.

Everything runs offscreen with the offline fakes, like the other UI tests.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.config import AppConfig  # noqa: E402
from teloude.ui import theme  # noqa: E402
from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.components import (  # noqa: E402
    FloatingBar, GlassCard, NavItemDelegate, NavRail, Scrim, present_blocking, with_scrim,
)
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def light_theme(qt_app):
    """Guarantees the other tests see the default appearance again."""
    theme.apply_theme(qt_app, dark=False)
    yield qt_app
    theme.apply_theme(qt_app, dark=False)


@pytest.fixture()
def window(light_theme, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "theme.db"))
    ctx = build_offline(config)
    ctx.bridge = ServiceBridge(ctx.bus)
    ctx.asker = UiThreadAsker()
    return MainWindow(ctx)


# ------------------------------------------------------------- colour maths --
def _luminance(rgb) -> float:
    channels = []
    for channel in rgb:
        srgb = channel / 255
        channels.append(srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(foreground, background) -> float:
    first, second = _luminance(foreground), _luminance(background)
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def _rgb(color: QtGui.QColor):
    return (color.red(), color.green(), color.blue())


def _over(top: QtGui.QColor, bottom: QtGui.QColor) -> QtGui.QColor:
    """`top` composited over `bottom` at top's alpha - what a glass fill does."""
    alpha = top.alpha() / 255
    return QtGui.QColor(
        int(round(alpha * top.red() + (1 - alpha) * bottom.red())),
        int(round(alpha * top.green() + (1 - alpha) * bottom.green())),
        int(round(alpha * top.blue() + (1 - alpha) * bottom.blue())),
    )


class TestTokens:
    def test_every_spacing_token_is_on_the_eight_point_grid(self):
        assert theme.BASE == 8
        for name, value in theme.SPACING.items():
            assert value % theme.BASE == 0, f"{name}={value} is not a multiple of 8"
            assert value > 0

    def test_radii_come_from_the_scale(self):
        assert set(theme.RADII) == {8, 12, 20}
        assert (theme.RADIUS_SMALL, theme.RADIUS_CONTROL, theme.RADIUS_CARD) == (8, 12, 20)

    def test_minimum_targets_match_the_design_system(self):
        assert theme.MIN_TARGET == 44
        assert theme.FIELD_HEIGHT == 36

    def test_fonts_are_sf_pro_with_a_native_fallback_only(self):
        for families in (theme.FONT_FAMILIES_TEXT, theme.FONT_FAMILIES_DISPLAY):
            assert families[0].startswith("SF Pro"), families
            assert any(name.startswith(("Segoe UI", "Noto Sans")) for name in families[:-1]), families
            assert families[-1] == "sans-serif"
            for name in families:
                for forbidden in theme.FORBIDDEN_FAMILIES:
                    assert forbidden not in name.lower(), f"{name} is forbidden by the skill"

    def test_light_is_the_default_and_dark_is_a_separate_set(self):
        assert theme.tokens().name == "light"
        assert theme.LIGHT.is_dark is False and theme.DARK.is_dark is True
        # Apple's exact accent values, light and dark.
        assert theme.LIGHT.accent.upper() == "#007AFF"
        assert theme.DARK.accent.upper() == "#0A84FF"

    def test_the_two_appearances_differ_where_it_matters(self):
        for name in ("canvas", "surface", "field", "separator", "glass", "label",
                     "label_secondary", "accent", "scrim"):
            assert getattr(theme.LIGHT, name) != getattr(theme.DARK, name), name

    def test_motion_uses_the_documented_curves(self):
        assert theme.MOTION_MS == 300
        standard = theme.motion_curve()
        spring = theme.motion_curve(spring=True)
        for curve in (standard, spring):
            assert round(curve.valueForProgress(0.0), 6) == 0.0
            assert round(curve.valueForProgress(1.0), 6) == 1.0
        # The spring overshoots on the way (cubic-bezier(0.4, 0, 0.2, 1.4)).
        assert max(spring.valueForProgress(step / 100) for step in range(101)) > 1.0
        # The standard curve is monotonic-ish and eases in and out.
        assert standard.valueForProgress(0.25) < 0.5 < standard.valueForProgress(0.75)


class TestContrast:
    """Measured with the WCAG formula, on the composited glass colours."""

    @pytest.mark.parametrize("dark", [False, True])
    def test_text_on_glass_surfaces_meets_aa(self, dark):
        tokens = theme.tokens(dark)
        canvas = tokens.color("canvas")
        rail = _over(tokens.color("glass"), canvas)
        capsule = _over(tokens.color("accent_tint"), rail)
        label = tokens.color("label")
        secondary = tokens.color("label_secondary")
        assert _contrast(_rgb(label), _rgb(rail)) >= 4.5
        assert _contrast(_rgb(secondary), _rgb(rail)) >= 4.5
        assert _contrast(_rgb(label), _rgb(capsule)) >= 4.5

    @pytest.mark.parametrize("dark", [False, True])
    def test_the_accent_capsule_is_visible_but_never_carries_small_text(self, dark):
        tokens = theme.tokens(dark)
        canvas = tokens.color("canvas")
        rail = _over(tokens.color("glass"), canvas)
        capsule = _over(tokens.color("accent_tint"), rail)
        # The tinted fill is a graphic, so 3:1 applies; the label uses --label.
        assert _contrast(_rgb(tokens.color("accent")), _rgb(capsule)) >= 3.0
        assert _contrast(_rgb(tokens.color("label")), _rgb(capsule)) >= 4.5


class TestThemeApplication:
    def test_stylesheet_matches_the_active_appearance(self, light_theme):
        window_rule = "QMainWindow, QDialog, QWidget#Canvas {{ background: {}; }}"
        light = theme.apply_theme(light_theme, dark=False)
        assert light.name == "light"
        assert window_rule.format(theme.LIGHT.canvas) in light_theme.styleSheet()
        assert window_rule.format(theme.DARK.canvas) not in light_theme.styleSheet()

        dark = theme.apply_theme(light_theme, dark=True)
        assert dark.name == "dark"
        assert window_rule.format(theme.DARK.canvas) in light_theme.styleSheet()
        assert window_rule.format(theme.LIGHT.canvas) not in light_theme.styleSheet()
        assert theme.tokens().name == "dark"
        theme.apply_theme(light_theme, dark=False)
        assert theme.tokens().name == "light"

    def test_neither_appearance_mentions_a_forbidden_font(self):
        for dark in (False, True):
            stylesheet = theme.build_stylesheet(dark).lower()
            for forbidden in theme.FORBIDDEN_FAMILIES:
                assert forbidden not in stylesheet

    def test_unknown_stored_preferences_fall_back_to_light(self, light_theme):
        class Services:
            class settings:  # noqa: N801 - a tiny stand-in for SettingsService
                @staticmethod
                def get(key, default=None):
                    return "psychedelic"

        assert theme.stored_appearance(Services) == "light"

    def test_the_choice_is_remembered_through_the_settings_service(self, window):
        settings = window._ctx.services.settings
        assert theme.remember_appearance(window._ctx.services, "dark") == "dark"
        assert settings.get("ui.appearance") == "dark"
        assert theme.stored_appearance(window._ctx.services) == "dark"
        theme.remember_appearance(window._ctx.services, "light")
        assert theme.stored_appearance(window._ctx.services) == "light"


class TestNavigationRail:
    def test_the_rail_is_text_only(self, window):
        nav = window.nav
        assert isinstance(nav, NavRail)
        assert nav.count() == 7
        for row in range(nav.count()):
            item = nav.item(row)
            assert item.icon().isNull(), "no icons: the rail is text-only"
            assert item.text() and not item.text().strip().startswith(
                ("\u2460", "\u2461", "\u2462", "\u2463", "\u2464")), "no numbered badges"
        # Nothing painted on top of the rows either (a badge would be a child).
        assert nav.findChildren(QtWidgets.QLabel) == []
        assert [child for child in nav.children() if isinstance(child, QtWidgets.QWidget)
                and child.objectName().lower().startswith("badge")] == []

    def test_rows_are_at_least_44px_tall(self, window):
        for row in range(window.nav.count()):
            height = window.nav.item(row).sizeHint().height()
            assert height >= theme.MIN_TARGET, f"row {row} is {height}px tall"

    def test_the_selected_row_is_painted_as_a_translucent_capsule(self, window):
        delegate = window.nav.itemDelegate()
        assert isinstance(delegate, NavItemDelegate)
        model = window.nav.model()
        index = model.index(0, 0)

        def render(selected: bool):
            option = QtWidgets.QStyleOptionViewItem()
            option.rect = QtCore.QRect(0, 0, theme.NAV_WIDTH, theme.MIN_TARGET)
            option.state = QtWidgets.QStyle.StateFlag.State_Enabled
            option.state |= (QtWidgets.QStyle.StateFlag.State_Selected if selected
                             else QtWidgets.QStyle.StateFlag.State_None)
            pixmap = QtGui.QPixmap(theme.NAV_WIDTH, theme.MIN_TARGET)
            pixmap.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pixmap)
            delegate.paint(painter, option, index)
            painter.end()
            return pixmap.toImage()

        def bluish_pixels(image):
            count = 0
            for y in range(image.height()):
                for x in range(image.width()):
                    color = image.pixelColor(x, y)
                    if color.alpha() > 0 and color.blue() > color.red() + 20:
                        count += 1
            return count

        selected, plain = bluish_pixels(render(True)), bluish_pixels(render(False))
        assert selected > 0, "the selected row must show the translucent accent capsule"
        assert plain == 0, "an unselected row must stay plain"

    def test_the_rail_sits_on_a_glass_panel(self, window):
        panel = window.nav_panel
        assert panel.objectName() == "NavPanel"
        assert panel.testAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground)
        assert panel.maximumWidth() == theme.NAV_WIDTH
        stylesheet = theme.build_stylesheet(False)
        assert "QFrame#NavPanel" in stylesheet
        assert theme.LIGHT.glass in stylesheet


class TestFloatingActionBars:
    def test_both_transfer_pages_have_one(self, window):
        for view in (window.backup, window.restore):
            assert isinstance(view.action_bar, FloatingBar)
            for button in (view.start_button, view.pause_button, view.resume_button,
                           view.cancel_button):
                assert button.parent() is view.action_bar, "the buttons moved into the bar"

    def test_the_bar_lifts_only_while_a_run_is_live(self, window):
        bar = window.backup.action_bar
        for state in (None, "starting", "running", "pausing", "paused", "resuming",
                      "completed", "completed_with_errors", "failed", "cancelled"):
            window.backup._apply_state(state)
            expected = state in ("starting", "running", "pausing", "paused", "resuming")
            assert bar.is_lifted() is expected, f"state {state!r} lifted={bar.is_lifted()}"

    def test_the_restore_bar_follows_the_same_rule(self, window):
        bar = window.restore.action_bar
        window.restore._apply_state("paused")
        assert bar.is_lifted()
        window.restore._apply_state("completed")
        assert not bar.is_lifted()

    def test_each_bar_carries_exactly_one_restrained_shadow(self, window):
        for view in (window.backup, window.restore):
            effects = [view.action_bar.graphicsEffect()]
            effect = effects[0]
            assert isinstance(effect, QtWidgets.QGraphicsDropShadowEffect)
            assert 0 < effect.color().alpha() < 160, "a restrained shadow, not a glow"
            assert 8 <= effect.blurRadius() <= 48
            # No shadow anywhere else in the page (the budget is two in the app).
            for child in view.findChildren(QtWidgets.QWidget):
                if child is not view.action_bar:
                    assert not isinstance(child.graphicsEffect(),
                                          QtWidgets.QGraphicsDropShadowEffect), child

    def test_lifting_deepens_the_shadow_towards_its_final_state(self, window):
        bar = window.backup.action_bar
        bar.set_lifted(False)
        idle = (bar.graphicsEffect().blurRadius(), bar.graphicsEffect().color().alpha())
        bar.set_lifted(True)
        lifted = (bar.graphicsEffect().blurRadius(), bar.graphicsEffect().color().alpha())
        assert lifted[0] > idle[0] and lifted[1] >= idle[1]


class TestScrimAndDialogs:
    def test_the_scrim_covers_the_window_and_nests(self, window):
        with with_scrim(window) as scrim:
            assert isinstance(scrim, Scrim)
            assert not scrim.isHidden(), "the scrim is up"
            assert scrim.geometry() == window.rect()
            with with_scrim(window) as inner:
                assert inner is scrim and scrim.depth == 2
            assert not scrim.isHidden(), "still up while the outer modal is open"
        assert scrim.isHidden() and scrim.depth == 0

    def test_the_scrim_never_swallows_input(self, window):
        with with_scrim(window) as scrim:
            assert scrim.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert scrim.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus

    def test_present_blocking_returns_the_result_and_cleans_up(self, window):
        """The modal helper still blocks, returns the result and cleans up."""
        dialog = QtWidgets.QDialog(window)
        QtCore.QTimer.singleShot(0, dialog.accept)   # answer as soon as it is up
        assert present_blocking(dialog, window) == QtWidgets.QDialog.DialogCode.Accepted
        scrim = getattr(window, "_teloude_scrim", None)
        assert scrim is not None and scrim.isHidden() and scrim.depth == 0

    def test_the_sign_in_wizard_uses_the_glass_card(self, window):
        from teloude.ui.auth_dialog import AuthDialog

        dialog = AuthDialog(window._ctx.services.auth, window)
        assert isinstance(dialog.card, GlassCard)
        assert dialog.card.objectName() == "GlassCard"
        # the card, not the dialog, holds the wizard
        assert dialog.stack.parent() is dialog.card
        assert dialog.status_label.parent() is dialog.card
        # unchanged behaviour: the three steps are still there, in order
        assert dialog.stack.count() == 3
        assert dialog.phone_edit.text() == "+"

    def test_a_parented_wizard_dims_the_window_behind_it(self, window):
        from teloude.ui.auth_dialog import AuthDialog

        dialog = AuthDialog(window._ctx.services.auth, window)
        dialog.show()
        scrim = getattr(window, "_teloude_scrim", None)
        assert scrim is not None and not scrim.isHidden() and scrim.depth == 1
        dialog.done(QtWidgets.QDialog.DialogCode.Rejected)
        assert scrim.isHidden() and scrim.depth == 0


class TestEightPointGrid:
    def _layouts(self, widget):
        """Every layout under the window, nested layouts included (like theme)."""
        pending, seen = [widget], set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            layout = current.layout()
            if layout is not None:
                nested = [layout]
                while nested:
                    inner = nested.pop()
                    yield current, inner
                    nested.extend(child for child in inner.children()
                                  if isinstance(child, QtWidgets.QLayout))
            for child in current.children():
                if isinstance(child, QtWidgets.QWidget):
                    pending.append(child)

    def test_every_layout_in_the_window_is_on_the_grid(self, window):
        checked = 0
        for owner, layout in self._layouts(window):
            margins = layout.contentsMargins()
            for name, value in (("left", margins.left()), ("top", margins.top()),
                                ("right", margins.right()), ("bottom", margins.bottom())):
                assert value % theme.BASE == 0, f"{type(owner).__name__} {name}={value}"
            if layout.spacing() >= 0:
                assert layout.spacing() % theme.BASE == 0, (
                    f"{type(owner).__name__} spacing={layout.spacing()}"
                )
            if isinstance(layout, QtWidgets.QFormLayout):
                for spacing in (layout.horizontalSpacing(), layout.verticalSpacing()):
                    if spacing >= 0:
                        assert spacing % theme.BASE == 0, (
                            f"{type(owner).__name__} form spacing={spacing}"
                        )
            checked += 1
        assert checked > 20, "the walk must actually reach the pages"

    def test_snapping_keeps_zero_and_rounds_to_the_grid(self):
        assert theme.snap(0) == 0
        assert theme.snap(1) == 8
        assert theme.snap(9) == 8
        assert theme.snap(13) == 16
        assert theme.snap(24) == 24

    def test_normalizing_never_reorders_or_removes_widgets(self, light_theme):
        holder = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(holder)
        outer.setContentsMargins(9, 11, 13, 15)
        outer.setSpacing(6)
        inner = QtWidgets.QHBoxLayout()
        buttons = [QtWidgets.QPushButton(f"b{i}") for i in range(3)]
        for button in buttons:
            inner.addWidget(button)
        outer.addLayout(inner)
        theme.normalize_layout_spacing(holder)
        margins = holder.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (8, 8, 16, 16)
        assert outer.spacing() == 8
        assert [holder.layout().itemAt(0).layout().itemAt(i).widget() for i in range(3)] == buttons


class TestAppearanceControl:
    def test_the_settings_page_switches_and_stores_the_appearance(self, window):
        view = window.settings
        index = view.appearance_combo.findData("dark")
        view.appearance_combo.setCurrentIndex(index)
        assert window._ctx.services.settings.get("ui.appearance") == "dark"
        assert theme.tokens().name == "dark"
        assert theme.DARK.canvas in QtWidgets.QApplication.instance().styleSheet()

        view.appearance_combo.setCurrentIndex(view.appearance_combo.findData("light"))
        assert window._ctx.services.settings.get("ui.appearance") == "light"
        assert theme.tokens().name == "light"
        assert theme.LIGHT.canvas in QtWidgets.QApplication.instance().styleSheet()

    def test_a_fresh_profile_shows_light(self, window):
        assert window.settings.appearance_combo.currentData() == "light"
