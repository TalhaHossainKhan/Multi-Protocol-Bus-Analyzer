"""Channel Manager — enterprise left-strip panel.

Shows all channels across all plot panels in a compact vertical list.
Each row displays the channel color, name, plot+axis badge, and a
visibility toggle.  Right-clicking a row shows the same context menu
as the legend right-click in the plot itself.

Subscribes to ``panel.channel_axis_changed`` from every panel so the list
stays in sync whenever axis assignments change via the legend or Settings.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class _ChannelRow(QFrame):
    """One row in the channel manager list."""

    visibility_toggled  = Signal(str, bool)    # key, visible
    color_changed       = Signal(str, str)     # key, new_hex_color
    move_to_axis_requested = Signal(str, object)  # key, axis_target (int or "new")
    remove_requested    = Signal(str)           # key

    def __init__(self, key: str, channel_name: str, device_label: str,
                 plot_num: int, axis_num: int, color: str,
                 visible: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chManagerRow")
        self._key     = key
        self._visible = visible
        self._color   = color

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        # Color chip — click to change color.
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(14, 14)
        self._color_btn.setToolTip("Click to change channel colour")
        self._color_btn.clicked.connect(self._pick_color)
        self._apply_color()
        layout.addWidget(self._color_btn)

        # Channel name.
        self._name_label = QLabel(channel_name)
        self._name_label.setToolTip(f"{channel_name}  [device: {device_label}]")
        self._name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._name_label)

        # Plot + axis badge.
        self._badge = QLabel(f"P{plot_num}·A{axis_num}")
        self._badge.setObjectName("chBadge")
        self._badge.setToolTip(f"Plot {plot_num}, Axis {axis_num}")
        layout.addWidget(self._badge)

        # Visibility toggle.
        self._eye_btn = QToolButton()
        self._eye_btn.setText("●" if visible else "○")
        self._eye_btn.setFixedSize(20, 20)
        self._eye_btn.setToolTip("Toggle channel visibility")
        self._eye_btn.clicked.connect(self._toggle_visibility)
        layout.addWidget(self._eye_btn)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ── Public update ────────────────────────────────────────────────────────

    def update_badge(self, plot_num: int, axis_num: int) -> None:
        self._badge.setText(f"P{plot_num}·A{axis_num}")
        self._badge.setToolTip(f"Plot {plot_num}, Axis {axis_num}")

    def update_color(self, color: str) -> None:
        self._color = color
        self._apply_color()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _apply_color(self) -> None:
        self._color_btn.setStyleSheet(
            f"background-color: {self._color}; "
            f"border: 1px solid #3a3a3a; border-radius: 2px;"
        )

    def _pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self)
        if c.isValid():
            self._color = c.name()
            self._apply_color()
            self.color_changed.emit(self._key, self._color)

    def _toggle_visibility(self) -> None:
        self._visible = not self._visible
        self._eye_btn.setText("●" if self._visible else "○")
        self.visibility_toggled.emit(self._key, self._visible)

    def _show_context_menu(self, pos) -> None:
        from ui.plot_panel import PlotPanel
        # Build "Move to axis" options from the panel — accessed via parent.
        menu = QMenu(self)

        # The parent ChannelManager can provide axis info.
        manager: Optional[ChannelManager] = None
        p = self.parent()
        while p is not None:
            if isinstance(p, ChannelManager):
                manager = p
                break
            p = p.parent()

        if manager is not None:
            panel = manager.panel_for_key(self._key)
            if panel is not None:
                axis_menu = menu.addMenu("Move to axis")
                for i, ax_cfg in enumerate(getattr(panel, "_y_axis_cfgs", [])):
                    side = "Left (primary)" if i == 0 else f"Right-{i}"
                    label_text = f"{side}  — {ax_cfg.label}" if ax_cfg.label else side
                    action = axis_menu.addAction(label_text)
                    action.setData(i)
                axis_menu.addSeparator()
                new_a = axis_menu.addAction("+ Add new Y-axis here")
                new_a.setData("new")

                menu.addSeparator()

        remove_action = menu.addAction("Remove from plot")
        hide_action   = menu.addAction("Hide" if self._visible else "Show")

        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is remove_action:
            self.remove_requested.emit(self._key)
        elif chosen is hide_action:
            self._toggle_visibility()
        elif chosen.data() is not None:
            self.move_to_axis_requested.emit(self._key, chosen.data())


class ChannelManager(QFrame):
    """Left-side collapsible channel strip.

    Attach to a WorkspaceWidget after creating it; subscribe to panels via
    ``subscribe_panel(panel, plot_index)``.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("channelManager")
        self.setMinimumWidth(180)
        self.setMaximumWidth(240)

        self._rows:       Dict[str, _ChannelRow] = {}   # key → row widget
        self._key_to_panel: Dict[str, object] = {}      # key → PlotPanel
        self._key_to_plot_idx: Dict[str, int] = {}      # key → plot display index
        self._collapsed = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header.
        header = QFrame(objectName="panelHeader")
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(10, 4, 6, 4)
        lbl = QLabel("CHANNELS")
        lbl.setObjectName("h3")
        hdr_layout.addWidget(lbl)
        hdr_layout.addStretch()

        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("◀")
        self._collapse_btn.setFixedSize(20, 20)
        self._collapse_btn.setToolTip("Collapse channel list")
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        hdr_layout.addWidget(self._collapse_btn)
        outer.addWidget(header)

        # Scrollable channel list.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._body_widget = QWidget()
        self._body_layout = QVBoxLayout(self._body_widget)
        self._body_layout.setContentsMargins(4, 4, 4, 4)
        self._body_layout.setSpacing(2)
        self._body_layout.addStretch()
        self._scroll.setWidget(self._body_widget)
        outer.addWidget(self._scroll, stretch=1)

    # ── Public API ────────────────────────────────────────────────────────────

    def subscribe_panel(self, panel, plot_idx: int) -> None:
        """Wire up signals for a panel and add its current channels."""
        panel.channel_axis_changed.connect(
            lambda key, axis, pi=plot_idx, p=panel: self._on_axis_changed(key, axis, pi, p)
        )
        for key, cfg in getattr(panel, "_channels", {}).items():
            self._add_row(key, cfg.channel_name, f"dev", plot_idx, cfg.y_axis_idx, cfg.color, panel)

    def panel_for_key(self, key: str):
        return self._key_to_panel.get(key)

    def remove_panel(self, panel) -> None:
        """Remove all rows belonging to *panel*."""
        to_remove = [k for k, p in self._key_to_panel.items() if p is panel]
        for key in to_remove:
            self._remove_row(key)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _add_row(self, key: str, channel_name: str, device_label: str,
                 plot_idx: int, axis_idx: int, color: str, panel) -> None:
        if key in self._rows:
            return
        row = _ChannelRow(
            key=key, channel_name=channel_name, device_label=device_label,
            plot_num=plot_idx + 1, axis_num=axis_idx, color=color,
        )
        row.visibility_toggled.connect(self._on_visibility_toggled)
        row.color_changed.connect(self._on_color_changed)
        row.move_to_axis_requested.connect(self._on_move_to_axis)
        row.remove_requested.connect(self._on_remove_requested)

        self._rows[key] = row
        self._key_to_panel[key] = panel
        self._key_to_plot_idx[key] = plot_idx

        # Insert before the trailing stretch.
        layout = self._body_layout
        count = layout.count()
        layout.insertWidget(count - 1, row)

    def _remove_row(self, key: str) -> None:
        row = self._rows.pop(key, None)
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        self._key_to_panel.pop(key, None)
        self._key_to_plot_idx.pop(key, None)

    def _on_axis_changed(self, key: str, axis: int, plot_idx: int, panel) -> None:
        if axis == -1:
            self._remove_row(key)
            return
        row = self._rows.get(key)
        if row is None:
            # Channel was added — add the row now.
            cfg = getattr(panel, "_channels", {}).get(key)
            if cfg:
                self._add_row(key, cfg.channel_name, "dev", plot_idx, axis, cfg.color, panel)
        else:
            row.update_badge(plot_idx + 1, axis)

    def _on_visibility_toggled(self, key: str, visible: bool) -> None:
        panel = self._key_to_panel.get(key)
        if panel and hasattr(panel, "set_channel_visible"):
            panel.set_channel_visible(key, visible)
        elif panel and hasattr(panel, "_live_curves"):
            curve = panel._live_curves.get(key)
            if curve:
                curve.setVisible(visible)

    def _on_color_changed(self, key: str, color: str) -> None:
        panel = self._key_to_panel.get(key)
        if panel is None:
            return
        cfg = getattr(panel, "_channels", {}).get(key)
        if cfg:
            from ui.plot_panel import ChannelStyleConfig
            import copy
            new_cfg = copy.copy(cfg)
            new_cfg.color = color
            panel.reconfigure_channel(key, new_cfg)
        row = self._rows.get(key)
        if row:
            row.update_color(color)

    def _on_move_to_axis(self, key: str, target) -> None:
        panel = self._key_to_panel.get(key)
        if panel and hasattr(panel, "move_channel_to_axis"):
            panel.move_channel_to_axis(key, target)

    def _on_remove_requested(self, key: str) -> None:
        panel = self._key_to_panel.get(key)
        if panel and hasattr(panel, "remove_channel"):
            panel.remove_channel(key)
        self._remove_row(key)

    def _toggle_collapse(self) -> None:
        self._collapsed = not self._collapsed
        self._scroll.setVisible(not self._collapsed)
        self.setMaximumWidth(24 if self._collapsed else 240)
        self._collapse_btn.setText("▶" if self._collapsed else "◀")
        self._collapse_btn.setToolTip(
            "Expand channel list" if self._collapsed else "Collapse channel list"
        )
