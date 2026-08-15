"""WorkspaceWidget — advanced visualization workspace.

Uses pyqtgraph's DockArea so every PlotPanel lives in an independently
resizable, movable, and *tearable* Dock.  Tearing a Dock turns it into a
floating OS window that can be dragged to a second monitor.

Key public API
--------------
WorkspaceBuilderDialog  — "How many plots? What layout?" modal.
WorkspaceWidget         — the DockArea + all PlotPanels + 60 Hz refresh.

Integration with MainWindow
---------------------------
1.  MainWindow creates a WorkspaceWidget and embeds it in the data page.
2.  On ``device_ready``  →  ``workspace.auto_add_device(path, metadata, label)``
3.  On ``device_removed`` → ``workspace.remove_device(path)``
4.  60 Hz timer calls   ``workspace.refresh(session_manager)``
5.  On save/load        →  ``workspace.to_config()`` / ``workspace.from_config(d)``
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pyqtgraph as pg
from pyqtgraph.dockarea import Dock, DockArea
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtCore import QObject
from engine.protocol import DeviceMetadataV2
from engine.session_serializer import (
    default_plot_config,
    load_session_config,
    save_session_config,
)
from ui.plot_panel import (
    ChannelStyleConfig,
    PlotPanel,
    _DEFAULT_COLORS,
)
try:
    from ui.vispy_plot_panel import VispyPlotPanel
    _VISPY_AVAILABLE = True
except Exception:
    _VISPY_AVAILABLE = False


# ── Workspace Builder dialog ──────────────────────────────────────────────────

class WorkspaceBuilderDialog(QDialog):
    """Modal asking the user how to configure their plot workspace.

    Returns via ``accepted()`` → ``n_plots`` / ``layout_mode`` properties.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure Workspace")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel(
            "Choose how many independent plot panels to open.\n"
            "Each panel can be dragged to a second monitor."
        ))

        # ── Number of plots ──
        count_grp = QGroupBox("Number of plots")
        count_layout = QHBoxLayout(count_grp)
        self._count_spin = QSpinBox()
        self._count_spin.setRange(1, 6)
        self._count_spin.setValue(2)
        count_layout.addWidget(self._count_spin)
        count_layout.addWidget(QLabel("independent panels"))
        count_layout.addStretch()
        layout.addWidget(count_grp)

        # ── Initial layout ──
        layout_grp = QGroupBox("Initial layout")
        layout_vbox = QVBoxLayout(layout_grp)
        self._layout_bg = QButtonGroup(self)
        for key, desc in (
            ("side_by_side",  "Side by side (split horizontally)"),
            ("stacked",       "Stacked (split vertically)"),
            ("tabs",          "Tabbed (save screen space)"),
        ):
            rb = QRadioButton(desc)
            rb.setChecked(key == "side_by_side")
            rb.setProperty("layout_key", key)
            self._layout_bg.addButton(rb)
            layout_vbox.addWidget(rb)
        layout.addWidget(layout_grp)

        # ── Buttons ──
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @property
    def n_plots(self) -> int:
        return self._count_spin.value()

    @property
    def layout_mode(self) -> str:
        checked = self._layout_bg.checkedButton()
        return checked.property("layout_key") if checked else "side_by_side"


# ── WorkspaceWidget ───────────────────────────────────────────────────────────

class WorkspaceWidget(QWidget):
    """DockArea hosting multiple PlotPanels with full session management.

    Aggregates ``alarm_triggered`` from every PlotPanel and re-emits as
    ``alarm_triggered(panel_title, alarm_name, channel_name, value, ts)``
    so the SettingsPanel alarm log can receive from all plots at once.
    """

    # panel_title, alarm_name, channel_name, value, pc_timestamp
    alarm_triggered = Signal(str, str, str, float, float)

    # Emitted whenever the set of panels changes (add, remove, restore from config).
    panels_changed = Signal()

    # Thread-safety: refresh() runs in the UI thread at 60 Hz.
    # Each PlotPanel.refresh() only reads ring buffers (protected by their
    # own locks) and calls PyQtGraph setData — no additional locking needed.

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._panels: List[PlotPanel]        = []
        self._docks:  List[Dock]             = []
        self._panel_counter: int             = 0
        self._device_label: str = ""
        self._device_connected: bool = False
        self._recording: bool = False
        self._recording_elapsed: float = 0.0

        # regression comparison: a second "baseline" session loaded
        # alongside the current one.  Held as a DataFrame for statistical diffing
        # and rendered as faded dashed overlays.  None until the user loads one.
        self._baseline_df = None
        self._baseline_name: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._dock_area = DockArea()
        layout.addWidget(self._dock_area, stretch=1)

    def update_device_status(self, label: str, connected: bool) -> None:
        self._device_label     = label
        self._device_connected = connected

    def update_recording_status(self, recording: bool, elapsed_s: float = 0.0) -> None:
        self._recording = recording
        self._recording_elapsed = elapsed_s

    def _refresh_status_hz(self, session_manager) -> None:
        # Status segments were removed; kept as a no-op so any leftover callers
        # don't crash. Live Hz / channel counts are visible per-panel.
        return

    # ── Public API ────────────────────────────────────────────────────────────

    def add_plot(self, title: str = "", backend: str = "pyqtgraph") -> PlotPanel:
        """Create a new plot panel inside a new Dock.

        *backend* can be ``"pyqtgraph"`` (default, full features) or
        ``"vispy"`` (GPU-accelerated, single-axis, high sample-rate).
        Falls back to pyqtgraph if Vispy is unavailable.
        """
        self._panel_counter += 1
        panel_id = f"plot_{self._panel_counter}"
        if not title:
            title = f"Plot {self._panel_counter}"

        if backend == "vispy" and _VISPY_AVAILABLE:
            panel = VispyPlotPanel(panel_id, title, parent=None)
        else:
            panel = PlotPanel(panel_id, title, show_toolbar=False, parent=None)

        dock = Dock(title, size=(640, 480), closable=True)
        dock.addWidget(panel)

        if not self._docks:
            self._dock_area.addDock(dock, "left")
        else:
            self._dock_area.addDock(dock, "right", self._docks[-1])

        self._panels.append(panel)
        self._docks.append(dock)
        panel.alarm_triggered.connect(
            lambda an, ch, val, ts, pt=title:
                self.alarm_triggered.emit(pt, an, ch, val, ts)
        )
        return panel

    def set_panel_visible(self, panel, visible: bool) -> None:
        """Show or hide the dock that contains *panel*."""
        try:
            idx = self._panels.index(panel)
            self._docks[idx].setVisible(visible)
        except ValueError:
            pass

    def add_plots_with_layout(self, n: int, layout_mode: str) -> List[PlotPanel]:
        """Create *n* plots arranged according to *layout_mode*."""
        panels = []
        for i in range(n):
            title = f"Plot {self._panel_counter + 1}"
            panel = self._add_panel_raw(title)
            panels.append(panel)

        if layout_mode == "tabs" and len(self._docks) >= 2:
            for dock in self._docks[1:]:
                self._dock_area.moveDock(dock, "above", self._docks[0])
        elif layout_mode == "stacked" and len(self._docks) >= 2:
            for i in range(1, len(self._docks)):
                self._dock_area.moveDock(
                    self._docks[i], "bottom", self._docks[i - 1]
                )
        # "side_by_side" is the default — panels already placed right-of-prev.

        return panels

    def _add_panel_raw(self, title: str, backend: str = "pyqtgraph") -> PlotPanel:
        """Internal: create panel + dock, append to lists."""
        self._panel_counter += 1
        panel_id = f"plot_{self._panel_counter}"
        if backend == "vispy" and _VISPY_AVAILABLE:
            panel = VispyPlotPanel(panel_id, title, parent=None)
        else:
            panel = PlotPanel(panel_id, title, show_toolbar=False, parent=None)

        dock = Dock(title, size=(640, 480), closable=True)
        dock.addWidget(panel)

        if not self._docks:
            self._dock_area.addDock(dock, "left")
        else:
            self._dock_area.addDock(dock, "right", self._docks[-1])

        self._panels.append(panel)
        self._docks.append(dock)
        # Aggregate alarm signal from this panel.
        panel.alarm_triggered.connect(
            lambda an, ch, val, ts, pt=title:
                self.alarm_triggered.emit(pt, an, ch, val, ts)
        )
        self.panels_changed.emit()
        return panel

    def rename_panel(self, panel: PlotPanel, new_title: str) -> None:
        """Update both the PlotPanel's title and the Dock label that holds it."""
        new_title = (new_title or "").strip()
        if not new_title or panel not in self._panels:
            return
        idx = self._panels.index(panel)
        panel._title = new_title
        if hasattr(panel, "_title_edit"):
            try:
                panel._title_edit.blockSignals(True)
                panel._title_edit.setText(new_title)
                panel._title_edit.blockSignals(False)
            except Exception:
                pass
        try:
            self._docks[idx].setTitle(new_title)
        except Exception:
            try:
                self._docks[idx].label.setText(new_title)
            except Exception:
                pass
        self.panels_changed.emit()

    def get_panels(self) -> List[PlotPanel]:
        return list(self._panels)

    def first_panel(self) -> Optional[PlotPanel]:
        return self._panels[0] if self._panels else None

    def get_all_channels(self) -> List[Tuple[str, str]]:
        """Return all unique (device_path, channel_name) pairs across all panels."""
        seen: set = set()
        result: List[Tuple[str, str]] = []
        for panel in self._panels:
            for cfg in panel._channels.values():
                key = (cfg.device_path, cfg.channel_name)
                if key not in seen:
                    seen.add(key)
                    result.append(key)
        return result

    # ── Device lifecycle integration ──────────────────────────────────────────

    def auto_add_device(
        self,
        device_path: str,
        metadata: DeviceMetadataV2,
        label: str = "",
    ) -> None:
        """Auto-assign all channels from a freshly-ready device to the first
        available plot panel with default visual settings.

        This preserves the 'just works' behavior from the pre-Step-7 UI
        while allowing manual reconfiguration via the Configure dialog.
        """
        panel = self.first_panel()
        if panel is None:
            panel = self.add_plot(f"Plot {self._panel_counter + 1}")

        # Ensure axis 0 exists.
        if panel.n_y_axes == 0:
            panel.add_y_axis()

        base_color_idx = len(panel._channels)
        for i, ch in enumerate(metadata.channels):
            color = _DEFAULT_COLORS[(base_color_idx + i) % len(_DEFAULT_COLORS)]
            legend_name = (f"{label} · {ch.name}" if label else ch.name)
            panel.add_channel(
                device_path,
                ch.name,
                y_axis_idx=0,
                color=color,
            )

        # Refresh the X-combo in all panels with up-to-date channel list.
        self._rebuild_all_x_combos(metadata, device_path, label)

    def clear(self) -> None:
        """Remove all plots and docks, resetting the workspace to empty."""
        for dock in list(self._docks):
            try:
                dock.close()
            except Exception:
                pass
        self._panels.clear()
        self._docks.clear()
        self._panel_counter = 0

    def remove_device(self, device_path: str) -> None:
        """Remove all live channels belonging to *device_path* from all panels."""
        for panel in self._panels:
            keys_to_remove = [
                k for k, cfg in panel._channels.items()
                if cfg.device_path == device_path
            ]
            for key in keys_to_remove:
                panel.remove_channel(key)

    def _rebuild_all_x_combos(
        self,
        metadata: DeviceMetadataV2,
        device_path: str,
        device_label: str,
    ) -> None:
        choices: List[Tuple[str, str, str]] = []
        for ch in metadata.channels:
            disp = (f"{device_label} · {ch.name}"
                    if device_label else ch.name)
            choices.append((device_path, ch.name, disp))
        for panel in self._panels:
            # Merge with what's already in the combo rather than rebuilding.
            current_data = {
                panel._x_combo.itemData(i)
                for i in range(panel._x_combo.count())
            }
            for dev_p, ch_n, label in choices:
                ud = (dev_p, ch_n)
                if ud not in current_data:
                    panel._x_combo.addItem(label, userData=ud)

    # ── baseline (regression comparison) ────────────────────────────

    def load_baseline_session(self, path: str) -> bool:
        """Load a second session as a faded baseline alongside the current one.

        Reads every data file in the session folder into one DataFrame (held
        for :meth:`AnalysisFacade.compare_sessions`) and renders its channels
        as 50%-opacity dashed overlays *behind* the live/post-analysis data on
        the first panel.  Returns ``True`` when at least one channel loaded.
        Does not disturb the existing single-session post-analysis workflow.
        """
        import os
        import numpy as np
        import pandas as pd
        from engine.session_folder import SessionFolder

        data_files: List[str] = []
        if os.path.isdir(path) and SessionFolder.is_session_folder(path):
            try:
                doc = SessionFolder.load_folder(path)
            except Exception:
                doc = {}
            for rel in (doc.get("data_files", {}) or {}).values():
                abs_path = os.path.join(path, rel)
                if os.path.exists(abs_path):
                    data_files.append(abs_path)
        if not data_files and os.path.isdir(path):
            for fname in sorted(os.listdir(path)):
                if fname.startswith("data_") and fname.endswith(
                    (".csv", ".parquet", ".pq")
                ):
                    data_files.append(os.path.join(path, fname))
        if not data_files:
            return False

        series: Dict[str, "np.ndarray"] = {}
        time_arr = None
        for fpath in data_files:
            try:
                if fpath.endswith((".parquet", ".pq")):
                    df = pd.read_parquet(fpath)
                else:
                    df = pd.read_csv(
                        fpath, on_bad_lines="skip", dtype=str,
                        low_memory=False, encoding_errors="replace",
                    )
            except Exception:
                continue
            cols = list(df.columns)
            if len(cols) < 2:
                continue
            x = pd.to_numeric(df[cols[0]], errors="coerce").to_numpy(dtype=float)
            if time_arr is None:
                time_arr = x
            for col in cols[1:]:
                bare = col.split("[", 1)[0].strip() if "[" in col else col
                if bare in series:
                    continue
                series[bare] = pd.to_numeric(
                    df[col], errors="coerce"
                ).to_numpy(dtype=float)
        if not series:
            return False

        n = min(len(v) for v in series.values())
        if time_arr is not None:
            n = min(n, len(time_arr))
        data = {k: v[:n] for k, v in series.items()}
        bdf = pd.DataFrame(data)
        bdf.insert(
            0, "time",
            time_arr[:n] if time_arr is not None else np.arange(n, dtype=float),
        )
        self._baseline_df = bdf
        self._baseline_name = os.path.basename(path.rstrip(os.sep)) or "baseline"

        # Render faded overlays on the first panel (best-effort).
        panels = self.get_panels()
        if panels:
            for fpath in data_files:
                try:
                    panels[0].load_overlay(fpath, faded=True, clear=False)
                except Exception:
                    pass
        return True

    def baseline_name(self) -> str:
        return self._baseline_name

    def has_baseline(self) -> bool:
        return self._baseline_df is not None and len(self._baseline_df) > 0

    def clear_baseline(self) -> None:
        self._baseline_df = None
        self._baseline_name = ""

    # ── 60 Hz refresh ────────────────────────────────────────────────────────

    def refresh(self, session_manager) -> None:
        """Delegate refresh to every live plot panel."""
        for panel in self._panels:
            panel.refresh(session_manager)
        self._refresh_status_hz(session_manager)

    # ── Session serialization ─────────────────────────────────────────────────

    def to_config(self) -> List[dict]:
        """Return a list of plot-config dicts for all current panels."""
        return [p.to_config() for p in self._panels]

    def from_config(self, plots: List[dict], merge: bool = False) -> None:
        """Restore workspace from a list of plot-config dicts.

        When *merge* is False (default) the workspace is cleared first.
        When *merge* is True the new plots are added on top of any existing
        panels — used when a second device connects and the user wants to
        keep the first device's plots intact.
        """
        if not merge:
            for dock in self._docks:
                dock.close()
            self._panels.clear()
            self._docks.clear()
            self._panel_counter = 0

        for d in plots:
            title = d.get("title", f"Plot {self._panel_counter + 1}")
            panel = self._add_panel_raw(title)
            panel.restore_config(d)
        self.panels_changed.emit()

    def save_workspace_config(self, path: str) -> str:
        """Serialize workspace to JSON at *path*.  Returns the saved path."""
        return save_session_config(self.to_config(), path)

    def load_workspace_config(self, path: str) -> None:
        """Load a JSON workspace config from *path*."""
        doc = load_session_config(path)
        self.from_config(doc.get("plots", []))

    # ── Convenience save/load via dialogs ─────────────────────────────────────

    def prompt_save_config(self, parent: QWidget) -> Optional[str]:
        """Open a Save-As dialog and write the workspace config.  Returns path."""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        default = f"session_config_{ts}.json"
        path, _ = QFileDialog.getSaveFileName(
            parent,
            "Save Workspace Config",
            default,
            "JSON (*.json)",
        )
        if not path:
            return None
        if not path.endswith(".json"):
            path += ".json"
        saved = self.save_workspace_config(path)
        QMessageBox.information(
            parent, "Saved", f"Workspace config saved to:\n{saved}"
        )
        return saved

    def prompt_load_config(self, parent: QWidget) -> None:
        """Open a file picker and restore the workspace config."""
        path, _ = QFileDialog.getOpenFileName(
            parent,
            "Load Workspace Config",
            "",
            "JSON (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            self.load_workspace_config(path)
        except Exception as exc:
            QMessageBox.warning(
                parent, "Load Error", f"Could not load config:\n{exc}"
            )
