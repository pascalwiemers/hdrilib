"""Qt panel for browsing and assigning HDRI/light textures.

Importing this module is safe in a plain Python process. Qt is only required when
``createInterface()`` (the Houdini Python Panel entry point) constructs the widget.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from . import assign, config, convert, files, prepare, resize, resolution, thumbs, variants

try:
    from hutil.Qt import QtCore, QtGui, QtWidgets

    _QT_IMPORT_ERROR = None
except (ImportError, RuntimeError) as error:
    QtCore = QtGui = QtWidgets = None  # type: ignore
    _QT_IMPORT_ERROR = error


_ACTIVE_THREADS = set()


def _format_duration(seconds):
    """Format an ETA compactly for the shared progress bar."""

    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "{}s".format(seconds)
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return "{}m {}s".format(minutes, seconds)
    hours, minutes = divmod(minutes, 60)
    return "{}h {}m".format(hours, minutes)


if QtCore is not None:
    Signal = getattr(QtCore, "Signal", None) or QtCore.pyqtSignal
    Slot = getattr(QtCore, "Slot", None) or QtCore.pyqtSlot

    def _resolve_qaction():
        # hutil.Qt's curated shim can miss QAction in both QtGui and QtWidgets,
        # so fall back to the real PySide modules.
        for module in (QtGui, QtWidgets):
            try:
                cls = getattr(module, "QAction", None)
            except AttributeError:
                cls = None
            if cls is not None:
                return cls
        try:
            from PySide6.QtGui import QAction as cls  # type: ignore

            return cls
        except ImportError:
            pass
        try:
            from PySide2.QtWidgets import QAction as cls  # type: ignore

            return cls
        except ImportError:
            return None

    QAction = _resolve_qaction()

    def _enum(container, scoped_name, member):
        direct = getattr(container, member, None)
        return direct if direct is not None else getattr(getattr(container, scoped_name), member)

    def _enum_value(value):
        return getattr(value, "value", value)

    # Virtual location aggregating every included library root in the browser.
    ALL_HDRI = "::all-hdris::"

    USER_ROLE = _enum(QtCore.Qt, "ItemDataRole", "UserRole")
    TOOLTIP_ROLE = _enum(QtCore.Qt, "ItemDataRole", "ToolTipRole")
    ROOT_ROLE = _enum_value(USER_ROLE) + 1
    GROUP_ROLE = _enum_value(USER_ROLE) + 2
    ALIGN_CENTER = _enum(QtCore.Qt, "AlignmentFlag", "AlignCenter")
    HORIZONTAL = _enum(QtCore.Qt, "Orientation", "Horizontal")
    ICON_MODE = _enum(QtWidgets.QListView, "ViewMode", "IconMode")
    LIST_MODE = _enum(QtWidgets.QListView, "ViewMode", "ListMode")
    ADJUST = _enum(QtWidgets.QListView, "ResizeMode", "Adjust")
    STATIC_MOVEMENT = _enum(QtWidgets.QListView, "Movement", "Static")
    EXTENDED_SELECTION = _enum(QtWidgets.QAbstractItemView, "SelectionMode", "ExtendedSelection")
    INSTANT_POPUP = _enum(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup")
    CUSTOM_CONTEXT_MENU = _enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu")
    STANDARD_FILE_ICON = _enum(QtWidgets.QStyle, "StandardPixmap", "SP_FileIcon")
    ITEM_IS_EDITABLE = _enum(QtCore.Qt, "ItemFlag", "ItemIsEditable")
    DIALOG_ACCEPTED = _enum_value(_enum(QtWidgets.QDialog, "DialogCode", "Accepted"))
    DIALOG_OK = _enum(QtWidgets.QDialogButtonBox, "StandardButton", "Ok")
    DIALOG_CANCEL = _enum(QtWidgets.QDialogButtonBox, "StandardButton", "Cancel")


    class RootSettingsDelegate(QtWidgets.QStyledItemDelegate):
        """Allow inline editing only for the optional display-label column."""

        def createEditor(self, parent, option, index):
            if index.column() != 1:
                return None
            return super().createEditor(parent, option, index)


    class ThumbnailWorker(QtCore.QObject):
        """Run the concurrent thumbnail helper in one dedicated Qt thread."""

        thumbnail_ready = Signal(str, str)
        progress = Signal(int, int)
        problem = Signal(str, str)
        finished = Signal(bool)

        def __init__(self, paths, size, workers, tonemap=None):
            super().__init__()
            self._paths = list(paths)
            self._size = int(size)
            self._workers = int(workers)
            self._tonemap = tonemap
            self._cancelled = threading.Event()

        def cancel(self):
            # Called directly from the UI thread. threading.Event is intentionally
            # used because this object's event loop is occupied while run() executes.
            self._cancelled.set()

        @Slot()
        def run(self):
            try:
                thumbs.generate_thumbnails_parallel(
                    self._paths,
                    size=self._size,
                    workers=self._workers,
                    tonemap=self._tonemap,
                    cancel_event=self._cancelled,
                    on_result=self.thumbnail_ready.emit,
                    on_error=lambda source, error: self.problem.emit(source, str(error)),
                    on_progress=self.progress.emit,
                )
            except Exception as error:
                self.problem.emit("", str(error))
            self.finished.emit(self._cancelled.is_set())


    class RatConversionWorker(QtCore.QObject):
        """Run a concurrent RAT conversion batch in one dedicated Qt thread."""

        converted = Signal(str, str)
        skipped = Signal(str, str, str)
        progress = Signal(int, int)
        problem = Signal(str, str)
        finished = Signal(bool)

        def __init__(self, paths, mode, subfolder_name, overwrite, workers):
            super().__init__()
            self._paths = list(paths)
            self._mode = mode
            self._subfolder_name = subfolder_name
            self._overwrite = bool(overwrite)
            self._workers = int(workers)
            self._cancelled = threading.Event()

        def cancel(self):
            self._cancelled.set()

        @Slot()
        def run(self):
            try:
                convert.convert_to_rat_parallel(
                    self._paths,
                    mode=self._mode,
                    subfolder_name=self._subfolder_name,
                    overwrite=self._overwrite,
                    workers=self._workers,
                    cancel_event=self._cancelled,
                    on_result=self.converted.emit,
                    on_skipped=self.skipped.emit,
                    on_error=lambda source, error: self.problem.emit(source, str(error)),
                    on_progress=self.progress.emit,
                )
            except Exception as error:
                self.problem.emit("", str(error))
            self.finished.emit(self._cancelled.is_set())


    class LowResWorker(QtCore.QObject):
        """Run one low-resolution rung as a concurrent batch."""

        resized = Signal(str)
        skipped = Signal(str, str, str)
        progress = Signal(int, int)
        problem = Signal(str, str)
        finished = Signal(bool)

        def __init__(self, paths, width, mode, also_rat, overwrite, workers):
            super().__init__()
            self._paths = list(paths)
            self._width = int(width)
            self._mode = mode
            self._also_rat = bool(also_rat)
            self._overwrite = bool(overwrite)
            self._workers = int(workers)
            self._cancelled = threading.Event()

        def cancel(self):
            self._cancelled.set()

        @Slot()
        def run(self):
            try:
                resize.resize_to_rung_parallel(
                    self._paths,
                    self._width,
                    mode=self._mode,
                    also_rat=self._also_rat,
                    overwrite=self._overwrite,
                    workers=self._workers,
                    cancel_event=self._cancelled,
                    on_result=lambda source, _result: self.resized.emit(source),
                    on_skipped=self.skipped.emit,
                    on_error=lambda source, error: self.problem.emit(source, str(error)),
                    on_progress=self.progress.emit,
                )
            except Exception as error:
                self.problem.emit("", str(error))
            self.finished.emit(self._cancelled.is_set())


    class PrepareWorker(QtCore.QObject):
        """Run a complete Settings-root preparation plan in one Qt job."""

        progress = Signal(int, int)
        problem = Signal(str, str)
        finished = Signal(bool, object)

        def __init__(self, plan, settings):
            super().__init__()
            self._plan = plan
            self._settings = dict(settings)
            self._cancelled = threading.Event()

        def cancel(self):
            self._cancelled.set()

        @Slot()
        def run(self):
            summary = prepare.PipelineSummary()
            try:
                summary, cancelled = prepare.run_pipeline(
                    self._plan,
                    rat_mode=self._settings.get("rat_output_mode", "alongside"),
                    rat_subfolder_name=self._settings.get("rat_subfolder_name", "rat"),
                    rat_overwrite=bool(
                        self._settings.get("rat_overwrite_existing", False)
                    ),
                    resize_overwrite=bool(
                        self._settings.get("lowres_overwrite_existing", False)
                    ),
                    generate_thumbnails=bool(
                        self._settings.get("prepare_generate_thumbnails", True)
                    ),
                    thumbnail_size=int(self._settings.get("thumbnail_size", 256)),
                    thumbnail_tonemap=self._settings.get(
                        "thumbnail_tonemap", thumbs.DEFAULT_TONEMAP
                    ),
                    workers=int(
                        self._settings.get(
                            "thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS
                        )
                    ),
                    cancel_event=self._cancelled,
                    on_progress=self.progress.emit,
                    on_problem=lambda source, error: self.problem.emit(
                        source, str(error)
                    ),
                )
            except Exception as error:
                summary.failed += 1
                self.problem.emit("", str(error))
                cancelled = self._cancelled.is_set()
            self.finished.emit(cancelled, summary)


    class HDRILibPanel(QtWidgets.QWidget):
        """Main HDRI Library widget."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._settings = config.load_config()
            self._folder = self._settings.get("last_folder", "")
            self._folder_root = ""
            self._all_files = []
            self._worker = None
            self._thread = None
            self._job_kind = None
            self._generation_errors = 0
            self._conversion_skipped = 0
            self._prepare_context = None
            self._progress_label = "Processing"
            self._progress_last_time = 0.0
            self._progress_last_count = 0
            self._progress_samples = deque(maxlen=20)
            self._format_actions = {}
            self._root_format_buttons = {}
            self._updating_roots = False
            self._syncing_location = False
            self._build_ui()
            self._restore_ui()
            self._rebuild_root_settings()
            self._rebuild_locations()

        def _build_ui(self):
            self.setObjectName("hdrilibPanel")
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)
            self.tabs = QtWidgets.QTabWidget()
            outer.addWidget(self.tabs)

            self.browse_tab = QtWidgets.QWidget()
            self.settings_tab = QtWidgets.QWidget()
            self.tabs.addTab(self.browse_tab, "Browse")
            self.tabs.addTab(self.settings_tab, "Settings")

            # The job bar lives outside the tabs so progress from jobs started
            # in either tab (Browse buttons or Settings context menus) is
            # always visible.
            generation_bar = QtWidgets.QHBoxLayout()
            self.generate_button = QtWidgets.QPushButton("Generate thumbnails")
            self.cancel_button = QtWidgets.QPushButton("Cancel")
            self.cancel_button.setEnabled(False)
            self.progress = QtWidgets.QProgressBar()
            self.progress.setTextVisible(True)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            generation_bar.addWidget(self.generate_button)
            generation_bar.addWidget(self.cancel_button)
            generation_bar.addWidget(self.progress, 1)
            outer.addLayout(generation_bar)

            self.status = QtWidgets.QLabel("Add an HDRI root in Settings to begin.")
            self.status.setWordWrap(True)
            outer.addWidget(self.status)

            self._build_browse_tab()
            self._build_settings_tab()

        def _build_browse_tab(self):
            layout = QtWidgets.QVBoxLayout(self.browse_tab)
            layout.setContentsMargins(4, 6, 4, 4)

            toolbar = QtWidgets.QHBoxLayout()
            self.location_label = QtWidgets.QLabel("Location")
            self.location_combo = QtWidgets.QComboBox()
            self.location_combo.setMinimumWidth(220)
            self.refresh_button = QtWidgets.QPushButton("Refresh")
            self.convert_folder_button = QtWidgets.QPushButton("Convert folder to .rat")
            self.lowres_folder_button = QtWidgets.QPushButton("Create low-res…")
            toolbar.addWidget(self.location_label)
            toolbar.addWidget(self.location_combo, 1)
            toolbar.addWidget(self.refresh_button)
            toolbar.addWidget(self.convert_folder_button)
            toolbar.addWidget(self.lowres_folder_button)
            layout.addLayout(toolbar)

            filter_bar = QtWidgets.QHBoxLayout()
            self.search = QtWidgets.QLineEdit()
            self.search.setClearButtonEnabled(True)
            self.search.setPlaceholderText("Filter by filename…")
            self.include_subfolders = QtWidgets.QCheckBox("Include subfolders")
            self.format_button = QtWidgets.QToolButton()
            self.format_button.setText("Formats")
            self.format_button.setPopupMode(INSTANT_POPUP)
            format_menu = QtWidgets.QMenu(self.format_button)
            for extension in config.DEFAULT_EXTENSIONS:
                action = QAction(extension, format_menu)
                action.setCheckable(True)
                action.toggled.connect(self._formats_changed)
                format_menu.addAction(action)
                self._format_actions[extension] = action
            self.format_button.setMenu(format_menu)
            self.group_resolutions = QtWidgets.QCheckBox("Group resolutions")
            self.group_resolutions.setToolTip(
                "Show one entry per HDRI, combining _1k/_4k… suffix and rung-"
                "subfolder variants. Hover an entry to see its files."
            )
            self.view_mode_combo = QtWidgets.QComboBox()
            self.view_mode_combo.addItems(["Grid", "List"])
            self.view_mode_combo.setToolTip("Switch between thumbnail grid and compact list")
            self.icon_size_slider = QtWidgets.QSlider(HORIZONTAL)
            self.icon_size_slider.setRange(48, 512)
            self.icon_size_slider.setSingleStep(16)
            self.icon_size_slider.setPageStep(64)
            self.icon_size_slider.setFixedWidth(120)
            self.icon_size_slider.setToolTip(
                "Thumbnail display size (cache resolution is set in Settings)"
            )
            filter_bar.addWidget(self.search, 1)
            filter_bar.addWidget(self.include_subfolders)
            filter_bar.addWidget(self.group_resolutions)
            filter_bar.addWidget(self.format_button)
            filter_bar.addWidget(self.view_mode_combo)
            filter_bar.addWidget(self.icon_size_slider)
            layout.addLayout(filter_bar)

            self.splitter = QtWidgets.QSplitter(HORIZONTAL)
            self.folder_tree = QtWidgets.QTreeWidget()
            self.folder_tree.setHeaderHidden(True)
            self.folder_tree.setMinimumWidth(180)
            self.grid = QtWidgets.QListWidget()
            self.grid.setViewMode(ICON_MODE)
            self.grid.setResizeMode(ADJUST)
            self.grid.setMovement(STATIC_MOVEMENT)
            self.grid.setSelectionMode(EXTENDED_SELECTION)
            self.grid.setUniformItemSizes(True)
            self.grid.setWordWrap(True)
            self.grid.setSpacing(6)
            self.grid.setContextMenuPolicy(CUSTOM_CONTEXT_MENU)
            self.splitter.addWidget(self.folder_tree)
            self.splitter.addWidget(self.grid)
            self.splitter.setStretchFactor(1, 1)
            layout.addWidget(self.splitter, 1)

            self.refresh_button.clicked.connect(self._refresh)
            self.convert_folder_button.clicked.connect(self._start_folder_conversion)
            self.lowres_folder_button.clicked.connect(self._show_folder_lowres_menu)
            self.search.textChanged.connect(self._search_changed)
            self.include_subfolders.toggled.connect(self._include_changed)
            self.folder_tree.currentItemChanged.connect(self._folder_changed)
            self.location_combo.currentIndexChanged.connect(self._dropdown_folder_changed)
            self.grid.itemDoubleClicked.connect(self._assign_item)
            self.grid.customContextMenuRequested.connect(self._show_grid_context_menu)
            self.generate_button.clicked.connect(self._start_generation)
            self.cancel_button.clicked.connect(self._cancel_job)
            self.group_resolutions.toggled.connect(self._grouping_changed)
            self.view_mode_combo.currentIndexChanged.connect(self._view_mode_changed)
            self.icon_size_slider.valueChanged.connect(self._display_size_changed)
            self.icon_size_slider.sliderReleased.connect(self._save)

        def _build_settings_tab(self):
            layout = QtWidgets.QVBoxLayout(self.settings_tab)
            layout.setContentsMargins(8, 8, 8, 8)

            roots_group = QtWidgets.QGroupBox("Root folders")
            roots_layout = QtWidgets.QVBoxLayout(roots_group)
            self.roots_list = QtWidgets.QTreeWidget()
            self.roots_list.setColumnCount(4)
            self.roots_list.setHeaderLabels(
                ["Folder", "Display label", "Color", "Formats"]
            )
            self.roots_list.setRootIsDecorated(False)
            self.roots_list.setAlternatingRowColors(True)
            self.roots_list.setItemDelegate(RootSettingsDelegate(self.roots_list))
            self.roots_list.setSelectionMode(EXTENDED_SELECTION)
            self.roots_list.setContextMenuPolicy(CUSTOM_CONTEXT_MENU)
            roots_layout.addWidget(self.roots_list)
            root_buttons = QtWidgets.QHBoxLayout()
            self.settings_add_root = QtWidgets.QPushButton("Add…")
            self.settings_remove_root = QtWidgets.QPushButton("Remove")
            self.settings_move_up = QtWidgets.QPushButton("Move up")
            self.settings_move_down = QtWidgets.QPushButton("Move down")
            self.settings_color_root = QtWidgets.QPushButton("Choose color…")
            self.settings_clear_color = QtWidgets.QPushButton("Clear color")
            for button in (
                self.settings_add_root,
                self.settings_remove_root,
                self.settings_move_up,
                self.settings_move_down,
                self.settings_color_root,
                self.settings_clear_color,
            ):
                root_buttons.addWidget(button)
            root_buttons.addStretch(1)
            roots_layout.addLayout(root_buttons)
            layout.addWidget(roots_group, 1)

            options_grid = QtWidgets.QGridLayout()
            location_group = QtWidgets.QGroupBox("Location UI")
            location_layout = QtWidgets.QVBoxLayout(location_group)
            self.sidebar_radio = QtWidgets.QRadioButton("Sidebar")
            self.dropdown_radio = QtWidgets.QRadioButton("Dropdown")
            location_layout.addWidget(self.sidebar_radio)
            location_layout.addWidget(self.dropdown_radio)
            location_layout.addStretch(1)
            options_grid.addWidget(location_group, 0, 0)

            thumbnail_group = QtWidgets.QGroupBox("Thumbnails")
            thumbnail_layout = QtWidgets.QFormLayout(thumbnail_group)
            self.thumbnail_size_spin = QtWidgets.QSpinBox()
            self.thumbnail_size_spin.setRange(64, 1024)
            self.thumbnail_size_spin.setSingleStep(32)
            self.thumbnail_size_spin.setSuffix(" px")
            self.thumbnail_size_spin.setKeyboardTracking(False)
            self.thumbnail_workers_spin = QtWidgets.QSpinBox()
            self.thumbnail_workers_spin.setRange(1, 64)
            self.thumbnail_workers_spin.setKeyboardTracking(False)
            self.thumbnail_tonemap_combo = QtWidgets.QComboBox()
            self.thumbnail_tonemap_combo.addItem("Neutral (soft contrast)", "neutral")
            self.thumbnail_tonemap_combo.addItem("ACES (filmic)", "aces")
            self.thumbnail_tonemap_combo.setToolTip(
                "Neutral compresses highlights with open shadows; ACES is contrastier."
                " Changing this regenerates thumbnails on demand."
            )
            self.clear_thumbs_button = QtWidgets.QPushButton("Clear thumbnail cache")
            self.assign_resolution_combo = QtWidgets.QComboBox()
            self.assign_resolution_combo.addItem("Highest resolution", "highest")
            self.assign_resolution_combo.addItem("Lowest resolution", "lowest")
            for width, label in (
                (1024, "1K"),
                (2048, "2K"),
                (4096, "4K"),
                (8192, "8K"),
                (16384, "16K"),
            ):
                self.assign_resolution_combo.addItem("Closest to " + label, str(width))
            self.assign_resolution_combo.setToolTip(
                "Which resolution variant a double-click assigns when"
                " \"Group resolutions\" is on."
            )
            thumbnail_layout.addRow("Preview size", self.thumbnail_size_spin)
            thumbnail_layout.addRow("Parallel workers", self.thumbnail_workers_spin)
            thumbnail_layout.addRow("Tone mapping", self.thumbnail_tonemap_combo)
            thumbnail_layout.addRow("Double-click assigns", self.assign_resolution_combo)
            thumbnail_layout.addRow("", self.clear_thumbs_button)
            options_grid.addWidget(thumbnail_group, 0, 1)

            rat_group = QtWidgets.QGroupBox("RAT Conversion")
            rat_layout = QtWidgets.QFormLayout(rat_group)
            self.rat_alongside_radio = QtWidgets.QRadioButton("Alongside source")
            self.rat_subfolder_radio = QtWidgets.QRadioButton("Source subfolder")
            rat_location = QtWidgets.QVBoxLayout()
            rat_location.addWidget(self.rat_alongside_radio)
            rat_location.addWidget(self.rat_subfolder_radio)
            self.rat_subfolder_name = QtWidgets.QLineEdit()
            self.rat_subfolder_name.setPlaceholderText("rat")
            self.rat_overwrite = QtWidgets.QCheckBox("Overwrite existing")
            self.rat_overwrite.setToolTip(
                "Also replace RAT files that are already at least as new as their source"
            )
            rat_layout.addRow("Output", rat_location)
            rat_layout.addRow("Subfolder name", self.rat_subfolder_name)
            rat_layout.addRow(self.rat_overwrite)
            options_grid.addWidget(rat_group, 1, 0)

            lowres_group = QtWidgets.QGroupBox("Low-Res Variants")
            lowres_layout = QtWidgets.QFormLayout(lowres_group)
            self.lowres_alongside_radio = QtWidgets.QRadioButton("Alongside source")
            self.lowres_subfolder_radio = QtWidgets.QRadioButton("Resolution subfolder")
            lowres_location = QtWidgets.QVBoxLayout()
            lowres_location.addWidget(self.lowres_alongside_radio)
            lowres_location.addWidget(self.lowres_subfolder_radio)
            self.lowres_also_rat = QtWidgets.QCheckBox("Also convert to .rat")
            self.lowres_also_rat.setToolTip(
                "Keep the same-format variant and add a mipmapped RAT companion"
            )
            self.lowres_overwrite = QtWidgets.QCheckBox("Overwrite existing")
            lowres_layout.addRow("Output", lowres_location)
            lowres_layout.addRow(self.lowres_also_rat)
            lowres_layout.addRow(self.lowres_overwrite)
            options_grid.addWidget(lowres_group, 1, 1)
            layout.addLayout(options_grid)

            self.settings_add_root.clicked.connect(self._add_root)
            self.settings_remove_root.clicked.connect(self._remove_root)
            self.settings_move_up.clicked.connect(lambda: self._move_root(-1))
            self.settings_move_down.clicked.connect(lambda: self._move_root(1))
            self.settings_color_root.clicked.connect(self._choose_root_color)
            self.settings_clear_color.clicked.connect(lambda: self._set_selected_root_color(""))
            self.roots_list.itemChanged.connect(self._root_item_changed)
            self.roots_list.currentItemChanged.connect(self._root_selection_changed)
            self.roots_list.itemDoubleClicked.connect(self._root_item_double_clicked)
            self.roots_list.customContextMenuRequested.connect(
                self._show_root_context_menu
            )
            self.sidebar_radio.toggled.connect(
                lambda checked: checked and self.set_location_ui_mode("sidebar")
            )
            self.dropdown_radio.toggled.connect(
                lambda checked: checked and self.set_location_ui_mode("dropdown")
            )
            self.thumbnail_size_spin.valueChanged.connect(self._thumbnail_settings_changed)
            self.thumbnail_workers_spin.valueChanged.connect(self._thumbnail_settings_changed)
            self.thumbnail_tonemap_combo.currentIndexChanged.connect(
                self._thumbnail_settings_changed
            )
            self.assign_resolution_combo.currentIndexChanged.connect(
                self._thumbnail_settings_changed
            )
            self.clear_thumbs_button.clicked.connect(self._clear_thumbnail_cache)
            self.rat_alongside_radio.toggled.connect(self._rat_settings_changed)
            self.rat_subfolder_radio.toggled.connect(self._rat_settings_changed)
            self.rat_subfolder_name.editingFinished.connect(self._rat_settings_changed)
            self.rat_overwrite.toggled.connect(self._rat_settings_changed)
            self.lowres_alongside_radio.toggled.connect(self._lowres_settings_changed)
            self.lowres_subfolder_radio.toggled.connect(self._lowres_settings_changed)
            self.lowres_also_rat.toggled.connect(self._lowres_settings_changed)
            self.lowres_overwrite.toggled.connect(self._lowres_settings_changed)

        def _restore_ui(self):
            self.search.blockSignals(True)
            self.search.setText(self._settings.get("search_text", ""))
            self.search.blockSignals(False)
            self.include_subfolders.blockSignals(True)
            self.include_subfolders.setChecked(bool(self._settings.get("include_subfolders")))
            self.include_subfolders.blockSignals(False)

            self._sync_browse_formats()

            self.thumbnail_size_spin.blockSignals(True)
            self.thumbnail_size_spin.setValue(int(self._settings.get("thumbnail_size", 256)))
            self.thumbnail_size_spin.blockSignals(False)
            self.thumbnail_workers_spin.blockSignals(True)
            self.thumbnail_workers_spin.setValue(
                int(self._settings.get("thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS))
            )
            self.thumbnail_workers_spin.blockSignals(False)
            self.thumbnail_tonemap_combo.blockSignals(True)
            self.thumbnail_tonemap_combo.setCurrentIndex(
                1 if self._settings.get("thumbnail_tonemap") == "aces" else 0
            )
            self.thumbnail_tonemap_combo.blockSignals(False)
            self.assign_resolution_combo.blockSignals(True)
            self.assign_resolution_combo.setCurrentIndex(
                max(
                    0,
                    self.assign_resolution_combo.findData(
                        self._settings.get("assign_resolution", variants.DEFAULT_ASSIGN)
                    ),
                )
            )
            self.assign_resolution_combo.blockSignals(False)
            self.group_resolutions.blockSignals(True)
            self.group_resolutions.setChecked(
                bool(self._settings.get("group_resolutions", False))
            )
            self.group_resolutions.blockSignals(False)
            self.view_mode_combo.blockSignals(True)
            self.view_mode_combo.setCurrentIndex(
                1 if self._settings.get("view_mode") == "list" else 0
            )
            self.view_mode_combo.blockSignals(False)
            self.icon_size_slider.blockSignals(True)
            self.icon_size_slider.setValue(int(self._settings.get("display_icon_size", 256)))
            self.icon_size_slider.blockSignals(False)
            rat_mode = self._settings.get("rat_output_mode", "alongside")
            self.rat_alongside_radio.blockSignals(True)
            self.rat_subfolder_radio.blockSignals(True)
            self.rat_alongside_radio.setChecked(rat_mode == "alongside")
            self.rat_subfolder_radio.setChecked(rat_mode == "subfolder")
            self.rat_alongside_radio.blockSignals(False)
            self.rat_subfolder_radio.blockSignals(False)
            self.rat_subfolder_name.blockSignals(True)
            self.rat_subfolder_name.setText(
                self._settings.get("rat_subfolder_name", "rat")
            )
            self.rat_subfolder_name.blockSignals(False)
            self.rat_overwrite.blockSignals(True)
            self.rat_overwrite.setChecked(
                bool(self._settings.get("rat_overwrite_existing", False))
            )
            self.rat_overwrite.blockSignals(False)
            self._update_rat_settings_enabled()
            lowres_mode = self._settings.get("lowres_output_mode", "alongside")
            self.lowres_alongside_radio.blockSignals(True)
            self.lowres_subfolder_radio.blockSignals(True)
            self.lowres_alongside_radio.setChecked(lowres_mode == "alongside")
            self.lowres_subfolder_radio.setChecked(lowres_mode == "subfolder")
            self.lowres_alongside_radio.blockSignals(False)
            self.lowres_subfolder_radio.blockSignals(False)
            self.lowres_also_rat.blockSignals(True)
            self.lowres_also_rat.setChecked(
                bool(self._settings.get("lowres_also_rat", False))
            )
            self.lowres_also_rat.blockSignals(False)
            self.lowres_overwrite.blockSignals(True)
            self.lowres_overwrite.setChecked(
                bool(self._settings.get("lowres_overwrite_existing", False))
            )
            self.lowres_overwrite.blockSignals(False)
            self._apply_icon_size()
            self.set_location_ui_mode(
                self._settings.get("location_ui_mode", "sidebar"), save=False
            )

        def _apply_icon_size(self):
            size = int(self._settings.get("display_icon_size", 256))
            if self._settings.get("view_mode", "grid") == "list":
                height = max(24, size // 4)
                self.grid.setViewMode(LIST_MODE)
                self.grid.setWordWrap(False)
                self.grid.setSpacing(1)
                self.grid.setIconSize(QtCore.QSize(height * 2, height))
                self.grid.setGridSize(QtCore.QSize())
            else:
                self.grid.setViewMode(ICON_MODE)
                self.grid.setWordWrap(True)
                self.grid.setSpacing(6)
                self.grid.setIconSize(QtCore.QSize(size, max(32, size // 2)))
                self.grid.setGridSize(QtCore.QSize(size + 24, max(100, size // 2 + 48)))

        def _grouping_changed(self, checked):
            self._settings["group_resolutions"] = bool(checked)
            self._save()
            self._populate_grid()

        def _view_mode_changed(self, index):
            self._settings["view_mode"] = "list" if index == 1 else "grid"
            self._apply_icon_size()
            self._save()
            # Repopulate so item text alignment matches the new mode.
            self._populate_grid()

        def _display_size_changed(self, value):
            self._settings["display_icon_size"] = int(value)
            self._apply_icon_size()
            if not self.icon_size_slider.isSliderDown():
                self._save()

        def _save(self):
            self._settings["last_folder"] = self._folder or ""
            self._settings["search_text"] = self.search.text()
            self._settings["include_subfolders"] = self.include_subfolders.isChecked()
            try:
                self._settings = config.save_config(self._settings)
            except OSError as error:
                self.status.setText("Could not save settings: {}".format(error))

        def _root_entries(self):
            return self._settings.get("roots", [])

        def _root_display_name(self, root):
            return root.get("label") or os.path.basename(root["path"]) or root["path"]

        def _root_by_path(self, path):
            return next(
                (root for root in self._root_entries() if root["path"] == path),
                None,
            )

        def _root_for_folder(self, folder=None):
            folder = os.path.abspath(folder or self._folder) if folder or self._folder else ""
            if not folder:
                return None
            hinted_root = self._root_by_path(self._folder_root)
            if hinted_root is not None:
                try:
                    if os.path.commonpath([folder, hinted_root["path"]]) == hinted_root["path"]:
                        return hinted_root
                except (OSError, ValueError):
                    pass
            matches = []
            for root in self._root_entries():
                root_path = root["path"]
                try:
                    inside = os.path.commonpath([folder, root_path]) == root_path
                except (OSError, ValueError):
                    inside = False
                if inside:
                    matches.append(root)
            return max(matches, key=lambda root: len(root["path"])) if matches else None

        def _format_count_text(self, root):
            return "Formats ({}/{})".format(
                len(root.get("extensions", ())), len(config.DEFAULT_EXTENSIONS)
            )

        def _make_root_formats_button(self, root):
            root_path = root["path"]
            button = QtWidgets.QToolButton(self.roots_list)
            button.setText(self._format_count_text(root))
            button.setPopupMode(INSTANT_POPUP)
            menu = QtWidgets.QMenu(button)
            enabled = set(root.get("extensions", ()))
            for extension in config.DEFAULT_EXTENSIONS:
                action = QAction(extension, menu)
                action.setCheckable(True)
                action.setChecked(extension in enabled)
                action.toggled.connect(
                    lambda checked, path=root_path, value=extension: self._root_format_changed(
                        path, value, checked
                    )
                )
                menu.addAction(action)
            button.setMenu(menu)
            self._root_format_buttons[root_path] = button
            return button

        def _color_icon(self, color):
            if not color:
                return QtGui.QIcon()
            qt_color = QtGui.QColor(color)
            if not qt_color.isValid():
                return QtGui.QIcon()
            pixmap = QtGui.QPixmap(14, 14)
            pixmap.fill(qt_color)
            return QtGui.QIcon(pixmap)

        def _rebuild_root_settings(self, selected_row=None):
            if selected_row is None and self.roots_list.currentItem() is not None:
                selected_row = self.roots_list.indexOfTopLevelItem(self.roots_list.currentItem())
            self._updating_roots = True
            self.roots_list.clear()
            self._root_format_buttons.clear()
            for root in self._root_entries():
                item = QtWidgets.QTreeWidgetItem(
                    self.roots_list,
                    [
                        root["path"],
                        root.get("label", ""),
                        root.get("color", ""),
                        self._format_count_text(root),
                    ],
                )
                item.setToolTip(0, root["path"])
                item.setFlags(item.flags() | ITEM_IS_EDITABLE)
                item.setIcon(2, self._color_icon(root.get("color", "")))
                self.roots_list.setItemWidget(
                    item, 3, self._make_root_formats_button(root)
                )
            self._updating_roots = False
            count = self.roots_list.topLevelItemCount()
            if count:
                row = max(0, min(count - 1, selected_row if selected_row is not None else 0))
                self.roots_list.setCurrentItem(self.roots_list.topLevelItem(row))
            self._root_selection_changed(self.roots_list.currentItem(), None)

        def _root_selection_changed(self, current, _previous):
            row = self.roots_list.indexOfTopLevelItem(current) if current is not None else -1
            count = len(self._root_entries())
            has_selection = 0 <= row < count
            self.settings_remove_root.setEnabled(has_selection)
            self.settings_move_up.setEnabled(has_selection and row > 0)
            self.settings_move_down.setEnabled(has_selection and row < count - 1)
            self.settings_color_root.setEnabled(has_selection)
            self.settings_clear_color.setEnabled(
                has_selection and bool(self._root_entries()[row].get("color"))
            )
            color = self._root_entries()[row].get("color", "") if has_selection else ""
            self.settings_color_root.setIcon(self._color_icon(color))

        def _root_item_changed(self, item, column):
            if self._updating_roots:
                return
            row = self.roots_list.indexOfTopLevelItem(item)
            if not 0 <= row < len(self._root_entries()):
                return
            if column != 1:
                root = self._root_entries()[row]
                expected = {
                    0: root["path"],
                    2: root.get("color", ""),
                    3: self._format_count_text(root),
                }.get(column, "")
                self._updating_roots = True
                item.setText(column, expected)
                self._updating_roots = False
                return
            label = item.text(1).strip()
            self._root_entries()[row]["label"] = label
            if item.text(1) != label:
                self._updating_roots = True
                item.setText(1, label)
                self._updating_roots = False
            self._save()
            self._rebuild_locations()

        def _root_item_double_clicked(self, item, column):
            if column == 2:
                self.roots_list.setCurrentItem(item)
                self._choose_root_color()

        def _root_scope(self, root):
            if root is None or not os.path.isdir(root.get("path", "")):
                return [], {}, ()
            paths = prepare.scan_root(root)
            dimensions = {}
            widths = {}
            for path in paths:
                value = resolution.probe_fast(path)
                dimensions[os.path.abspath(path)] = value
                widths[os.path.abspath(path)] = value[0] if value is not None else None
            return paths, widths, prepare.sensible_rungs(paths, dimensions)

        def _show_multi_root_menu(self, position, selected_roots):
            paths = []
            for root in selected_roots:
                for path in prepare.scan_root(root):
                    if path not in paths:
                        paths.append(path)
            available = bool(paths) and self._thread is None
            description = "{} folders".format(len(selected_roots))

            menu = QtWidgets.QMenu(self.roots_list)
            convert_action = QAction(
                "Convert to .rat ({} files)".format(len(paths)), menu
            )
            convert_action.setEnabled(available)
            convert_action.triggered.connect(
                lambda _checked=False, values=paths: self._start_rat_conversion(
                    values, "selected folder file"
                )
            )
            menu.addAction(convert_action)
            thumbnail_action = QAction(
                "Generate Thumbnails ({} files)".format(len(paths)), menu
            )
            thumbnail_action.setEnabled(available)
            thumbnail_action.triggered.connect(
                lambda _checked=False, values=paths: self._start_thumbnail_generation(
                    values, "folder file"
                )
            )
            menu.addAction(thumbnail_action)
            lowres_menu = menu.addMenu("Generate Low-Res Versions")
            self._populate_lowres_menu(lowres_menu, paths, description)
            menu.addSeparator()
            remove_action = QAction(
                "Remove {} entries".format(len(selected_roots)), menu
            )
            remove_action.triggered.connect(lambda _checked=False: self._remove_root())
            menu.addAction(remove_action)
            menu.exec(self.roots_list.viewport().mapToGlobal(position))

        def _set_root_include_in_all(self, root_path, included):
            root = self._root_by_path(root_path)
            if root is None:
                return
            root["include_in_all"] = bool(included)
            self._save()
            if self._folder == ALL_HDRI:
                self._populate_grid()

        def _focus_root_formats(self, root_path):
            root = self._root_by_path(root_path)
            if root is None:
                return
            row = self._root_entries().index(root)
            item = self.roots_list.topLevelItem(row)
            button = self._root_format_buttons.get(root_path)
            if item is None or button is None:
                return
            self.roots_list.setCurrentItem(item)
            button.setFocus()
            QtCore.QTimer.singleShot(0, button.showMenu)

        def _filtered_action_text(self, label, classification):
            if classification.state == "empty":
                return "{} (no supported images found)".format(label)
            if classification.state == "hidden-by-filter":
                count = classification.hidden_count
                return "{} ({} file{} hidden by format filter)".format(
                    label, count, "" if count == 1 else "s"
                )
            return label

        def _show_root_context_menu(self, position):
            item = self.roots_list.itemAt(position)
            if item is None:
                return
            if not item.isSelected():
                self.roots_list.clearSelection()
                item.setSelected(True)
            self.roots_list.setCurrentItem(item)
            rows = self._selected_root_rows()
            roots = self._root_entries()
            if len(rows) > 1:
                self._show_multi_root_menu(position, [roots[row] for row in rows])
                return
            row = self.roots_list.indexOfTopLevelItem(item)
            if not 0 <= row < len(roots):
                return
            root = roots[row]
            paths, widths, rungs = self._root_scope(root)
            classification = prepare.classify_root_scan(root, paths)
            available = bool(paths) and self._thread is None

            menu = QtWidgets.QMenu(self.roots_list)
            menu.setToolTipsVisible(True)
            convert_label = "Convert to .rat"
            if classification.state == "only-rat":
                convert_label += " (all matching files are already .rat)"
            else:
                convert_label = self._filtered_action_text(
                    convert_label, classification
                )
            convert_action = QAction(convert_label, menu)
            convert_action.setEnabled(
                available and classification.state != "only-rat"
            )
            convert_action.triggered.connect(
                lambda _checked=False, root_path=root["path"], values=paths: self._confirm_root_action(
                    root_path, values, {}, True, (), "Convert to .rat"
                )
            )
            menu.addAction(convert_action)

            thumbnail_action = QAction(
                self._filtered_action_text("Generate Thumbnails", classification),
                menu,
            )
            thumbnail_action.setEnabled(available)
            thumbnail_action.triggered.connect(
                lambda _checked=False, values=paths: self._start_thumbnail_generation(
                    values, "folder file"
                )
            )
            menu.addAction(thumbnail_action)

            lowres_label = self._filtered_action_text(
                "Generate Low-Res Versions", classification
            )
            lowres_menu = menu.addMenu(lowres_label)
            if not available or not rungs:
                empty = QAction("No lower standard rungs available", lowres_menu)
                empty.setEnabled(False)
                lowres_menu.addAction(empty)
                lowres_menu.setEnabled(False)
            else:
                unknown = sum(value is None for value in widths.values())
                for width in rungs:
                    known = {
                        path: value
                        for path, value in widths.items()
                        if value is not None
                    }
                    eligible, skipped = resize.partition_by_width(known, width)
                    label = "{} ({} resize, {} skip)".format(
                        resize.rung_label(width).upper(), len(eligible), len(skipped)
                    )
                    if unknown:
                        label += ", {} unknown".format(unknown)
                    action = QAction(label, lowres_menu)
                    action.triggered.connect(
                        lambda _checked=False, root_path=root["path"], values=paths, known_widths=widths, rung=width: self._confirm_root_action(
                            root_path,
                            values,
                            known_widths,
                            False,
                            (rung,),
                            "Generate {} Versions".format(
                                resize.rung_label(rung).upper()
                            ),
                        )
                    )
                    lowres_menu.addAction(action)

            menu.addSeparator()
            prepare_action = QAction(
                self._filtered_action_text("Prepare for Library…", classification),
                menu,
            )
            prepare_action.setEnabled(available)
            prepare_action.triggered.connect(
                lambda _checked=False, root_path=root["path"], values=paths, known_widths=widths, useful=rungs: self._show_prepare_dialog(
                    root_path, values, known_widths, useful
                )
            )
            menu.addAction(prepare_action)
            if classification.state == "hidden-by-filter":
                tooltip = (
                    "This folder's format selection excludes these supported files. "
                    "Use Edit Formats… to include them."
                )
                convert_action.setToolTip(tooltip)
                thumbnail_action.setToolTip(tooltip)
                lowres_menu.menuAction().setToolTip(tooltip)
                prepare_action.setToolTip(tooltip)
            menu.addSeparator()
            include_action = QAction('Include in "All HDRIs"', menu)
            include_action.setCheckable(True)
            include_action.setChecked(root.get("include_in_all", True))
            include_action.toggled.connect(
                lambda checked, root_path=root["path"]: self._set_root_include_in_all(
                    root_path, checked
                )
            )
            menu.addAction(include_action)
            edit_formats = QAction("Edit Formats…", menu)
            edit_formats.triggered.connect(
                lambda _checked=False, root_path=root["path"]: self._focus_root_formats(
                    root_path
                )
            )
            menu.addAction(edit_formats)
            menu.exec(self.roots_list.viewport().mapToGlobal(position))

        def _dialog_result(self, dialog):
            execute = getattr(dialog, "exec", None) or dialog.exec_
            return _enum_value(execute())

        def _confirm_root_action(
            self, root_path, paths, widths, convert_rat, rungs, title
        ):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(title)
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(
                QtWidgets.QLabel(
                    "Process {} matching file{} recursively?".format(
                        len(paths), "" if len(paths) == 1 else "s"
                    )
                )
            )
            lowres_format = self._settings.get("prepare_lowres_format", "both")
            if rungs:
                format_labels = {
                    "native": "same as original",
                    "rat": ".rat (mipmapped)",
                    "both": "same as original + .rat (mipmapped)",
                }
                layout.addWidget(
                    QtWidgets.QLabel(
                        "Low-res output: {}".format(
                            format_labels.get(lowres_format, format_labels["both"])
                        )
                    )
                )
            add_roots = QtWidgets.QCheckBox(
                "Add generated subfolders to folder list"
            )
            add_roots.setChecked(
                bool(self._settings.get("prepare_auto_add_subfolders", True))
            )
            layout.addWidget(add_roots)
            generate_thumbnails = QtWidgets.QCheckBox(
                "Generate thumbnails when done"
            )
            generate_thumbnails.setChecked(
                bool(self._settings.get("prepare_generate_thumbnails", True))
            )
            layout.addWidget(generate_thumbnails)
            buttons = QtWidgets.QDialogButtonBox(DIALOG_OK | DIALOG_CANCEL)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if self._dialog_result(dialog) != DIALOG_ACCEPTED:
                return
            plan = prepare.build_pipeline_plan(
                root_path,
                paths,
                convert_to_rat=convert_rat,
                rungs=rungs,
                widths=widths or None,
                lowres_format=lowres_format,
            )
            self._start_root_prepare(
                plan,
                add_roots.isChecked(),
                generate_thumbnails.isChecked(),
                title,
            )

        def _show_prepare_dialog(self, root_path, paths, widths, useful_rungs):
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Prepare for Library")
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(
                QtWidgets.QLabel(
                    "Prepare {} matching file{} recursively:".format(
                        len(paths), "" if len(paths) == 1 else "s"
                    )
                )
            )
            convert_box = QtWidgets.QCheckBox("Convert ORIGINAL source files to .rat")
            convert_box.setChecked(True)
            layout.addWidget(convert_box)
            rung_boxes = []
            for width in useful_rungs:
                box = QtWidgets.QCheckBox(
                    "Generate {} versions".format(resize.rung_label(width).upper())
                )
                box.setChecked(True)
                layout.addWidget(box)
                rung_boxes.append((width, box))

            format_group = QtWidgets.QGroupBox("Low-res output format")
            format_layout = QtWidgets.QVBoxLayout(format_group)
            native_radio = QtWidgets.QRadioButton(
                "Same as original (.exr/.hdr/…)"
            )
            rat_radio = QtWidgets.QRadioButton(".rat (mipmapped)")
            both_radio = QtWidgets.QRadioButton("Both")
            format_layout.addWidget(native_radio)
            format_layout.addWidget(rat_radio)
            format_layout.addWidget(both_radio)
            explanation = QtWidgets.QLabel(
                "Low-res versions are always rendered from the original pixels; "
                "this only chooses the saved format."
            )
            explanation.setWordWrap(True)
            format_layout.addWidget(explanation)
            selected_format = self._settings.get("prepare_lowres_format", "both")
            native_radio.setChecked(selected_format == "native")
            rat_radio.setChecked(selected_format == "rat")
            both_radio.setChecked(selected_format not in ("native", "rat"))
            layout.addWidget(format_group)
            add_roots = QtWidgets.QCheckBox(
                "Add generated subfolders to folder list"
            )
            add_roots.setChecked(
                bool(self._settings.get("prepare_auto_add_subfolders", True))
            )
            layout.addWidget(add_roots)
            generate_thumbnails = QtWidgets.QCheckBox(
                "Generate thumbnails when done"
            )
            generate_thumbnails.setChecked(
                bool(self._settings.get("prepare_generate_thumbnails", True))
            )
            layout.addWidget(generate_thumbnails)
            buttons = QtWidgets.QDialogButtonBox(DIALOG_OK | DIALOG_CANCEL)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if self._dialog_result(dialog) != DIALOG_ACCEPTED:
                return
            rungs = tuple(width for width, box in rung_boxes if box.isChecked())
            lowres_format = (
                "native"
                if native_radio.isChecked()
                else "rat"
                if rat_radio.isChecked()
                else "both"
            )
            if not convert_box.isChecked() and not rungs:
                self.status.setText("Choose at least one preparation action.")
                return
            plan = prepare.build_pipeline_plan(
                root_path,
                paths,
                convert_to_rat=convert_box.isChecked(),
                rungs=rungs,
                widths=widths,
                lowres_format=lowres_format,
            )
            self._start_root_prepare(
                plan,
                add_roots.isChecked(),
                generate_thumbnails.isChecked(),
                "Prepare for Library",
            )

        def _root_format_changed(self, root_path, extension, checked):
            root = self._root_by_path(root_path)
            if root is None:
                return
            enabled = set(root.get("extensions", ()))
            if checked:
                enabled.add(extension)
            else:
                enabled.discard(extension)
            root["extensions"] = [
                value for value in config.DEFAULT_EXTENSIONS if value in enabled
            ]
            button = self._root_format_buttons.get(root_path)
            if button is not None:
                button.setText(self._format_count_text(root))
            is_current = self._root_for_folder() is root
            self._save()
            if is_current:
                self._sync_browse_formats()
                self._populate_grid()

        def _add_root(self):
            start = self._folder or str(Path.home())
            selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Add HDRI folder", start)
            if not selected:
                return
            selected = os.path.abspath(selected)
            additions = [selected]
            try:
                subfolders = sorted(
                    (
                        entry.path
                        for entry in os.scandir(selected)
                        # Follow symlinks: NAS libraries commonly link set folders.
                        if entry.is_dir() and files._wanted_directory(entry.name)
                    ),
                    key=str.lower,
                )
            except OSError:
                subfolders = []
            if subfolders:
                box = QtWidgets.QMessageBox(self)
                box.setWindowTitle("Add HDRI folder")
                box.setText(
                    "{} contains {} subfolder{}.".format(
                        os.path.basename(selected) or selected,
                        len(subfolders),
                        "" if len(subfolders) == 1 else "s",
                    )
                )
                box.setInformativeText("Add subfolders as separate library entries?")
                accept_role = _enum(QtWidgets.QMessageBox, "ButtonRole", "AcceptRole")
                single = box.addButton("This folder only", accept_role)
                choose = box.addButton("Choose subfolders…", accept_role)
                cancel = box.addButton(
                    _enum(QtWidgets.QMessageBox, "StandardButton", "Cancel")
                )
                box.setDefaultButton(single)
                box.exec()
                if box.clickedButton() is cancel:
                    return
                if box.clickedButton() is choose:
                    chosen = self._pick_subfolder_entries(selected)
                    if chosen is None:
                        return
                    if chosen:
                        additions = chosen
            roots = self._root_entries()
            known = {root["path"] for root in roots}
            row = None
            for path in additions:
                if path in known:
                    if row is None:
                        row = next(
                            index
                            for index, root in enumerate(roots)
                            if root["path"] == path
                        )
                    continue
                roots.append(
                    {
                        "path": path,
                        "label": "",
                        "color": "",
                        "extensions": list(config.DEFAULT_EXTENSIONS),
                        "include_in_all": True,
                    }
                )
                known.add(path)
                if row is None:
                    row = len(roots) - 1
            selected = additions[0]
            if row is None:
                row = 0
            self.status.setText(
                "Added {} folder entr{}.".format(
                    len(additions), "y" if len(additions) == 1 else "ies"
                )
            )
            self._folder = selected
            self._folder_root = selected
            self._save()
            self._rebuild_root_settings(row)
            self._rebuild_locations()

        def _selected_root_rows(self):
            roots = self._root_entries()
            rows = {
                self.roots_list.indexOfTopLevelItem(item)
                for item in self.roots_list.selectedItems()
            }
            current = self.roots_list.currentItem()
            if not rows and current is not None:
                rows = {self.roots_list.indexOfTopLevelItem(current)}
            return sorted(row for row in rows if 0 <= row < len(roots))

        def _pick_subfolder_entries(self, selected):
            """Show the full nested folder tree with checkboxes.

            Returns ``None`` on cancel, ``[]`` for "this folder only", or the
            checked folder paths. Only the first level starts checked so deep
            trees do not explode into hundreds of entries by accident.
            """

            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle("Add HDRI folder")
            dialog.resize(460, 420)
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.addWidget(
                QtWidgets.QLabel(
                    "Check every folder that should become its own"
                    " library entry under {}:".format(
                        os.path.basename(selected) or selected
                    )
                )
            )
            tree = QtWidgets.QTreeWidget()
            tree.setHeaderHidden(True)
            checkable = _enum(QtCore.Qt, "ItemFlag", "ItemIsUserCheckable")
            checked = _enum(QtCore.Qt, "CheckState", "Checked")
            unchecked = _enum(QtCore.Qt, "CheckState", "Unchecked")

            def add_children(parent_item, directory, depth):
                try:
                    entries = sorted(
                        (
                            entry.path
                            for entry in os.scandir(directory)
                            if entry.is_dir() and files._wanted_directory(entry.name)
                        ),
                        key=str.lower,
                    )
                except OSError:
                    return
                for path in entries:
                    item = QtWidgets.QTreeWidgetItem(
                        parent_item, [os.path.basename(path)]
                    )
                    item.setToolTip(0, path)
                    item.setData(0, USER_ROLE, path)
                    item.setFlags(item.flags() | checkable)
                    item.setCheckState(0, checked if depth == 0 else unchecked)
                    add_children(item, path, depth + 1)

            add_children(tree, selected, 0)
            tree.expandToDepth(0)
            layout.addWidget(tree, 1)

            def checked_paths():
                result = []
                iterator = QtWidgets.QTreeWidgetItemIterator(tree)
                while iterator.value():
                    item = iterator.value()
                    if _enum_value(item.checkState(0)) == _enum_value(checked):
                        result.append(item.data(0, USER_ROLE))
                    iterator += 1
                return result

            select_bar = QtWidgets.QHBoxLayout()
            all_button = QtWidgets.QPushButton("All")
            top_button = QtWidgets.QPushButton("Top level")
            none_button = QtWidgets.QPushButton("None")
            select_bar.addWidget(all_button)
            select_bar.addWidget(top_button)
            select_bar.addWidget(none_button)
            select_bar.addStretch(1)
            layout.addLayout(select_bar)

            def set_all(state, top_state=None):
                iterator = QtWidgets.QTreeWidgetItemIterator(tree)
                while iterator.value():
                    item = iterator.value()
                    top = item.parent() is None
                    item.setCheckState(
                        0, top_state if top and top_state is not None else state
                    )
                    iterator += 1

            all_button.clicked.connect(lambda: set_all(checked))
            top_button.clicked.connect(lambda: set_all(unchecked, top_state=checked))
            none_button.clicked.connect(lambda: set_all(unchecked))

            button_bar = QtWidgets.QHBoxLayout()
            single_button = QtWidgets.QPushButton("This folder only")
            add_button = QtWidgets.QPushButton()
            cancel_button = QtWidgets.QPushButton("Cancel")
            button_bar.addWidget(single_button)
            button_bar.addStretch(1)
            button_bar.addWidget(add_button)
            button_bar.addWidget(cancel_button)
            layout.addLayout(button_bar)

            def refresh_add_label():
                count = len(checked_paths())
                add_button.setText("Add selected ({})".format(count))
                add_button.setEnabled(count > 0)

            tree.itemChanged.connect(lambda _item, _column: refresh_add_label())
            refresh_add_label()
            add_button.setDefault(True)

            single_button.clicked.connect(lambda: dialog.done(1))
            add_button.clicked.connect(lambda: dialog.done(2))
            cancel_button.clicked.connect(dialog.reject)

            result = self._dialog_result(dialog)
            if result == 1:
                return []
            if result == 2:
                return checked_paths()
            return None

        def _remove_root(self):
            rows = self._selected_root_rows()
            roots = self._root_entries()
            if not rows:
                return
            for row in reversed(rows):
                removed = roots.pop(row)["path"]
                if self._folder == removed or self._folder.startswith(removed + os.sep):
                    self._folder = ""
                    self._folder_root = ""
            if not self._folder and roots:
                self._folder = roots[0]["path"]
                self._folder_root = roots[0]["path"]
            self._save()
            self._rebuild_root_settings(min(rows[0], len(roots) - 1))
            self._rebuild_locations()

        def _move_root(self, offset):
            item = self.roots_list.currentItem()
            row = self.roots_list.indexOfTopLevelItem(item) if item is not None else -1
            destination = row + int(offset)
            roots = self._root_entries()
            if not 0 <= row < len(roots) or not 0 <= destination < len(roots):
                return
            roots[row], roots[destination] = roots[destination], roots[row]
            self._save()
            self._rebuild_root_settings(destination)
            self._rebuild_locations()

        def _choose_root_color(self):
            item = self.roots_list.currentItem()
            row = self.roots_list.indexOfTopLevelItem(item) if item is not None else -1
            if not 0 <= row < len(self._root_entries()):
                return
            current = QtGui.QColor(self._root_entries()[row].get("color", ""))
            color = QtWidgets.QColorDialog.getColor(current, self, "Choose folder color")
            if color.isValid():
                self._set_selected_root_color(color.name())

        def _set_selected_root_color(self, color):
            item = self.roots_list.currentItem()
            row = self.roots_list.indexOfTopLevelItem(item) if item is not None else -1
            if not 0 <= row < len(self._root_entries()):
                return
            self._root_entries()[row]["color"] = color
            self._save()
            self._rebuild_root_settings(row)
            self._rebuild_locations()

        def _add_tree_directory(self, path, parent, root, combo_prefix, seen_combo_paths):
            try:
                directories = sorted(
                    (
                        entry
                        for entry in os.scandir(path)
                        if entry.is_dir(follow_symlinks=False)
                        and files._wanted_directory(entry.name)
                    ),
                    key=lambda entry: entry.name.lower(),
                )
            except OSError:
                return
            icon = self._color_icon(root.get("color", ""))
            for directory in directories:
                item = QtWidgets.QTreeWidgetItem(parent, [directory.name])
                item.setData(0, USER_ROLE, directory.path)
                item.setData(0, ROOT_ROLE, root["path"])
                item.setToolTip(0, directory.path)
                item.setIcon(0, icon)
                relative = os.path.relpath(directory.path, root["path"]).replace(os.sep, " / ")
                self._add_combo_folder(
                    directory.path,
                    "{} / {}".format(combo_prefix, relative),
                    root,
                    seen_combo_paths,
                )
                self._add_tree_directory(
                    directory.path, item, root, combo_prefix, seen_combo_paths
                )

        def _add_combo_folder(self, path, label, root, seen_paths):
            key = (root["path"], path)
            if key in seen_paths:
                return
            seen_paths.add(key)
            self.location_combo.addItem(self._color_icon(root.get("color", "")), label, path)
            index = self.location_combo.count() - 1
            self.location_combo.setItemData(index, path, TOOLTIP_ROLE)
            self.location_combo.setItemData(index, root["path"], ROOT_ROLE)

        def _combo_folder_index(self, path, root_path=""):
            for index in range(self.location_combo.count()):
                if self.location_combo.itemData(index, USER_ROLE) != path:
                    continue
                if not root_path or self.location_combo.itemData(index, ROOT_ROLE) == root_path:
                    return index
            return -1

        def _rebuild_locations(self):
            self._syncing_location = True
            self.folder_tree.blockSignals(True)
            self.location_combo.blockSignals(True)
            self.folder_tree.clear()
            self.location_combo.clear()
            selected_item = None
            seen_combo_paths = set()
            if self._root_entries():
                all_item = QtWidgets.QTreeWidgetItem(self.folder_tree, ["All HDRIs"])
                all_item.setToolTip(0, "Every texture from included library roots")
                all_item.setData(0, USER_ROLE, ALL_HDRI)
                all_item.setData(0, ROOT_ROLE, ALL_HDRI)
                self.location_combo.addItem("All HDRIs", ALL_HDRI)
                self.location_combo.setItemData(
                    self.location_combo.count() - 1, ALL_HDRI, ROOT_ROLE
                )
                if self._folder == ALL_HDRI:
                    selected_item = all_item
            preferred_root_path = self._folder_root
            if self._folder and not preferred_root_path:
                preferred_root = self._root_for_folder(self._folder)
                if preferred_root is not None:
                    preferred_root_path = preferred_root["path"]
            for root in self._root_entries():
                path = root["path"]
                if not os.path.isdir(path):
                    continue
                label = self._root_display_name(root)
                item = QtWidgets.QTreeWidgetItem(self.folder_tree, [label])
                item.setToolTip(0, path)
                item.setData(0, USER_ROLE, path)
                item.setData(0, ROOT_ROLE, path)
                item.setIcon(0, self._color_icon(root.get("color", "")))
                self._add_combo_folder(path, label, root, seen_combo_paths)
                self._add_tree_directory(path, item, root, label, seen_combo_paths)
                if self._folder == path:
                    selected_item = item

            if self._folder and selected_item is None:
                iterator = QtWidgets.QTreeWidgetItemIterator(self.folder_tree)
                while iterator.value():
                    item = iterator.value()
                    if item.data(0, USER_ROLE) == self._folder and (
                        not preferred_root_path
                        or item.data(0, ROOT_ROLE) == preferred_root_path
                    ):
                        selected_item = item
                        break
                    iterator += 1
            if selected_item is None and self.folder_tree.topLevelItemCount():
                # Prefer the first real root over the aggregate view so a fresh
                # session does not start with a full multi-root scan.
                index = 1 if self.folder_tree.topLevelItemCount() > 1 else 0
                selected_item = self.folder_tree.topLevelItem(index)
                self._folder = selected_item.data(0, USER_ROLE)
            if selected_item is not None:
                self._folder_root = selected_item.data(0, ROOT_ROLE)
                self.folder_tree.setCurrentItem(selected_item)
                self.folder_tree.scrollToItem(selected_item)
            else:
                self._folder_root = ""
            combo_index = self._combo_folder_index(self._folder, self._folder_root)
            if combo_index >= 0:
                self.location_combo.setCurrentIndex(combo_index)
            self.folder_tree.blockSignals(False)
            self.location_combo.blockSignals(False)
            self._syncing_location = False
            self._sync_browse_formats()
            self._populate_grid()

        def _folder_changed(self, current, _previous):
            if current is None or self._syncing_location:
                return
            self._set_folder(
                current.data(0, USER_ROLE),
                root_path=current.data(0, ROOT_ROLE),
                sync_tree=False,
            )

        def _dropdown_folder_changed(self, index):
            if index < 0 or self._syncing_location:
                return
            self._set_folder(
                self.location_combo.itemData(index, USER_ROLE),
                root_path=self.location_combo.itemData(index, ROOT_ROLE),
                sync_combo=False,
            )

        def _set_folder(self, path, root_path="", sync_tree=True, sync_combo=True):
            if not path:
                return
            self._folder = path
            self._folder_root = root_path
            if not self._folder_root:
                root = self._root_for_folder(path)
                self._folder_root = root["path"] if root is not None else ""
            self._syncing_location = True
            if sync_combo:
                index = self._combo_folder_index(path, self._folder_root)
                if index >= 0:
                    self.location_combo.setCurrentIndex(index)
            if sync_tree:
                iterator = QtWidgets.QTreeWidgetItemIterator(self.folder_tree)
                while iterator.value():
                    item = iterator.value()
                    if item.data(0, USER_ROLE) == path and (
                        not self._folder_root
                        or item.data(0, ROOT_ROLE) == self._folder_root
                    ):
                        self.folder_tree.setCurrentItem(item)
                        break
                    iterator += 1
            self._syncing_location = False
            self._sync_browse_formats()
            self._save()
            self._populate_grid()

        def set_location_ui_mode(self, mode, save=True):
            """Switch Browse location controls live; useful to host integrations too."""

            if mode not in ("sidebar", "dropdown"):
                raise ValueError("location UI mode must be 'sidebar' or 'dropdown'")
            self._settings["location_ui_mode"] = mode
            self.sidebar_radio.blockSignals(True)
            self.dropdown_radio.blockSignals(True)
            self.sidebar_radio.setChecked(mode == "sidebar")
            self.dropdown_radio.setChecked(mode == "dropdown")
            self.sidebar_radio.blockSignals(False)
            self.dropdown_radio.blockSignals(False)
            sidebar = mode == "sidebar"
            self.folder_tree.setVisible(sidebar)
            self.location_label.setVisible(not sidebar)
            self.location_combo.setVisible(not sidebar)
            if save:
                self._save()

        def _checked_extensions(self):
            return [
                extension
                for extension, action in self._format_actions.items()
                if action.isChecked()
            ]

        def _sync_browse_formats(self):
            root = self._root_for_folder()
            enabled = set(root.get("extensions", ())) if root is not None else set()
            for extension, action in self._format_actions.items():
                action.blockSignals(True)
                action.setVisible(True)
                action.setChecked(extension in enabled)
                action.blockSignals(False)
            self.format_button.setToolTip(
                "Formats for {}".format(self._root_display_name(root)) if root else ""
            )
            self._update_format_button()

        def _update_format_button(self):
            selected = len(self._checked_extensions())
            available = len(config.DEFAULT_EXTENSIONS)
            self.format_button.setText("Formats ({}/{})".format(selected, available))
            self.format_button.setEnabled(self._root_for_folder() is not None)

        def _formats_changed(self, _checked=False):
            root = self._root_for_folder()
            if root is None:
                self._sync_browse_formats()
                return
            root_path = root["path"]
            root["extensions"] = self._checked_extensions()
            self._update_format_button()
            self._save()
            button = self._root_format_buttons.get(root_path)
            saved_root = self._root_by_path(root_path)
            if button is not None and saved_root is not None:
                button.setText(self._format_count_text(saved_root))
            self._populate_grid()

        def _thumbnail_settings_changed(self, _value=None):
            self._settings["thumbnail_size"] = self.thumbnail_size_spin.value()
            self._settings["thumbnail_workers"] = self.thumbnail_workers_spin.value()
            self._settings["thumbnail_tonemap"] = (
                self.thumbnail_tonemap_combo.currentData() or thumbs.DEFAULT_TONEMAP
            )
            self._settings["assign_resolution"] = (
                self.assign_resolution_combo.currentData() or variants.DEFAULT_ASSIGN
            )
            self._save()
            self._apply_icon_size()
            self._populate_grid()

        def _clear_thumbnail_cache(self):
            removed, freed = thumbs.clear_cache()
            self.status.setText(
                "Cleared {} cached thumbnail{} ({:.1f} MB).".format(
                    removed, "" if removed == 1 else "s", freed / (1024 * 1024)
                )
            )
            self._populate_grid()

        def _update_rat_settings_enabled(self):
            self.rat_subfolder_name.setEnabled(self.rat_subfolder_radio.isChecked())

        def _rat_settings_changed(self, _value=None):
            mode = "subfolder" if self.rat_subfolder_radio.isChecked() else "alongside"
            name = self.rat_subfolder_name.text().strip()
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                name = "rat"
                self.rat_subfolder_name.setText(name)
            self._settings["rat_output_mode"] = mode
            self._settings["rat_subfolder_name"] = name
            self._settings["rat_overwrite_existing"] = self.rat_overwrite.isChecked()
            self._update_rat_settings_enabled()
            self._save()

        def _lowres_settings_changed(self, _value=None):
            self._settings["lowres_output_mode"] = (
                "subfolder" if self.lowres_subfolder_radio.isChecked() else "alongside"
            )
            self._settings["lowres_also_rat"] = self.lowres_also_rat.isChecked()
            self._settings["lowres_overwrite_existing"] = self.lowres_overwrite.isChecked()
            self._save()

        def _include_changed(self, _checked=False):
            self._save()
            self._populate_grid()

        def _search_changed(self, _text):
            self._save()
            self._populate_grid()

        def _refresh(self):
            self._rebuild_locations()
            self.status.setText("Folder list refreshed.")

        def _all_hdri_paths(self):
            found = set()
            for root in self._root_entries():
                if not root.get("include_in_all", True):
                    continue
                extensions = root.get("extensions", ())
                if not extensions or not os.path.isdir(root["path"]):
                    continue
                found.update(
                    files.scan_files(root["path"], extensions=extensions, recursive=True)
                )
            return sorted(found, key=lambda value: (os.path.basename(value).lower(), value))

        def _populate_grid(self):
            self.grid.clear()
            if self._folder == ALL_HDRI:
                self._all_files = self._all_hdri_paths()
            elif not self._folder or not os.path.isdir(self._folder):
                self._all_files = []
                if not self._root_entries():
                    self.status.setText("Add an HDRI root in Settings to begin.")
                return
            else:
                root = self._root_for_folder()
                extensions = root.get("extensions", ()) if root is not None else ()
                self._all_files = (
                    files.scan_files(
                        self._folder,
                        extensions=extensions,
                        recursive=self.include_subfolders.isChecked(),
                    )
                    if extensions
                    else []
                )
            query = self.search.text().strip().lower()
            visible = [path for path in self._all_files if query in os.path.basename(path).lower()]
            fallback_icon = self.style().standardIcon(STANDARD_FILE_ICON)
            size = int(self._settings.get("thumbnail_size", 256))
            tonemap = self._settings.get("thumbnail_tonemap", thumbs.DEFAULT_TONEMAP)
            center_text = self._settings.get("view_mode", "grid") != "list"

            def make_item(text, tooltip, assign_path, icon_paths, group_paths=None):
                item = QtWidgets.QListWidgetItem(text)
                if center_text:
                    item.setTextAlignment(ALIGN_CENTER)
                item.setToolTip(tooltip)
                item.setData(USER_ROLE, assign_path)
                if group_paths:
                    item.setData(GROUP_ROLE, group_paths)
                cached = None
                for candidate in icon_paths:
                    cached = thumbs.cached_thumbnail(candidate, size=size, tonemap=tonemap)
                    if cached:
                        break
                item.setIcon(QtGui.QIcon(cached) if cached else fallback_icon)
                self.grid.addItem(item)

            if self._settings.get("group_resolutions", False):
                groups = variants.build_groups(visible)
                for group in groups:
                    lines = []
                    for variant in group.variants:
                        lines.append("{}: {}".format(variant.label, variant.path))
                        lines.extend("rat: {}".format(c) for c in variant.companions)
                    single = len(group.variants) == 1
                    text = (
                        os.path.basename(group.variants[0].path)
                        if single
                        else "{} ({})".format(group.name, group.badge())
                    )
                    # Smallest variant renders fastest and shows identical pixels.
                    icon_order = sorted(
                        group.variants,
                        key=lambda variant: variants.token_width(variant.token)
                        or (1 << 30),
                    )
                    make_item(
                        text,
                        "\n".join(lines),
                        group.variants[0].path,
                        [variant.path for variant in icon_order],
                        group_paths=group.paths,
                    )
                self.status.setText(
                    "{} HDRI{} ({} file{})".format(
                        len(groups),
                        "" if len(groups) == 1 else "s",
                        len(visible),
                        "" if len(visible) == 1 else "s",
                    )
                )
            else:
                for path in visible:
                    make_item(os.path.basename(path), path, path, [path])
                self.status.setText(
                    "{} texture{}".format(len(visible), "" if len(visible) == 1 else "s")
                )

        def _assign_item(self, item):
            group_paths = item.data(GROUP_ROLE)
            target = item.data(USER_ROLE)
            if group_paths and len(group_paths) > 1:
                group = next(
                    (g for g in variants.build_groups(group_paths) if len(g.variants) > 1),
                    None,
                )
                if group is not None:
                    widths = {}
                    for path in group.paths:
                        dimensions = resolution.probe_fast(path)
                        if dimensions is not None:
                            widths[path] = dimensions[0]
                    target = variants.pick_variant(
                        group,
                        self._settings.get("assign_resolution", variants.DEFAULT_ASSIGN),
                        widths,
                    ).path
            self._assign_path(target)

        def _copy_path(self, path):
            clipboard = QtWidgets.QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(path)
            self.status.setText("Copied {}".format(path))

        def _assign_path(self, path):
            result = assign.assign_texture(path)
            # Always echo the concrete file so resolution picks are never silent.
            self.status.setText(
                "{} — {}".format(os.path.basename(path), result.message)
            )

        def _show_grid_context_menu(self, position):
            clicked = self.grid.itemAt(position)
            if clicked is not None and not clicked.isSelected():
                self.grid.clearSelection()
                clicked.setSelected(True)
            selected = []
            for entry in self.grid.selectedItems():
                group_paths = entry.data(GROUP_ROLE)
                for value in group_paths or ([entry.data(USER_ROLE)] if entry.data(USER_ROLE) else []):
                    if value not in selected:
                        selected.append(value)
            menu = QtWidgets.QMenu(self.grid)
            if clicked is not None:
                clicked_paths = clicked.data(GROUP_ROLE) or (
                    [clicked.data(USER_ROLE)] if clicked.data(USER_ROLE) else []
                )
                entries = []
                for group in variants.build_groups(clicked_paths):
                    for variant in group.variants:
                        entries.append(
                            (
                                "{} — {}".format(
                                    variant.label, os.path.basename(variant.path)
                                ),
                                variant.path,
                            )
                        )
                        entries.extend(
                            (
                                "{} rat — {}".format(
                                    variant.label, os.path.basename(companion)
                                ),
                                companion,
                            )
                            for companion in variant.companions
                        )
                if len(entries) > 1:
                    assign_menu = menu.addMenu("Assign Resolution")
                    copy_menu = menu.addMenu("Copy Path")
                    for label, path in entries:
                        assign_action = QAction(label, assign_menu)
                        assign_action.triggered.connect(
                            lambda _checked=False, value=path: self._assign_path(value)
                        )
                        assign_menu.addAction(assign_action)
                        copy_action = QAction(label, copy_menu)
                        copy_action.triggered.connect(
                            lambda _checked=False, value=path: self._copy_path(value)
                        )
                        copy_menu.addAction(copy_action)
                elif entries:
                    copy_action = QAction("Copy Path", menu)
                    copy_action.triggered.connect(
                        lambda _checked=False, value=entries[0][1]: self._copy_path(value)
                    )
                    menu.addAction(copy_action)
                menu.addSeparator()
            action = QAction("Convert to .rat", menu)
            action.setEnabled(bool(selected) and self._thread is None)
            action.triggered.connect(
                lambda _checked=False, paths=selected: self._start_rat_conversion(
                    paths, "selected texture"
                )
            )
            menu.addAction(action)
            thumb_selected = QAction(
                "Generate Thumbnails ({} selected)".format(len(selected)), menu
            )
            thumb_selected.setEnabled(bool(selected) and self._thread is None)
            thumb_selected.triggered.connect(
                lambda _checked=False, paths=selected: self._start_thumbnail_generation(
                    paths, "selected thumbnail"
                )
            )
            menu.addAction(thumb_selected)
            all_files = list(self._all_files)
            thumb_all = QAction(
                "Generate Thumbnails (all {} in folder)".format(len(all_files)), menu
            )
            thumb_all.setEnabled(bool(all_files) and self._thread is None)
            thumb_all.triggered.connect(
                lambda _checked=False, paths=all_files: self._start_thumbnail_generation(
                    paths, "thumbnail"
                )
            )
            menu.addAction(thumb_all)
            lowres_menu = menu.addMenu("Create Low-Res Versions")
            self._populate_lowres_menu(lowres_menu, selected, "selected texture")
            menu.exec(self.grid.viewport().mapToGlobal(position))

        def _folder_scope_paths(self):
            if self._folder == ALL_HDRI:
                return self._all_hdri_paths()
            if not self._folder or not os.path.isdir(self._folder):
                return []
            root = self._root_for_folder()
            extensions = root.get("extensions", ()) if root is not None else ()
            return (
                files.scan_files(
                    self._folder,
                    extensions=extensions,
                    recursive=self.include_subfolders.isChecked(),
                )
                if extensions
                else []
            )

        def _populate_lowres_menu(self, menu, paths, description):
            paths = list(paths)
            if not paths or self._thread is not None:
                menu.setEnabled(False)
                return
            widths = {}
            unknown = 0
            for path in paths:
                dimensions = resolution.probe_fast(path)
                if dimensions is None:
                    unknown += 1
                else:
                    widths[path] = dimensions[0]
            rungs = (
                resize.STANDARD_RUNGS
                if unknown
                else resize.rungs_below_largest(widths.values())
            )
            if not rungs:
                action = QAction("No lower standard rungs available", menu)
                action.setEnabled(False)
                menu.addAction(action)
                menu.setEnabled(False)
                return
            for width in rungs:
                eligible, skipped = resize.partition_by_width(widths, width)
                label = resize.rung_label(width).upper()
                counts = "{} ({} resize, {} skip)".format(
                    label, len(eligible), len(skipped)
                )
                if unknown:
                    counts += ", {} unknown".format(unknown)
                action = QAction(counts, menu)
                action.triggered.connect(
                    lambda _checked=False, values=paths, rung=width, scope=description: self._start_lowres(
                        values, rung, scope
                    )
                )
                menu.addAction(action)

        def _show_folder_lowres_menu(self):
            if self._thread is not None:
                return
            paths = self._folder_scope_paths()
            if not paths:
                self.status.setText("No textures match the current low-res scope.")
                return
            menu = QtWidgets.QMenu(self.lowres_folder_button)
            self._populate_lowres_menu(menu, paths, "folder texture")
            menu.exec(
                self.lowres_folder_button.mapToGlobal(
                    QtCore.QPoint(0, self.lowres_folder_button.height())
                )
            )

        def _start_folder_conversion(self):
            if self._thread is not None:
                return
            if not self._folder or not os.path.isdir(self._folder):
                self.status.setText("Choose a valid folder before converting to RAT.")
                return
            paths = self._folder_scope_paths()
            self._start_rat_conversion(paths, "folder texture")

        def _start_rat_conversion(self, paths, description):
            if self._thread is not None:
                return
            paths = list(paths)
            if not paths:
                self.status.setText("No textures match the current RAT conversion scope.")
                return

            thread = QtCore.QThread()
            worker = RatConversionWorker(
                paths,
                self._settings.get("rat_output_mode", "alongside"),
                self._settings.get("rat_subfolder_name", "rat"),
                bool(self._settings.get("rat_overwrite_existing", False)),
                int(self._settings.get("thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS)),
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.converted.connect(self._rat_converted)
            worker.skipped.connect(self._rat_skipped)
            worker.problem.connect(self._rat_problem)
            worker.progress.connect(self._job_progress)
            worker.finished.connect(self._conversion_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: self._thread_finished(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._job_kind = "rat"
            self._generation_errors = 0
            self._conversion_skipped = 0
            self._begin_job_progress("Converting", len(paths))
            self._set_job_controls(True)
            workers = min(len(paths), worker._workers)
            self.status.setText(
                "Converting {} {}{} to RAT with {} worker{}…".format(
                    len(paths),
                    description,
                    "" if len(paths) == 1 else "s",
                    workers,
                    "" if workers == 1 else "s",
                )
            )
            thread.start()

        def _start_lowres(self, paths, width, description):
            if self._thread is not None:
                return
            paths = list(paths)
            if not paths:
                self.status.setText("No textures match the current low-res scope.")
                return

            thread = QtCore.QThread()
            worker = LowResWorker(
                paths,
                width,
                self._settings.get("lowres_output_mode", "alongside"),
                bool(self._settings.get("lowres_also_rat", False)),
                bool(self._settings.get("lowres_overwrite_existing", False)),
                int(self._settings.get("thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS)),
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.resized.connect(self._lowres_resized)
            worker.skipped.connect(self._lowres_skipped)
            worker.problem.connect(self._lowres_problem)
            worker.progress.connect(self._job_progress)
            worker.finished.connect(self._lowres_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: self._thread_finished(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._job_kind = "lowres"
            self._generation_errors = 0
            self._conversion_skipped = 0
            self._begin_job_progress("Creating low-res", len(paths))
            self._set_job_controls(True)
            workers = min(len(paths), worker._workers)
            self.status.setText(
                "Creating {} variants for {} {}{} with {} worker{}…".format(
                    resize.rung_label(width).upper(),
                    len(paths),
                    description,
                    "" if len(paths) == 1 else "s",
                    workers,
                    "" if workers == 1 else "s",
                )
            )
            thread.start()

        def _start_root_prepare(
            self, plan, add_roots, generate_thumbnails, description
        ):
            changed = False
            if self._settings.get("prepare_lowres_format", "both") != plan.lowres_format:
                self._settings["prepare_lowres_format"] = plan.lowres_format
                changed = True
            if bool(self._settings.get("prepare_auto_add_subfolders", True)) != bool(add_roots):
                self._settings["prepare_auto_add_subfolders"] = bool(add_roots)
                changed = True
            if bool(self._settings.get("prepare_generate_thumbnails", True)) != bool(
                generate_thumbnails
            ):
                self._settings["prepare_generate_thumbnails"] = bool(
                    generate_thumbnails
                )
                changed = True
            if changed:
                self._save()
            if self._thread is not None or not plan.total:
                if not plan.total:
                    self.status.setText("No matching work was found for this action.")
                return
            parent = self._root_by_path(plan.root)
            if parent is None:
                self.status.setText("The selected root is no longer in Settings.")
                return

            generated = [
                (folder, resize.rung_label(width), plan.lowres_format)
                for folder, width in zip(plan.generated_folders, plan.rungs)
            ]
            if (
                plan.convert_originals
                and not plan.resize_stages
                and self._settings.get("rat_output_mode", "alongside") == "subfolder"
            ):
                subfolder = self._settings.get("rat_subfolder_name", "rat")
                seen = set()
                for source in plan.sources:
                    folder = str(
                        convert.build_rat_target(source, "subfolder", subfolder).parent
                    )
                    if folder in seen:
                        continue
                    seen.add(folder)
                    try:
                        relative = Path(folder).relative_to(plan.root)
                        suffix = " ".join(relative.parts)
                    except ValueError:
                        suffix = subfolder
                    generated.append((folder, suffix, True))

            thread = QtCore.QThread()
            worker = PrepareWorker(plan, self._settings)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.problem.connect(self._prepare_problem)
            worker.progress.connect(self._job_progress)
            worker.finished.connect(self._prepare_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: self._thread_finished(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._job_kind = "prepare"
            self._generation_errors = 0
            self._prepare_context = {
                "parent_path": plan.root,
                "add_roots": bool(add_roots),
                "generated": generated,
                "description": description,
            }
            self._begin_job_progress("Preparing", plan.total)
            self._set_job_controls(True)
            self.status.setText(
                "{}: running {} queued operation{}…".format(
                    description, plan.total, "" if plan.total == 1 else "s"
                )
            )
            thread.start()

        def _start_generation(self):
            self._start_thumbnail_generation(self._all_files, "thumbnail")

        def _start_thumbnail_generation(self, paths, description):
            if self._thread is not None:
                return
            size = int(self._settings.get("thumbnail_size", 256))
            tonemap = self._settings.get("thumbnail_tonemap", thumbs.DEFAULT_TONEMAP)
            pending = [
                path
                for path in paths
                if not thumbs.cached_thumbnail(path, size=size, tonemap=tonemap)
            ]
            if not pending:
                self.status.setText("All thumbnails for this scope are cached.")
                return

            thread = QtCore.QThread()
            worker = ThumbnailWorker(
                pending,
                size,
                int(self._settings.get("thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS)),
                tonemap=tonemap,
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.thumbnail_ready.connect(self._thumbnail_ready)
            worker.problem.connect(self._thumbnail_problem)
            worker.progress.connect(self._job_progress)
            worker.finished.connect(self._generation_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: self._thread_finished(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._job_kind = "thumbnails"
            self._generation_errors = 0
            self._begin_job_progress("Generating thumbnails", len(pending))
            self._set_job_controls(True)
            workers = min(len(pending), worker._workers)
            self.status.setText(
                "Generating {} {}{} with {} worker{}…".format(
                    len(pending),
                    description,
                    "" if len(pending) == 1 else "s",
                    workers,
                    "" if workers == 1 else "s",
                )
            )
            thread.start()

        def _set_job_controls(self, running):
            self.generate_button.setEnabled(not running)
            self.convert_folder_button.setEnabled(not running)
            self.lowres_folder_button.setEnabled(not running)
            self.cancel_button.setEnabled(running)

        def _cancel_job(self):
            if self._worker is not None:
                self._worker.cancel()
                self.cancel_button.setEnabled(False)
                job = {
                    "thumbnails": "thumbnail",
                    "rat": "RAT",
                    "lowres": "low-res",
                    "prepare": "library-preparation",
                }.get(self._job_kind, "texture")
                self.status.setText(
                    "Cancelling pending and active {} conversions…".format(job)
                )

        def _begin_job_progress(self, label, total):
            self._progress_label = label
            self._progress_last_time = time.monotonic()
            self._progress_last_count = 0
            self._progress_samples.clear()
            self.progress.setRange(0, total)
            self.progress.setValue(0)
            self.progress.setFormat(
                "{} 0/{} (0%) — estimating…".format(label, total)
            )

        def _job_progress(self, current, total):
            now = time.monotonic()
            delta_count = current - self._progress_last_count
            if delta_count > 0:
                elapsed = max(0.0, now - self._progress_last_time)
                per_file = elapsed / float(delta_count)
                for _index in range(delta_count):
                    self._progress_samples.append(per_file)
                self._progress_last_time = now
                self._progress_last_count = current
            self.progress.setRange(0, total)
            self.progress.setValue(current)
            percent = int(round((100.0 * current / total) if total else 0.0))
            if current >= 3 and self._progress_samples and current < total:
                average = sum(self._progress_samples) / len(self._progress_samples)
                estimate = "about {} left".format(
                    _format_duration(average * (total - current))
                )
            elif current >= total and total:
                estimate = "done"
            else:
                estimate = "estimating…"
            self.progress.setFormat(
                "{} {}/{} ({}%) — {}".format(
                    self._progress_label, current, total, percent, estimate
                )
            )

        def _thumbnail_ready(self, source, thumbnail):
            for index in range(self.grid.count()):
                item = self.grid.item(index)
                if item.data(USER_ROLE) == source:
                    item.setIcon(QtGui.QIcon(thumbnail))
                    break

        def _thumbnail_problem(self, source, message):
            self._generation_errors += 1
            concise = message.splitlines()[-1] if message else "unknown error"
            name = os.path.basename(source) if source else "thumbnail worker"
            self.status.setText("Failed {}: {}".format(name, concise))

        def _generation_finished(self, cancelled):
            completed = self.progress.value()
            total = self.progress.maximum()
            if cancelled:
                message = "Thumbnail generation cancelled ({}/{})".format(completed, total)
            elif self._generation_errors:
                message = "Thumbnail generation complete: {}/{} processed, {} failed".format(
                    completed, total, self._generation_errors
                )
            else:
                message = "Thumbnail generation complete ({}/{})".format(completed, total)
            self.status.setText(message)
            self._set_job_controls(False)
            self._worker = None
            self._job_kind = None

        def _rat_converted(self, _source, _target):
            pass

        def _rat_skipped(self, _source, _target, _reason):
            self._conversion_skipped += 1

        def _rat_problem(self, source, message):
            self._generation_errors += 1
            concise = message.splitlines()[-1] if message else "unknown error"
            name = os.path.basename(source) if source else "RAT worker"
            self.status.setText("Failed {}: {}".format(name, concise))

        def _conversion_finished(self, cancelled):
            completed = self.progress.value()
            total = self.progress.maximum()
            converted = max(0, completed - self._generation_errors - self._conversion_skipped)
            if cancelled:
                message = "RAT conversion cancelled ({}/{})".format(completed, total)
            else:
                message = "RAT conversion complete: {} converted, {} skipped, {} failed".format(
                    converted, self._conversion_skipped, self._generation_errors
                )
            self._populate_grid()
            self.status.setText(message)
            self._set_job_controls(False)
            self._worker = None
            self._job_kind = None

        def _lowres_resized(self, _source):
            pass

        def _lowres_skipped(self, _source, _target, _reason):
            self._conversion_skipped += 1

        def _lowres_problem(self, source, message):
            self._generation_errors += 1
            concise = message.splitlines()[-1] if message else "unknown error"
            name = os.path.basename(source) if source else "low-res worker"
            self.status.setText("Failed {}: {}".format(name, concise))

        def _lowres_finished(self, cancelled):
            completed = self.progress.value()
            total = self.progress.maximum()
            resized_count = max(
                0, completed - self._generation_errors - self._conversion_skipped
            )
            if cancelled:
                message = "Low-res creation cancelled ({}/{})".format(completed, total)
            else:
                message = "Low-res creation complete: {} resized, {} skipped, {} failed".format(
                    resized_count, self._conversion_skipped, self._generation_errors
                )
            self._populate_grid()
            self.status.setText(message)
            self._set_job_controls(False)
            self._worker = None
            self._job_kind = None

        def _prepare_problem(self, source, message):
            self._generation_errors += 1
            concise = message.splitlines()[-1] if message else "unknown error"
            name = os.path.basename(source) if source else "preparation worker"
            self.status.setText("Failed {}: {}".format(name, concise))

        def _prepare_finished(self, cancelled, summary):
            context = self._prepare_context or {}
            added = []
            if context.get("add_roots"):
                parent = self._root_by_path(context.get("parent_path", ""))
                if parent is not None:
                    folders = []
                    for path, suffix, rat_only in context.get("generated", ()):
                        if not os.path.isdir(path):
                            continue
                        folders.append(
                            (
                                path,
                                suffix,
                                prepare.folder_is_rat_only(path)
                                if rat_only is None
                                else rat_only,
                            )
                        )
                    added = prepare.generated_root_entries(
                        self._root_entries(), parent, folders
                    )
                    if added:
                        self._root_entries().extend(added)
                        self._save()
                        self._rebuild_root_settings()
                        self._rebuild_locations()

            if cancelled:
                message = "Library preparation cancelled ({}/{})".format(
                    summary.completed, self.progress.maximum()
                )
            else:
                message = (
                    "Library preparation complete: {} converted, {} resized, "
                    "{} thumbnails, {} skipped, {} failed"
                ).format(
                    summary.converted,
                    summary.resized,
                    summary.thumbnails,
                    summary.skipped,
                    summary.failed,
                )
            if added:
                message += "; added {} folder{}".format(
                    len(added), "" if len(added) == 1 else "s"
                )
            self._populate_grid()
            self.status.setText(message)
            self._set_job_controls(False)
            self._worker = None
            self._job_kind = None
            self._prepare_context = None

        def _thread_finished(self, thread):
            _ACTIVE_THREADS.discard(thread)
            if self._thread is thread:
                self._thread = None

        def closeEvent(self, event):
            self._save()
            if self._worker is not None:
                self._worker.cancel()
            super().closeEvent(event)


else:

    class HDRILibPanel:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "HDRI Library requires Houdini's hutil.Qt: {}".format(_QT_IMPORT_ERROR)
            )


def createInterface():
    """Houdini Python Panel entry point."""

    return HDRILibPanel()
