"""Qt panel for browsing and assigning HDRI/light textures.

Importing this module is safe in a plain Python process. Qt is only required when
``createInterface()`` (the Houdini Python Panel entry point) constructs the widget.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from . import assign, config, files, thumbs

try:
    from hutil.Qt import QtCore, QtGui, QtWidgets

    _QT_IMPORT_ERROR = None
except (ImportError, RuntimeError) as error:
    QtCore = QtGui = QtWidgets = None  # type: ignore
    _QT_IMPORT_ERROR = error


_ACTIVE_THREADS = set()


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

    USER_ROLE = _enum(QtCore.Qt, "ItemDataRole", "UserRole")
    TOOLTIP_ROLE = _enum(QtCore.Qt, "ItemDataRole", "ToolTipRole")
    ROOT_ROLE = _enum_value(USER_ROLE) + 1
    ALIGN_CENTER = _enum(QtCore.Qt, "AlignmentFlag", "AlignCenter")
    HORIZONTAL = _enum(QtCore.Qt, "Orientation", "Horizontal")
    ICON_MODE = _enum(QtWidgets.QListView, "ViewMode", "IconMode")
    ADJUST = _enum(QtWidgets.QListView, "ResizeMode", "Adjust")
    STATIC_MOVEMENT = _enum(QtWidgets.QListView, "Movement", "Static")
    EXTENDED_SELECTION = _enum(QtWidgets.QAbstractItemView, "SelectionMode", "ExtendedSelection")
    INSTANT_POPUP = _enum(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup")
    STANDARD_FILE_ICON = _enum(QtWidgets.QStyle, "StandardPixmap", "SP_FileIcon")
    ITEM_IS_EDITABLE = _enum(QtCore.Qt, "ItemFlag", "ItemIsEditable")


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

        def __init__(self, paths, size, workers):
            super().__init__()
            self._paths = list(paths)
            self._size = int(size)
            self._workers = int(workers)
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
                    cancel_event=self._cancelled,
                    on_result=self.thumbnail_ready.emit,
                    on_error=lambda source, error: self.problem.emit(source, str(error)),
                    on_progress=self.progress.emit,
                )
            except Exception as error:
                self.problem.emit("", str(error))
            self.finished.emit(self._cancelled.is_set())


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
            self._generation_errors = 0
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
            toolbar.addWidget(self.location_label)
            toolbar.addWidget(self.location_combo, 1)
            toolbar.addWidget(self.refresh_button)
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
            filter_bar.addWidget(self.search, 1)
            filter_bar.addWidget(self.include_subfolders)
            filter_bar.addWidget(self.format_button)
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
            self.splitter.addWidget(self.folder_tree)
            self.splitter.addWidget(self.grid)
            self.splitter.setStretchFactor(1, 1)
            layout.addWidget(self.splitter, 1)

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
            layout.addLayout(generation_bar)

            self.status = QtWidgets.QLabel("Add an HDRI root in Settings to begin.")
            self.status.setWordWrap(True)
            layout.addWidget(self.status)

            self.refresh_button.clicked.connect(self._refresh)
            self.search.textChanged.connect(self._search_changed)
            self.include_subfolders.toggled.connect(self._include_changed)
            self.folder_tree.currentItemChanged.connect(self._folder_changed)
            self.location_combo.currentIndexChanged.connect(self._dropdown_folder_changed)
            self.grid.itemDoubleClicked.connect(self._assign_item)
            self.generate_button.clicked.connect(self._start_generation)
            self.cancel_button.clicked.connect(self._cancel_generation)

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

            options_row = QtWidgets.QHBoxLayout()
            location_group = QtWidgets.QGroupBox("Location UI")
            location_layout = QtWidgets.QVBoxLayout(location_group)
            self.sidebar_radio = QtWidgets.QRadioButton("Sidebar")
            self.dropdown_radio = QtWidgets.QRadioButton("Dropdown")
            location_layout.addWidget(self.sidebar_radio)
            location_layout.addWidget(self.dropdown_radio)
            location_layout.addStretch(1)
            options_row.addWidget(location_group)

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
            thumbnail_layout.addRow("Preview size", self.thumbnail_size_spin)
            thumbnail_layout.addRow("Parallel workers", self.thumbnail_workers_spin)
            options_row.addWidget(thumbnail_group)
            layout.addLayout(options_row)

            self.settings_add_root.clicked.connect(self._add_root)
            self.settings_remove_root.clicked.connect(self._remove_root)
            self.settings_move_up.clicked.connect(lambda: self._move_root(-1))
            self.settings_move_down.clicked.connect(lambda: self._move_root(1))
            self.settings_color_root.clicked.connect(self._choose_root_color)
            self.settings_clear_color.clicked.connect(lambda: self._set_selected_root_color(""))
            self.roots_list.itemChanged.connect(self._root_item_changed)
            self.roots_list.currentItemChanged.connect(self._root_selection_changed)
            self.roots_list.itemDoubleClicked.connect(self._root_item_double_clicked)
            self.sidebar_radio.toggled.connect(
                lambda checked: checked and self.set_location_ui_mode("sidebar")
            )
            self.dropdown_radio.toggled.connect(
                lambda checked: checked and self.set_location_ui_mode("dropdown")
            )
            self.thumbnail_size_spin.valueChanged.connect(self._thumbnail_settings_changed)
            self.thumbnail_workers_spin.valueChanged.connect(self._thumbnail_settings_changed)

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
            self._apply_icon_size()
            self.set_location_ui_mode(
                self._settings.get("location_ui_mode", "sidebar"), save=False
            )

        def _apply_icon_size(self):
            size = int(self._settings.get("thumbnail_size", 256))
            self.grid.setIconSize(QtCore.QSize(size, max(32, size // 2)))
            self.grid.setGridSize(QtCore.QSize(size + 24, max(100, size // 2 + 48)))

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
            roots = self._root_entries()
            existing = next(
                (index for index, root in enumerate(roots) if root["path"] == selected),
                None,
            )
            if existing is None:
                roots.append(
                    {
                        "path": selected,
                        "label": "",
                        "color": "",
                        "extensions": list(config.DEFAULT_EXTENSIONS),
                    }
                )
                row = len(roots) - 1
            else:
                row = existing
            self._folder = selected
            self._folder_root = selected
            self._save()
            self._rebuild_root_settings(row)
            self._rebuild_locations()

        def _remove_root(self):
            item = self.roots_list.currentItem()
            row = self.roots_list.indexOfTopLevelItem(item) if item is not None else -1
            roots = self._root_entries()
            if not 0 <= row < len(roots):
                return
            removed = roots.pop(row)["path"]
            if self._folder == removed or self._folder.startswith(removed + os.sep):
                self._folder = roots[0]["path"] if roots else ""
                self._folder_root = roots[0]["path"] if roots else ""
            self._save()
            self._rebuild_root_settings(min(row, len(roots) - 1))
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
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
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

            if self._folder:
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
                selected_item = self.folder_tree.topLevelItem(0)
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
            self._save()
            self._apply_icon_size()
            self._populate_grid()

        def _include_changed(self, _checked=False):
            self._save()
            self._populate_grid()

        def _search_changed(self, _text):
            self._save()
            self._populate_grid()

        def _refresh(self):
            self._rebuild_locations()
            self.status.setText("Folder list refreshed.")

        def _populate_grid(self):
            self.grid.clear()
            if not self._folder or not os.path.isdir(self._folder):
                self._all_files = []
                if not self._root_entries():
                    self.status.setText("Add an HDRI root in Settings to begin.")
                return
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
            for path in visible:
                item = QtWidgets.QListWidgetItem(os.path.basename(path))
                item.setTextAlignment(ALIGN_CENTER)
                item.setToolTip(path)
                item.setData(USER_ROLE, path)
                cached = thumbs.cached_thumbnail(path, size=size)
                item.setIcon(QtGui.QIcon(cached) if cached else fallback_icon)
                self.grid.addItem(item)
            self.status.setText(
                "{} texture{}".format(len(visible), "" if len(visible) == 1 else "s")
            )

        def _assign_item(self, item):
            result = assign.assign_texture(item.data(USER_ROLE))
            self.status.setText(result.message)

        def _start_generation(self):
            if self._thread is not None:
                return
            size = int(self._settings.get("thumbnail_size", 256))
            pending = [
                path
                for path in self._all_files
                if not thumbs.cached_thumbnail(path, size=size)
            ]
            if not pending:
                self.status.setText("All thumbnails for this view are cached.")
                return

            thread = QtCore.QThread()
            worker = ThumbnailWorker(
                pending,
                size,
                int(self._settings.get("thumbnail_workers", config.DEFAULT_THUMBNAIL_WORKERS)),
            )
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.thumbnail_ready.connect(self._thumbnail_ready)
            worker.problem.connect(self._thumbnail_problem)
            worker.progress.connect(self._thumbnail_progress)
            worker.finished.connect(self._generation_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: self._thread_finished(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._generation_errors = 0
            self.progress.setRange(0, len(pending))
            self.progress.setValue(0)
            self.generate_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            workers = min(len(pending), worker._workers)
            self.status.setText(
                "Generating {} thumbnail{} with {} worker{}…".format(
                    len(pending),
                    "" if len(pending) == 1 else "s",
                    workers,
                    "" if workers == 1 else "s",
                )
            )
            thread.start()

        def _cancel_generation(self):
            if self._worker is not None:
                self._worker.cancel()
                self.cancel_button.setEnabled(False)
                self.status.setText("Cancelling pending and active thumbnail conversions…")

        def _thumbnail_progress(self, current, total):
            self.progress.setRange(0, total)
            self.progress.setValue(current)

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
            self.generate_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self._worker = None

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
