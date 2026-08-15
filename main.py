"""Multi-Protocol Bus Analyzer — application entry point."""

from __future__ import annotations

import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from engine.device_registry import DeviceRegistry
from ui.main_window import MainWindow, apply_enterprise_theme


def main() -> int:
    # Load the device registry before building any UI so the first
    # metadata_ready signal can look up display names immediately.
    registry = DeviceRegistry()
    registry.load()

    pg.setConfigOptions(
        antialias=False,
        useOpenGL=False,
        background="#121212",
        foreground="#dddddd",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Multi-Protocol Bus Analyzer")
    apply_enterprise_theme(app)

    window = MainWindow(registry=registry)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
