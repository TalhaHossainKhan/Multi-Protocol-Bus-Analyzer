"""Header-mapping dialog for the Live File Watcher source.

Shown by MainWindow immediately after LiveFileWorker emits
``headers_detected(device_path, col_names)``.  The user picks which
columns to stream and plot; the dialog returns the selection via
``selected_columns()``.
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


class ColumnMappingDialog(QDialog):
    """Multi-select list of detected file columns.

    The user must pick at least one column before OK is enabled.
    All columns are pre-selected on open so a single OK click accepts
    everything — the common case when plotting a small file.
    """

    def __init__(
        self,
        device_path: str,
        headers: List[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Columns to Plot")
        self.setMinimumWidth(460)
        self.setMinimumHeight(320)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Show the file path so the user can confirm which file was opened.
        fname = device_path.removeprefix("file://")
        file_label = QLabel(f"<b>File:</b> {fname}")
        file_label.setWordWrap(True)
        layout.addWidget(file_label)

        layout.addWidget(
            QLabel(
                "Select the data columns to stream and plot.\n"
                "Hold Ctrl / ⌘ to toggle individual items."
            )
        )

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.ExtendedSelection)
        for h in headers:
            self._list.addItem(QListWidgetItem(h))
        self._list.selectAll()
        self._list.itemSelectionChanged.connect(self._sync_ok_button)
        layout.addWidget(self._list, stretch=1)

        self._buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        ok = self._buttons.button(QDialogButtonBox.Ok)
        ok.setText("Select Data  →")
        ok.setObjectName("primary")
        ok.setMinimumWidth(180)
        ok.setMinimumHeight(36)
        self._buttons.setCenterButtons(True)
        self._buttons.accepted.connect(self.accept)
        layout.addWidget(self._buttons)

        self._sync_ok_button()

    def _sync_ok_button(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(bool(self._list.selectedItems()))

    def selected_columns(self) -> List[str]:
        """Return selected column names in their original file order."""
        selected_texts = {item.text() for item in self._list.selectedItems()}
        return [
            self._list.item(i).text()
            for i in range(self._list.count())
            if self._list.item(i).text() in selected_texts
        ]
