"""Launch screen.

The first screen the user sees.  Two large, equal-weight hero buttons:

  • Real-Time Analysis — connect to a live device and start streaming
  • Post-Analysis      — load a recorded data file and analyse offline

Emits ``mode_selected("realtime" | "post_analysis")`` on click.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class _HeroButton(QPushButton):
    """Large card-style hero button — number + title + subtitle, styled via QSS."""

    def __init__(self, number: str, title: str, subtitle: str,
                 variant: str = "primary", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("hero" if variant == "primary" else "heroAlt")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        # Large numeral — styled via QSS descendant selector
        num_lbl = QLabel(number)
        num_lbl.setObjectName("heroNumber")
        layout.addWidget(num_lbl)

        layout.addSpacing(4)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("heroTitle")
        layout.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("heroSub")
        sub_lbl.setWordWrap(True)
        layout.addWidget(sub_lbl)

        layout.addStretch()


class LaunchScreen(QWidget):
    """Entry screen of the DAQ application."""

    mode_selected = Signal(str)   # "realtime" | "post_analysis"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Centered content wrapper.
        wrapper = QWidget()
        wrapper.setMaximumWidth(820)
        root.addStretch(1)
        outer = QHBoxLayout()
        outer.addStretch(1)
        outer.addWidget(wrapper)
        outer.addStretch(1)
        root.addLayout(outer, stretch=0)
        root.addStretch(1)

        col = QVBoxLayout(wrapper)
        col.setSpacing(28)
        col.setContentsMargins(40, 40, 40, 40)
        col.setAlignment(Qt.AlignHCenter)

        # Display headline.
        headline = QLabel("Choose how you'd like to start.")
        headline.setObjectName("display")
        headline.setAlignment(Qt.AlignHCenter)
        col.addWidget(headline)

        sub = QLabel(
            "Stream from a live MCU over USB, BLE, Wi-Fi, or CAN bus — "
            "or open a recording to analyse what's already on disk."
        )
        sub.setObjectName("subtle")
        sub.setAlignment(Qt.AlignHCenter)
        sub.setWordWrap(True)
        col.addWidget(sub)

        col.addSpacing(20)

        # ── Two hero buttons ──
        buttons = QHBoxLayout()
        buttons.setSpacing(20)

        self.btn_realtime = _HeroButton(
            "01",
            "Real-Time Analysis",
            "Connect to a live MCU over USB, BLE, Wi-Fi, or CAN bus — or stream "
            "from a live CSV / Parquet file — for real-time monitoring, recording, "
            "and visual analysis.",
            variant="primary",
        )
        self.btn_realtime.clicked.connect(
            lambda: self.mode_selected.emit("realtime")
        )
        buttons.addWidget(self.btn_realtime, stretch=1)

        self.btn_post = _HeroButton(
            "02",
            "Post-Analysis",
            "Open a previously recorded CSV or Parquet file and explore its "
            "channels with the full workspace — same tools you use during live sessions.",
            variant="alt",
        )
        self.btn_post.clicked.connect(
            lambda: self.mode_selected.emit("post_analysis")
        )
        buttons.addWidget(self.btn_post, stretch=1)

        col.addLayout(buttons)

        # Footer caption.
        col.addSpacing(20)
        footer = QLabel(
            "All sessions use a PC-monotonic clock for cross-device alignment.  "
            "Recorded data and layouts are saved separately so you can replay "
            "any run with its exact visual settings."
        )
        footer.setObjectName("caption")
        footer.setAlignment(Qt.AlignHCenter)
        footer.setWordWrap(True)
        col.addWidget(footer)
