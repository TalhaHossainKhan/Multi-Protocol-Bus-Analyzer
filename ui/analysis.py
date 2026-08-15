"""Post-Analysis Manager.

Allows the user to load a historical CSV/Parquet recording and render it
as a background overlay on any live-streaming PlotPanel.  The visual
settings (channel colours, axis assignments, thresholds) are optionally
restored from a matching ``session_config.json`` file.

Workflow
--------
1.  User opens the Post-Analysis Manager from the data-page toolbar.
2.  Picks a data file (CSV / Parquet).
3.  Optionally loads a matching ``session_config.json`` for exact visual
    replay.
4.  Selects which active plot panel should receive the overlay.
5.  Clicks "Apply Overlay" — historical data appears behind the live stream
    using dashed, semi-transparent curves.

Live comparison
---------------
Because the overlay curves are rendered in the background and live curves
are updated in real time on top, the user can visually compare a previous
test run against the current one without any additional UI.
"""

from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from engine.session_serializer import load_session_config
from ui.plot_panel import ChannelStyleConfig, PlotPanel


class PostAnalysisManager(QDialog):
    """Dialog for loading historical data as a live-overlay background.

    Accepts a list of currently active PlotPanels so the user can choose
    which panel should receive the overlay.
    """

    def __init__(self, panels: List[PlotPanel], parent=None) -> None:
        super().__init__(parent)
        self._panels = panels
        self.setWindowTitle("Post-Analysis Manager")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ── Data file ──────────────────────────────────────────────────────
        data_grp = QGroupBox("Historical data file")
        data_form = QFormLayout(data_grp)

        self._data_edit = QLineEdit()
        self._data_edit.setReadOnly(True)
        self._data_edit.setPlaceholderText("No file selected")
        btn_data = QPushButton("Browse…")
        btn_data.clicked.connect(self._pick_data)
        data_row = QHBoxLayout()
        data_row.addWidget(self._data_edit, stretch=1)
        data_row.addWidget(btn_data)
        data_form.addRow("File:", data_row)

        layout.addWidget(data_grp)

        # ── Config file (optional) ─────────────────────────────────────────
        cfg_grp = QGroupBox("Visual config (optional)")
        cfg_form = QFormLayout(cfg_grp)

        self._cfg_edit = QLineEdit()
        self._cfg_edit.setReadOnly(True)
        self._cfg_edit.setPlaceholderText("No config selected (use default style)")
        btn_cfg = QPushButton("Browse…")
        btn_cfg.clicked.connect(self._pick_config)
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self._cfg_edit, stretch=1)
        cfg_row.addWidget(btn_cfg)
        cfg_form.addRow("session_config.json:", cfg_row)

        layout.addWidget(cfg_grp)

        # ── Target plot panel ──────────────────────────────────────────────
        target_grp = QGroupBox("Apply overlay to")
        target_form = QFormLayout(target_grp)

        self._panel_combo = QComboBox()
        for p in panels:
            self._panel_combo.addItem(p.title, userData=p)
        target_form.addRow("Plot panel:", self._panel_combo)

        layout.addWidget(target_grp)

        # ── Status label ──────────────────────────────────────────────────
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        # ── Action buttons ────────────────────────────────────────────────
        btn_apply  = QPushButton("Apply Overlay")
        btn_apply.clicked.connect(self._apply_overlay)
        btn_clear  = QPushButton("Clear Overlay")
        btn_clear.clicked.connect(self._clear_overlay)
        btn_close  = QDialogButtonBox(QDialogButtonBox.Close)
        btn_close.rejected.connect(self.accept)

        btn_row = QHBoxLayout()
        btn_row.addWidget(btn_apply)
        btn_row.addWidget(btn_clear)
        layout.addLayout(btn_row)
        layout.addWidget(btn_close)

    # ── File pickers ──────────────────────────────────────────────────────────

    def _pick_data(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Historical Data File",
            os.path.expanduser("~/Documents"),
            "Data Files (*.csv *.tsv *.parquet *.pq);;All Files (*)",
        )
        if path:
            self._data_edit.setText(path)
            self._status_label.setText(f"Data file set: {os.path.basename(path)}")

    def _pick_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Session Config",
            os.path.expanduser("~/Documents"),
            "JSON (*.json);;All Files (*)",
        )
        if path:
            self._cfg_edit.setText(path)
            self._status_label.setText(
                f"Config file set: {os.path.basename(path)}"
            )

    # ── Overlay apply / clear ─────────────────────────────────────────────────

    def _apply_overlay(self) -> None:
        data_path = self._data_edit.text().strip()
        if not data_path:
            QMessageBox.warning(self, "No data", "Please select a data file first.")
            return

        panel: Optional[PlotPanel] = self._panel_combo.currentData()
        if panel is None:
            QMessageBox.warning(self, "No panel", "No active plot panel found.")
            return

        # Load optional visual config.
        cfg_path = self._cfg_edit.text().strip()
        channel_cfgs: Optional[List[ChannelStyleConfig]] = None

        if cfg_path:
            try:
                doc = load_session_config(cfg_path)
                plots = doc.get("plots", [])
                if plots:
                    channels_raw = plots[0].get("channels", [])
                    channel_cfgs = [
                        ChannelStyleConfig.from_dict(c) for c in channels_raw
                    ]
            except Exception as exc:
                QMessageBox.warning(
                    self, "Config Error",
                    f"Could not load session config:\n{exc}\n\n"
                    "The overlay will use default styles."
                )
                channel_cfgs = None

        try:
            panel.load_overlay(data_path, channel_cfgs)
            n_curves = len(panel._overlay_curves)
            self._status_label.setText(
                f"✓ Overlay applied to '{panel.title}': "
                f"{n_curves} channel(s) loaded."
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Overlay Error", f"Failed to apply overlay:\n{exc}"
            )

    def _clear_overlay(self) -> None:
        panel: Optional[PlotPanel] = self._panel_combo.currentData()
        if panel:
            panel.clear_overlay()
            self._status_label.setText(
                f"Overlay cleared from '{panel.title}'."
            )
