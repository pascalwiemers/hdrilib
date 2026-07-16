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

    USER_ROLE = _enum(QtCore.Qt, "ItemDataRole", "UserRole")
    ALIGN_CENTER = _enum(QtCore.Qt, "AlignmentFlag", "AlignCenter")
    HORIZONTAL = _enum(QtCore.Qt, "Orientation", "Horizontal")
    ICON_MODE = _enum(QtWidgets.QListView, "ViewMode", "IconMode")
    ADJUST = _enum(QtWidgets.QListView, "ResizeMode", "Adjust")
    STATIC_MOVEMENT = _enum(QtWidgets.QListView, "Movement", "Static")
    EXTENDED_SELECTION = _enum(QtWidgets.QAbstractItemView, "SelectionMode", "ExtendedSelection")
    INSTANT_POPUP = _enum(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup")
    STANDARD_FILE_ICON = _enum(QtWidgets.QStyle, "StandardPixmap", "SP_FileIcon")


    class ThumbnailWorker(QtCore.QObject):
        thumbnail_ready = Signal(str, str)
        progress = Signal(int, int)
        problem = Signal(str, str)
        finished = Signal(bool)

        def __init__(self, paths, size):
            super().__init__()
            self._paths = list(paths)
            self._size = int(size)
            self._cancelled = threading.Event()

        def cancel(self):
            self._cancelled.set()

        @Slot()
        def run(self):
            total = len(self._paths)
            for index, source in enumerate(self._paths, 1):
                if self._cancelled.is_set():
                    break
                try:
                    result = thumbs.generate_thumbnail(source, size=self._size)
                    self.thumbnail_ready.emit(source, result)
                except Exception as error:
                    self.problem.emit(source, str(error))
                self.progress.emit(index, total)
            self.finished.emit(self._cancelled.is_set())


    class HDRILibPanel(QtWidgets.QWidget):
        """Main HDRI Library widget."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._settings = config.load_config()
            self._folder = self._settings.get("last_folder", "")
            self._all_files = []
            self._worker = None
            self._thread = None
            self._generation_errors = 0
            self._format_actions = {}
            self._build_ui()
            self._restore_ui()
            self._rebuild_tree()

        def _build_ui(self):
            self.setObjectName("hdrilibPanel")
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)

            root_bar = QtWidgets.QHBoxLayout()
            self.add_root_button = QtWidgets.QPushButton("Add Folder…")
            self.remove_root_button = QtWidgets.QPushButton("Remove Root")
            self.refresh_button = QtWidgets.QPushButton("Refresh")
            root_bar.addWidget(self.add_root_button)
            root_bar.addWidget(self.remove_root_button)
            root_bar.addWidget(self.refresh_button)
            root_bar.addStretch(1)
            outer.addLayout(root_bar)

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
            outer.addLayout(filter_bar)

            splitter = QtWidgets.QSplitter(HORIZONTAL)
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
            splitter.addWidget(self.folder_tree)
            splitter.addWidget(self.grid)
            splitter.setStretchFactor(1, 1)
            outer.addWidget(splitter, 1)

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

            self.status = QtWidgets.QLabel("Add an HDRI folder to begin.")
            self.status.setWordWrap(True)
            outer.addWidget(self.status)

            self.add_root_button.clicked.connect(self._add_root)
            self.remove_root_button.clicked.connect(self._remove_root)
            self.refresh_button.clicked.connect(self._refresh)
            self.search.textChanged.connect(self._search_changed)
            self.include_subfolders.toggled.connect(self._include_changed)
            self.folder_tree.currentItemChanged.connect(self._folder_changed)
            self.grid.itemDoubleClicked.connect(self._assign_item)
            self.generate_button.clicked.connect(self._start_generation)
            self.cancel_button.clicked.connect(self._cancel_generation)

        def _restore_ui(self):
            self.search.blockSignals(True)
            self.search.setText(self._settings.get("search_text", ""))
            self.search.blockSignals(False)
            self.include_subfolders.blockSignals(True)
            self.include_subfolders.setChecked(bool(self._settings.get("include_subfolders")))
            self.include_subfolders.blockSignals(False)
            enabled = set(self._settings.get("enabled_extensions", ()))
            for extension, action in self._format_actions.items():
                action.blockSignals(True)
                action.setChecked(extension in enabled)
                action.blockSignals(False)
            self._apply_icon_size()

        def _apply_icon_size(self):
            size = int(self._settings.get("thumbnail_size", 256))
            self.grid.setIconSize(QtCore.QSize(size, max(32, size // 2)))
            self.grid.setGridSize(QtCore.QSize(size + 24, max(100, size // 2 + 48)))

        def _save(self):
            self._settings["last_folder"] = self._folder or ""
            self._settings["search_text"] = self.search.text()
            self._settings["include_subfolders"] = self.include_subfolders.isChecked()
            self._settings["enabled_extensions"] = [
                extension for extension, action in self._format_actions.items() if action.isChecked()
            ]
            try:
                self._settings = config.save_config(self._settings)
            except OSError as error:
                self.status.setText("Could not save settings: {}".format(error))

        def _add_root(self):
            start = self._folder or str(Path.home())
            selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Add HDRI folder", start)
            if not selected:
                return
            selected = os.path.abspath(selected)
            roots = list(self._settings.get("roots", ()))
            if selected not in roots:
                roots.append(selected)
                self._settings["roots"] = roots
            self._folder = selected
            self._save()
            self._rebuild_tree()

        def _remove_root(self):
            roots = list(self._settings.get("roots", ()))
            selected = self._folder
            root = next(
                (candidate for candidate in roots if selected == candidate or selected.startswith(candidate + os.sep)),
                None,
            )
            if root is None:
                self.status.setText("Select a folder belonging to a configured root.")
                return
            roots.remove(root)
            self._settings["roots"] = roots
            self._folder = roots[0] if roots else ""
            self._save()
            self._rebuild_tree()

        def _add_tree_directory(self, path, parent):
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
            for directory in directories:
                item = QtWidgets.QTreeWidgetItem(parent, [directory.name])
                item.setData(0, USER_ROLE, directory.path)
                self._add_tree_directory(directory.path, item)

        def _rebuild_tree(self):
            self.folder_tree.blockSignals(True)
            self.folder_tree.clear()
            selected_item = None
            for root in self._settings.get("roots", ()):
                if not os.path.isdir(root):
                    continue
                item = QtWidgets.QTreeWidgetItem(self.folder_tree, [os.path.basename(root) or root])
                item.setToolTip(0, root)
                item.setData(0, USER_ROLE, root)
                self._add_tree_directory(root, item)
                if self._folder == root:
                    selected_item = item
            if self._folder and selected_item is None:
                iterator = QtWidgets.QTreeWidgetItemIterator(self.folder_tree)
                while iterator.value():
                    item = iterator.value()
                    if item.data(0, USER_ROLE) == self._folder:
                        selected_item = item
                        break
                    iterator += 1
            if selected_item is None and self.folder_tree.topLevelItemCount():
                selected_item = self.folder_tree.topLevelItem(0)
                self._folder = selected_item.data(0, USER_ROLE)
            self.folder_tree.blockSignals(False)
            if selected_item is not None:
                self.folder_tree.setCurrentItem(selected_item)
                self.folder_tree.scrollToItem(selected_item)
            self._populate_grid()

        def _folder_changed(self, current, _previous):
            if current is None:
                return
            self._folder = current.data(0, USER_ROLE)
            self._save()
            self._populate_grid()

        def _enabled_extensions(self):
            return [extension for extension, action in self._format_actions.items() if action.isChecked()]

        def _formats_changed(self, _checked=False):
            self._save()
            self._populate_grid()

        def _include_changed(self, _checked=False):
            self._save()
            self._populate_grid()

        def _search_changed(self, _text):
            self._save()
            self._populate_grid()

        def _refresh(self):
            self._rebuild_tree()
            self.status.setText("Folder list refreshed.")

        def _populate_grid(self):
            self.grid.clear()
            if not self._folder or not os.path.isdir(self._folder):
                self._all_files = []
                return
            extensions = self._enabled_extensions()
            self._all_files = files.scan_files(
                self._folder,
                extensions=extensions,
                recursive=self.include_subfolders.isChecked(),
            ) if extensions else []
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
            self.status.setText("{} texture{}".format(len(visible), "" if len(visible) == 1 else "s"))

        def _assign_item(self, item):
            result = assign.assign_texture(item.data(USER_ROLE))
            self.status.setText(result.message)

        def _start_generation(self):
            if self._thread is not None:
                return
            size = int(self._settings.get("thumbnail_size", 256))
            pending = [path for path in self._all_files if not thumbs.cached_thumbnail(path, size=size)]
            if not pending:
                self.status.setText("All thumbnails for this view are cached.")
                return

            thread = QtCore.QThread()
            worker = ThumbnailWorker(pending, size)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.thumbnail_ready.connect(self._thumbnail_ready)
            worker.problem.connect(self._thumbnail_problem)
            worker.progress.connect(self._thumbnail_progress)
            worker.finished.connect(self._generation_finished)
            worker.finished.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda thread=thread: _ACTIVE_THREADS.discard(thread))
            _ACTIVE_THREADS.add(thread)
            self._thread = thread
            self._worker = worker
            self._generation_errors = 0
            self.progress.setRange(0, len(pending))
            self.progress.setValue(0)
            self.generate_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.status.setText("Generating {} thumbnail{}…".format(len(pending), "" if len(pending) == 1 else "s"))
            thread.start()

        def _cancel_generation(self):
            if self._worker is not None:
                self._worker.cancel()
                self.cancel_button.setEnabled(False)
                self.status.setText("Cancelling after the current thumbnail…")

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
            self.status.setText("Failed {}: {}".format(os.path.basename(source), concise))

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
            self._thread = None
            self._worker = None

        def closeEvent(self, event):
            self._save()
            if self._worker is not None:
                self._worker.cancel()
            super().closeEvent(event)


else:

    class HDRILibPanel:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise RuntimeError("HDRI Library requires Houdini's hutil.Qt: {}".format(_QT_IMPORT_ERROR))


def createInterface():
    """Houdini Python Panel entry point."""

    return HDRILibPanel()
