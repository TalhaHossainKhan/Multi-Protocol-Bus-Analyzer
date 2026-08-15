"""Dark theme — Multi-Protocol Bus Analyzer.

Single source of truth for the visual identity.  Applied once at startup via
``app.setStyleSheet(ENTERPRISE_QSS)``.

Palette
-------
  Background    #181818   deep charcoal
  Surface       #202020   raised panels / cards
  Surface alt   #282828   hover / elevated states
  Top bar       #121212   chrome bar (darkest layer)
  Border        #333333   default divider
  Border hover  #4A4A4A   hovered / focused border
  Text primary  #F0F0F0   high-contrast body text
  Text muted    #8C8C8C   secondary / label text
  Text faint    #505050   captions / placeholder
  Accent        #C8C8C8   neutral light-grey — active indicator
  Accent bright #FFFFFF   fully selected / focused state
  Accent dim    #909090   pressed / deactivated active
  Success       #4CAF80   green — recording / connected
  Warning       #E0A030   amber — transitional state
  Danger        #D94F4F   red — error / alarm
"""

ENTERPRISE_QSS = """
/* ═══════════════════════════════════════════════════════════════════
   Global reset
   ═══════════════════════════════════════════════════════════════════ */
* {
    font-family: "Inter", "Helvetica Neue", "Arial", sans-serif;
    font-size: 13px;
    color: #F0F0F0;
    outline: none;
}

QMainWindow, QDialog, QWidget {
    background-color: #181818;
}

/* ═══════════════════════════════════════════════════════════════════
   Structural frames
   ═══════════════════════════════════════════════════════════════════ */
QFrame#card {
    background-color: #202020;
    border: 1px solid #333333;
    border-radius: 8px;
}

QFrame#topBar {
    background-color: #121212;
    border-bottom: 1px solid #2C2C2C;
}

QFrame#panelHeader {
    background-color: #161616;
    border-bottom: 1px solid #2C2C2C;
    min-height: 36px;
    max-height: 36px;
}

QFrame#sidebar {
    background-color: #161616;
    border-left: 1px solid #2C2C2C;
}

/* ═══════════════════════════════════════════════════════════════════
   Typography
   ═══════════════════════════════════════════════════════════════════ */
QLabel#display {
    font-size: 34px;
    font-weight: 300;
    color: #FFFFFF;
    letter-spacing: -0.5px;
}

QLabel#h1 {
    font-size: 20px;
    font-weight: 600;
    color: #FFFFFF;
}

QLabel#h2 {
    font-size: 15px;
    font-weight: 600;
    color: #F0F0F0;
}

QLabel#h3 {
    font-size: 10px;
    font-weight: 700;
    color: #707070;
    letter-spacing: 0.8px;
}

QLabel#subtle {
    color: #8C8C8C;
    font-size: 13px;
}

QLabel#caption {
    color: #505050;
    font-size: 11px;
}

QLabel#brand {
    color: #C8C8C8;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1.2px;
}

/* ═══════════════════════════════════════════════════════════════════
   Buttons — Primary
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#primary {
    background-color: #323232;
    color: #F0F0F0;
    border: 1px solid #484848;
    border-radius: 6px;
    padding: 9px 22px;
    font-weight: 600;
    font-size: 13px;
    min-height: 34px;
}
QPushButton#primary:hover {
    background-color: #3C3C3C;
    border-color: #5A5A5A;
    color: #FFFFFF;
}
QPushButton#primary:pressed  { background-color: #262626; border-color: #3A3A3A; }
QPushButton#primary:focus    { border: 1px solid #909090; }
QPushButton#primary:checked  {
    background-color: #282828;
    border-color: #C8C8C8;
    color: #FFFFFF;
}
QPushButton#primary:disabled {
    background-color: #1C1C1C;
    color: #3A3A3A;
    border-color: #252525;
}

/* ═══════════════════════════════════════════════════════════════════
   Buttons — Secondary
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#secondary {
    background-color: #202020;
    color: #D0D0D0;
    border: 1px solid #363636;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: 500;
    min-height: 34px;
}
QPushButton#secondary:hover {
    background-color: #2A2A2A;
    border-color: #4A4A4A;
    color: #F0F0F0;
}
QPushButton#secondary:pressed  { background-color: #181818; }
QPushButton#secondary:focus    { border-color: #909090; }
QPushButton#secondary:disabled { color: #3A3A3A; border-color: #252525; }

/* ═══════════════════════════════════════════════════════════════════
   Buttons — success / verified state
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#primarySuccess {
    background-color: #2E7D32;
    color: #FFFFFF;
    border: 1px solid #1B5E20;
    border-radius: 6px;
    padding: 8px 18px;
    font-weight: 600;
    min-height: 34px;
}
QPushButton#primarySuccess:disabled {
    background-color: #2E7D32;
    color: #FFFFFF;
    border: 1px solid #1B5E20;
}

/* ═══════════════════════════════════════════════════════════════════
   Buttons — Danger
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#danger {
    background-color: #200E0E;
    color: #D94F4F;
    border: 1px solid #3A1A1A;
    border-radius: 6px;
    padding: 7px 16px;
    font-weight: 600;
    min-height: 32px;
}
QPushButton#danger:hover {
    background-color: #2C1414;
    border-color: #5C2A2A;
    color: #E87070;
}
QPushButton#danger:focus { border-color: #D94F4F; }

/* ═══════════════════════════════════════════════════════════════════
   Buttons — Quiet (ghost)
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#quiet {
    background: transparent;
    color: #8C8C8C;
    border: none;
    padding: 6px 12px;
    font-weight: 500;
    border-radius: 4px;
}
QPushButton#quiet:hover { background-color: #222222; color: #F0F0F0; }
QPushButton#quiet:focus { border: 1px solid #363636; }

/* ═══════════════════════════════════════════════════════════════════
   Buttons — Top bar navigation
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#homeBtn {
    background: transparent;
    color: #8C8C8C;
    border: none;
    padding: 5px 10px;
    font-weight: 500;
    font-size: 13px;
    border-radius: 4px;
}
QPushButton#homeBtn:hover {
    color: #F0F0F0;
    background-color: #222222;
}

/* ═══════════════════════════════════════════════════════════════════
   Hero buttons (launch screen)
   ═══════════════════════════════════════════════════════════════════ */
QPushButton#hero {
    background-color: #E8E8E8;
    border: none;
    border-radius: 12px;
    padding: 0;
    min-width: 260px;
    min-height: 200px;
    text-align: left;
}
QPushButton#hero:hover   { background-color: #D0D0D0; }
QPushButton#hero:pressed { background-color: #BEBEBE; }
QPushButton#hero:focus   { border: 2px solid #AAAAAA; }

QPushButton#hero QLabel { background: transparent; }
QPushButton#hero QLabel#heroNumber {
    color: rgba(0, 0, 0, 0.15);
    font-size: 42px;
    font-weight: 800;
    letter-spacing: -1px;
}
QPushButton#hero QLabel#heroTitle {
    color: #1A1A1A;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.2px;
}
QPushButton#hero QLabel#heroSub {
    color: #404040;
    font-size: 12px;
    font-weight: 400;
}

QPushButton#heroAlt {
    background-color: #202020;
    border: 1px solid #2E2E2E;
    border-radius: 12px;
    padding: 0;
    min-width: 260px;
    min-height: 200px;
    text-align: left;
}
QPushButton#heroAlt:hover {
    background-color: #2A2A2A;
    border-color: #484848;
}
QPushButton#heroAlt:pressed { background-color: #181818; }
QPushButton#heroAlt:focus   { border: 2px solid #909090; }

QPushButton#heroAlt QLabel { background: transparent; }
QPushButton#heroAlt QLabel#heroNumber {
    color: #484848;
    font-size: 42px;
    font-weight: 800;
    letter-spacing: -1px;
}
QPushButton#heroAlt QLabel#heroTitle {
    color: #F0F0F0;
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.2px;
}
QPushButton#heroAlt QLabel#heroSub {
    color: #6A6A6A;
    font-size: 12px;
    font-weight: 400;
}

/* ═══════════════════════════════════════════════════════════════════
   Tab widget — underline indicator
   ═══════════════════════════════════════════════════════════════════ */
QTabWidget::pane {
    border: none;
    border-top: 1px solid #2C2C2C;
    background: transparent;
}

QTabBar {
    qproperty-drawBase: 0;
    background: transparent;
}

QTabBar::tab {
    background: transparent;
    color: #5A5A5A;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 10px 26px;
    margin-right: 2px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.1px;
    min-width: 90px;
}

QTabBar::tab:hover:!selected {
    color: #A0A0A0;
    border-bottom: 2px solid #484848;
}

QTabBar::tab:selected {
    color: #F0F0F0;
    background: transparent;
    border-bottom: 2px solid #C8C8C8;
}

/* ═══════════════════════════════════════════════════════════════════
   List widget
   ═══════════════════════════════════════════════════════════════════ */
QListWidget {
    background-color: #1C1C1C;
    border: 1px solid #2C2C2C;
    border-radius: 6px;
    padding: 3px;
    outline: none;
}

QListWidget::item {
    padding: 10px 12px;
    border-radius: 4px;
    border-bottom: 1px solid #232323;
    color: #D0D0D0;
    font-size: 13px;
}

QListWidget::item:last { border-bottom: none; }

QListWidget::item:selected {
    background-color: rgba(200, 200, 200, 0.12);
    color: #F0F0F0;
    border: 1px solid rgba(200, 200, 200, 0.20);
    border-radius: 4px;
}

QListWidget::item:hover:!selected { background-color: #222222; }

/* ═══════════════════════════════════════════════════════════════════
   Inputs (line edit, combo, spinboxes)
   ═══════════════════════════════════════════════════════════════════ */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background-color: #1C1C1C;
    color: #F0F0F0;
    border: 1px solid #333333;
    border-radius: 5px;
    padding: 6px 10px;
    selection-background-color: rgba(200, 200, 200, 0.25);
    selection-color: #FFFFFF;
    min-height: 20px;
}

QLineEdit:hover, QComboBox:hover,
QDoubleSpinBox:hover, QSpinBox:hover { border-color: #4A4A4A; }

QLineEdit:focus, QComboBox:focus,
QDoubleSpinBox:focus, QSpinBox:focus {
    border-color: #909090;
    background-color: #1E1E1E;
}

QLineEdit:disabled, QComboBox:disabled,
QDoubleSpinBox:disabled, QSpinBox:disabled {
    color: #3A3A3A;
    background-color: #181818;
    border-color: #252525;
}

QLineEdit::placeholder { color: #484848; }

/* ── Combo chrome ────────────────────────────────────────────────── */
QComboBox {
    padding-right: 32px;        /* room for the arrow */
}

QComboBox::drop-down {
    border: none;
    width: 26px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
    background-color: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #909090;
    margin-right: 10px;
}

QComboBox:hover::down-arrow {
    border-top-color: #F0F0F0;
}

QComboBox::down-arrow:on {     /* popup open */
    border-top: 0;
    border-bottom: 5px solid #C8C8C8;
}

QComboBox QAbstractItemView {
    background-color: #1E1E1E;
    color: #F0F0F0;
    border: 1px solid #4A4A4A;
    border-radius: 8px;
    selection-background-color: rgba(200, 200, 200, 0.16);
    selection-color: #FFFFFF;
    outline: none;
    padding: 6px;
}

QComboBox QAbstractItemView::item {
    padding: 8px 14px;
    min-height: 24px;
    color: #D0D0D0;
    border-radius: 4px;
    margin: 1px 2px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: rgba(200, 200, 200, 0.08);
    color: #F0F0F0;
}

QComboBox QAbstractItemView::item:selected {
    background-color: rgba(200, 200, 200, 0.18);
    color: #FFFFFF;
}

/* ═══════════════════════════════════════════════════════════════════
   Popup menus (QMenu) — mirror the combo dropdown look so the
   "Remove Y-Axis" and "Capture" menus feel like the rest of the UI.
   ═══════════════════════════════════════════════════════════════════ */
QMenu {
    background-color: #1E1E1E;
    color: #F0F0F0;
    border: 1px solid #4A4A4A;
    border-radius: 8px;
    padding: 4px;
    font-size: 13px;
}

QMenu::item {
    background: transparent;
    color: #D0D0D0;
    padding: 4px 12px;
    min-height: 16px;
    border-radius: 4px;
    margin: 0px;
}

QMenu::item:selected {
    background-color: rgba(200, 200, 200, 0.18);
    color: #FFFFFF;
}

QMenu::item:disabled {
    color: #5A5A5A;
}

QMenu::separator {
    height: 1px;
    background: #2C2C2C;
    margin: 4px 8px;
}

QMenu::icon {
    width: 0px;
    padding-left: 0px;
}

/* ── Spin arrows ─────────────────────────────────────────────────── */
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {
    background: transparent;
    border: none;
    width: 18px;
}

/* ═══════════════════════════════════════════════════════════════════
   Group box
   ═══════════════════════════════════════════════════════════════════ */
QGroupBox {
    background-color: transparent;
    border: 1px solid #2C2C2C;
    border-radius: 6px;
    margin-top: 16px;
    padding: 10px 12px 12px 12px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 0px;
    padding: 0 6px;
    background: transparent;
    color: #909090;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.8px;
}

QGroupBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #3A3A3A;
    border-radius: 3px;
    background-color: #1E1E1E;
}
QGroupBox::indicator:checked {
    background-color: #C8C8C8;
    border-color: #C8C8C8;
}
QGroupBox::indicator:hover { border-color: #909090; }

/* ═══════════════════════════════════════════════════════════════════
   Checkbox
   ═══════════════════════════════════════════════════════════════════ */
QCheckBox {
    spacing: 8px;
    color: #D0D0D0;
    padding: 4px 6px;
    border-radius: 4px;
}
QCheckBox:hover {
    background-color: rgba(200, 200, 200, 0.08);
    color: #FFFFFF;
}
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #3A3A3A;
    border-radius: 3px;
    background-color: #1C1C1C;
}
QCheckBox::indicator:hover   { border-color: #C8C8C8; }
QCheckBox::indicator:checked {
    background-color: #C8C8C8;
    border-color: #C8C8C8;
}

/* ═══════════════════════════════════════════════════════════════════
   Radio button
   ═══════════════════════════════════════════════════════════════════ */
QRadioButton {
    spacing: 8px;
    color: #D0D0D0;
    padding: 4px 6px;
    border-radius: 4px;
}
QRadioButton:hover {
    background-color: rgba(200, 200, 200, 0.08);
    color: #FFFFFF;
}
QRadioButton::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #3A3A3A;
    border-radius: 8px;
    background-color: #1C1C1C;
}
QRadioButton::indicator:hover   { border-color: #C8C8C8; }
QRadioButton::indicator:checked {
    background-color: #C8C8C8;
    border-color: #C8C8C8;
}

/* ═══════════════════════════════════════════════════════════════════
   Scroll bars
   ═══════════════════════════════════════════════════════════════════ */
QScrollBar:vertical {
    background: transparent;
    border: none;
    width: 8px;
    margin: 3px 2px;
}
QScrollBar:horizontal {
    background: transparent;
    border: none;
    height: 8px;
    margin: 2px 3px;
}
QScrollBar::handle {
    background-color: #303030;
    border-radius: 3px;
    min-height: 28px;
    min-width: 28px;
}
QScrollBar::handle:hover   { background-color: #424242; }
QScrollBar::handle:pressed { background-color: #909090; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ═══════════════════════════════════════════════════════════════════
   Status bar
   ═══════════════════════════════════════════════════════════════════ */
QStatusBar {
    background-color: #121212;
    color: #606060;
    border-top: 1px solid #222222;
    padding: 3px 14px;
    font-size: 12px;
}
QStatusBar::item { border: none; }

/* ═══════════════════════════════════════════════════════════════════
   Splitter
   ═══════════════════════════════════════════════════════════════════ */
QSplitter::handle { background-color: #2C2C2C; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical   { height: 1px; }
QSplitter::handle:hover      { background-color: #909090; }

/* ═══════════════════════════════════════════════════════════════════
   Tooltip
   ═══════════════════════════════════════════════════════════════════ */
QToolTip {
    background-color: #141414;
    color: #F0F0F0;
    border: 1px solid #363636;
    padding: 6px 10px;
    border-radius: 5px;
    font-size: 12px;
}

/* ═══════════════════════════════════════════════════════════════════
   Dialogs
   ═══════════════════════════════════════════════════════════════════ */
QMessageBox            { background-color: #1E1E1E; }
QMessageBox QLabel     { color: #F0F0F0; }
QMessageBox QPushButton { min-width: 80px; padding: 6px 16px; }

/* ═══════════════════════════════════════════════════════════════════
   Status pill labels
   ═══════════════════════════════════════════════════════════════════ */
QLabel#statusReady {
    background-color: #0A1F14;
    color: #4CAF80;
    border: 1px solid #143D26;
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}

QLabel#statusGathering {
    background-color: #1F1600;
    color: #E0A030;
    border: 1px solid #3D2C00;
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}

QLabel#statusFailed {
    background-color: #1F0808;
    color: #D94F4F;
    border: 1px solid #3D1010;
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}

/* ═══════════════════════════════════════════════════════════════════
   Recording status strip
   ═══════════════════════════════════════════════════════════════════ */
QLabel#recordingStrip {
    color: #505050;
    font-size: 11px;
    padding: 3px 14px;
    background: transparent;
}

QLabel#recordingStrip[recording="true"] {
    color: #4CAF80;
    background-color: rgba(76, 175, 128, 0.07);
    border-top: 1px solid rgba(76, 175, 128, 0.16);
    font-size: 11px;
    font-weight: 600;
    padding: 3px 14px;
}

/* ═══════════════════════════════════════════════════════════════════
   Dock separator chrome
   ═══════════════════════════════════════════════════════════════════ */
QMainWindow::separator {
    background: #2C2C2C;
    width: 1px;
    height: 1px;
}

/* ═══════════════════════════════════════════════════════════════════
   Table widget
   ═══════════════════════════════════════════════════════════════════ */
QTableWidget {
    background-color: #1C1C1C;
    border: 1px solid #2C2C2C;
    border-radius: 4px;
    gridline-color: #272727;
}
QTableWidget::item { padding: 4px 8px; color: #D0D0D0; }
QTableWidget::item:selected {
    background-color: rgba(200, 200, 200, 0.12);
    color: #F0F0F0;
}
QHeaderView::section {
    background-color: #202020;
    color: #8C8C8C;
    border: none;
    border-bottom: 1px solid #2C2C2C;
    padding: 5px 8px;
    font-size: 11px;
    font-weight: 600;
}

/* ═══════════════════════════════════════════════════════════════════
   Workspace status bar
   ═══════════════════════════════════════════════════════════════════ */
QFrame#workspaceStatusBar {
    background-color: #121212;
    border-top: 1px solid #2C2C2C;
}

QLabel#statusSegment {
    color: #8C8C8C;
    font-size: 11px;
    padding: 0 12px;
}

QFrame#statusSep {
    color: #2C2C2C;
    background-color: #2C2C2C;
}

/* ═══════════════════════════════════════════════════════════════════
   Channel Manager
   ═══════════════════════════════════════════════════════════════════ */
QFrame#channelManager {
    background-color: #1A1A1A;
    border-right: 1px solid #2C2C2C;
}

QFrame#chManagerRow {
    background-color: #1A1A1A;
    border-bottom: 1px solid #242424;
}

QFrame#chManagerRow:hover {
    background-color: #222222;
}

QLabel#chBadge {
    background-color: #2A2A2A;
    color: #8C8C8C;
    border: 1px solid #383838;
    border-radius: 3px;
    font-size: 10px;
    padding: 1px 4px;
}

/* ═══════════════════════════════════════════════════════════════════
   Digital Readout Dashboard
   ═══════════════════════════════════════════════════════════════════ */
QDockWidget#liveValuesDock {
    color: #C8C8C8;
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}
QDockWidget#liveValuesDock::title {
    background-color: #161616;
    color: #C8C8C8;
    border-bottom: 1px solid #2C2C2C;
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
    text-align: left;
}

QWidget#droHost, QWidget#droInner { background-color: #161616; }

QFrame#droBox {
    background-color: #1E1E1E;
    border: 1px solid #2C2C2C;
    border-left: 3px solid #3A3A3A;
    border-radius: 6px;
}

QFrame#droBox[alarm="true"] {
    background-color: #2A0E0E;
    border: 1px solid #D94F4F;
    border-left: 3px solid #FF4848;
}

QLabel#droName {
    color: #8C8C8C;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.0px;
    text-transform: uppercase;
    background: transparent;
}
QFrame#droBox[alarm="true"] QLabel#droName {
    color: #FFB0B0;
}

QLabel#droValue {
    color: #F0F0F0;
    font-family: "JetBrains Mono", "SF Mono", "Menlo", "Consolas", monospace;
    font-size: 28px;
    font-weight: 600;
    letter-spacing: -0.5px;
    background: transparent;
}
QFrame#droBox[alarm="true"] QLabel#droValue {
    color: #FF6464;
}

QScrollArea QScrollBar:horizontal {
    height: 6px;
}

/* GPU badge on Vispy panels */
QLabel#gpuBadge {
    background-color: #1E2A1E;
    color: #4CAF80;
    border: 1px solid #4CAF80;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 600;
    padding: 1px 6px;
}

"""


def apply_enterprise_theme(app) -> None:
    """Apply the enterprise QSS to a QApplication instance."""
    app.setStyleSheet(ENTERPRISE_QSS)
