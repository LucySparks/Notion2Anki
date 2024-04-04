"""Notion Sync plugin."""

import json
import zipfile
from collections import defaultdict
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory
from traceback import format_exc
from typing import Any, Dict, List, Optional, Set, cast

from anki.collection import Collection
from aqt import mw
from aqt.gui_hooks import main_window_did_init
from aqt.utils import showCritical, showInfo
from jsonschema import ValidationError, validate
from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenu, QMessageBox

from .helpers import (
    BASE_DIR,
    enable_logging_to_file,
    get_logger,
    normalize_block_id,
    safe_path,
    safe_str,
)
from .notes_manager import NotesManager
from .notion_client import NotionClient, NotionClientError
from .parser import AnkiNote, extract_notes_data


class NotionSyncPlugin(QObject):
    """Notion sync plugin.

    Reads config, handles signals from Anki and spawns synchronization tasks
    on timer.
    """

    #: Default sync interval, min
    DEFAULT_SYNC_INTERVAL: int = 30

    def __init__(self):
        """Init plugin."""
        super().__init__()
        # While testing `mw` is None
        if not mw:
            return
        # Load config
        config = mw.addonManager.getConfig(__name__)
        mw.addonManager.setConfigUpdatedAction(__name__, self.reload_config)
        # Validate config
        self.config = self.get_valid_config(config)
        # Create a logger
        self.debug = "debug" in self.config and self.config["debug"]
        if self.debug:
            enable_logging_to_file()
        self.logger = get_logger(self.__class__.__name__, self.debug)
        self.logger.info("Config loaded: %s", self.config)
        # Anki's collection and note manager
        self.collection: Optional[Collection] = None
        self._collection_seeded = False
        # The notes managers for each deck
        self.notes_managers: Optional[Dict[str, List[NotesManager]]] = None
        # Workers scaffolding
        self.thread_pool = QThreadPool()
        # The notes ids that were synced for each deck
        self.synced_note_ids: Dict[str, Set[int]] = defaultdict(set)
        self._alive_workers: int = 0
        self._sync_errors: List[str] = []
        # Sync stats
        self._processed = self._created = self._updated = self._deleted = 0
        # The notes ids that were already in the collection for each deck
        self.existing_note_ids: Dict[str, Set[int]] = defaultdict(set)
        self._remove_obsolete_on_sync = False
        # Add action to Anki menu
        self.notion_menu: Optional[QMenu] = None
        self.add_actions()
        # Add callback to seed the collection then it's ready
        main_window_did_init.append(self.seed_collection)
        # Perform auto sync after main window initialization
        main_window_did_init.append(self.auto_sync)
        # Create and run timer
        self._is_auto_sync = True
        self.timer = QTimer()
        sync_interval_milliseconds = (
            self.config.get("sync_every_minutes", self.DEFAULT_SYNC_INTERVAL)
            * 60  # seconds
            * 1000  # milliseconds
        )
        if sync_interval_milliseconds:
            self.timer.setInterval(sync_interval_milliseconds)
            self.timer.timeout.connect(self.auto_sync)
            self.timer.start()

    def _validate_config(self, config: Optional[Dict[str, Any]]):
        """Validate config.

        :param config: config
        :raises ValidationError: if config is invalid
        """
        if not config:
            raise ValidationError("Config is empty")
        # Load schema and validate configuration
        with open(
            BASE_DIR / "schemas/config_schema.json", encoding="utf8"
        ) as s:
            schema = json.load(s)
        validate(config, schema)

    def get_valid_config(
        self, config: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Get valid configuration.

        :param config: configuration
        :returns: either configuration provided (if it's valid) or default
            config
        """
        try:
            self._validate_config(config)
        except ValidationError as exc:
            showCritical(str(exc), title="Notion loader config load error")
            assert mw  # mypy
            default_config = mw.addonManager.addonConfigDefaults(str(BASE_DIR))
            return cast(Dict[str, Any], default_config)
        else:
            assert config  # mypy
            return config

    def reload_config(self, new_config: Optional[Dict[str, Any]]) -> None:
        """Reload configuration.

        :param new_config: new configuration
        """
        if not new_config:
            assert mw  # mypy
            new_config = mw.addonManager.getConfig(__name__)
        try:
            self._validate_config(new_config)
        except ValidationError as exc:
            self.logger.error("Config update error", exc_info=exc)
            showCritical(str(exc), title="Notion loader config update error")
        else:
            assert new_config  # mypy
            self.config = new_config

    def add_actions(self):
        """Add Notion menu entry with actions to Tools menu."""
        assert mw  # mypy
        self.notion_menu = mw.form.menuTools.addMenu("NotionSync")
        load_action = QAction("Load notes", mw)
        load_action_and_remove_obsolete = QAction(
            "Load notes and remove obsolete", mw
        )
        load_action.triggered.connect(self.sync)
        load_action_and_remove_obsolete.triggered.connect(
            self.sync_and_remove_obsolete
        )
        self.notion_menu.addActions(
            (load_action, load_action_and_remove_obsolete)
        )

    def seed_collection(self):
        """Init collection and note manager after Anki loaded."""
        assert mw  # mypy
        self.collection = mw.col
        if not self.collection:
            self.logger.error("Collection is empty")
            return
        # Create notes managers
        self.logger.info("Creating notes managers...")
        self.notes_managers = {}
        for page_conf in self.get_notion_pages_config():
            page_id, target_deck, _ = page_conf

            self.logger.info(
                f"Creating notes manager for Notion page:{page_id} and deck:{target_deck}"
            )

            self.notes_managers[target_deck] = NotesManager(
                collection=self.collection,
                deck_name=target_deck,
                debug=self.debug,
            )

        self.logger.info("Collection initialized")
        self.existing_note_ids = {
            deck: nm.existing_note_ids
            for deck, nm in self.notes_managers.items()
        }
        self._collection_seeded = True

    def handle_worker_result(self, deck: str, notes: List[AnkiNote]) -> None:
        """Add notes to collection.

        :param deck: deck name
        :param notes: notes
        """
        assert self.notes_managers and deck in self.notes_managers  # mypy
        try:
            for note in notes:
                if not note.front:
                    self.logger.warning(
                        "Note front is empty. Back: %s", safe_str(note.back)
                    )
                    continue
                self._processed += 1
                # Find out if note already exists
                note_id = self.notes_managers[deck].find_note(note)
                if note_id:
                    is_updated = self.notes_managers[deck].update_note(
                        note_id, note
                    )
                    if is_updated:
                        self._updated += 1
                # Create new note
                else:
                    note_id = self.notes_managers[deck].create_note(note)
                    self._created += 1
                self.synced_note_ids[deck].add(note_id)
        except Exception:
            error_msg = format_exc()
            self._sync_errors.append(error_msg)

    def handle_sync_finished(self, deck: str) -> None:
        """Handle sync finished.

        In case of any error - show error message in manual mode and do nothing
        otherwise.  If no error - save the collection and show sync statistics
        in manual mode.  If `self._remove_obsolete_on_sync` is True - remove
        all notes that is not added or updated in current sync.

        :param deck: deck name that finished sync (Only this deck is finished)
        """
        assert self.notes_managers  # mypy
        assert self.collection  # mypy
        self.logger.info(f"Worker finished: {deck}")
        self._alive_workers -= 1
        # If all workers finished, execute following code in this function, otherwise wait
        if self._alive_workers:
            return
        assert self.notion_menu  # mypy
        self.notion_menu.setTitle("NotionSync")
        # Show errors if manual sync
        if self._sync_errors:
            if not self._is_auto_sync:
                error_msg = "\n".join(self._sync_errors)
                showCritical(error_msg, title="Loading from Notion failed")
        # If no errors - save collection and refresh Anki window
        else:
            if self._remove_obsolete_on_sync:
                # Get the note id that should be removed per deck
                ids_to_remove = defaultdict(set)
                temp_total_removed = 0
                for deck in self.existing_note_ids.keys():
                    temp_ids = (
                        self.existing_note_ids[deck]
                        - self.synced_note_ids[deck]
                    )
                    if len(temp_ids) > 0:
                        ids_to_remove[deck] = temp_ids
                        self.logger.info(
                            f"Will delete {len(temp_ids)} note(s) in {deck}"
                        )
                        temp_total_removed += len(ids_to_remove)
                if ids_to_remove:
                    msg = (
                        f"Will delete {temp_total_removed} obsolete note(s), "
                        f"continue?"
                    )
                    assert mw  # mypy
                    do_delete = QMessageBox.question(
                        mw,
                        "Confirm deletion",
                        msg,
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No,
                    )
                    if do_delete == QMessageBox.StandardButton.Yes.value:
                        for deck, notes_id in ids_to_remove.items():
                            self.notes_managers[deck].remove_notes(notes_id)
                        self._deleted += len(notes_id)

            self.collection.save(trx=False)
            mw.maybeReset()  # type: ignore[union-attr]
            mw.deckBrowser.refresh()  # type: ignore[union-attr]
            stats = (
                f"Processed: {self._processed}\n"
                f"Created: {self._created}\n"
                f"Updated: {self._updated}\n"
                f"Deleted: {self._deleted}"
            )
            if not self._is_auto_sync:
                showInfo(
                    f"Successfully loaded:\n{stats}",
                    title="Loading from Notion",
                )
        self.logger.info(
            "Sync finished, processed=%s, created=%s, updated=%s, deleted=%s",
            self._processed,
            self._created,
            self._updated,
            self._deleted,
        )
        self._reset_stats()

    def handle_worker_error(self, error_message) -> None:
        """Handle worker error.

        :param error_message: error message
        """
        self._sync_errors.append(error_message)

    def auto_sync(self) -> None:
        """Perform synchronization in background."""
        self.logger.info("Auto sync started")
        # Reload config
        assert mw  # mypy
        self.reload_config(None)
        self._is_auto_sync = True
        self._sync()

    def sync(self) -> None:
        """Perform synchronization and report result."""
        self.logger.info("Sync started")
        # Reload config
        assert mw  # mypy
        self.reload_config(None)
        if not self._alive_workers:
            self._is_auto_sync = False
            self._sync()
        else:
            showInfo(
                "Sync is already in progress, please wait",
                title="Load from Notion",
            )

    def sync_and_remove_obsolete(self) -> None:
        """Perform synchronization and remove obsolete notes."""
        self.logger.info("Sync with remove obsolete started")
        self._remove_obsolete_on_sync = True
        self.sync()

    def _reset_stats(self) -> None:
        """Reset variables before sync.

        Saves pre-sync existing note ids and resets sync stats and errors.
        """
        self._remove_obsolete_on_sync = False
        self.synced_note_ids = defaultdict(set)
        assert self.notes_managers  # mypy
        self.existing_note_ids = {
            deck: nm.existing_note_ids
            for deck, nm in self.notes_managers.items()
        }
        self._processed = self._created = self._updated = self._deleted = 0
        self._sync_errors = []

    def _sync(self) -> None:
        """Start sync."""
        if not self._collection_seeded:
            self.logger.warning(
                "Collection is not seeded yet, trying to seed now"
            )
            self.seed_collection()
            if not self._collection_seeded:
                return
        assert self.notion_menu  # mypy
        self.notion_menu.setTitle("Notion (syncing...)")
        for page_conf in self.get_notion_pages_config():
            page_id, target_deck, recursive = page_conf
            worker = NotesExtractorWorker(
                notion_token=self.config["notion_token"],
                page_id=page_id,
                recursive=recursive,
                target_deck=target_deck,
                notion_namespace=self.config.get("notion_namespace", ""),
                debug=self.debug,
            )
            worker.signals.result.connect(self.handle_worker_result)
            worker.signals.error.connect(self.handle_worker_error)
            worker.signals.finished.connect(self.handle_sync_finished)
            # Start worker
            self.thread_pool.start(worker)
            self._alive_workers += 1

    def get_notion_pages_config(self) -> List[List[str]]:
        """Get Notion pages configuration. For page_spec without a specified target_deck value, the target_deck will default to using the page_id.

        :returns: Notion pages configuration, including page_id, target_deck and recursive flag
        """
        pages_conf = []
        for page_spec in self.config.get("notion_pages", []):
            ori_page_id = page_spec["page_id"]
            page_id = normalize_block_id(ori_page_id)
            target_deck = page_spec.get("target_deck", None)
            recursive = page_spec.get("recursive", False)
            if target_deck == "" or target_deck is None:
                target_deck = ori_page_id

            pages_conf.append([page_id, target_deck, recursive])

        return pages_conf


class NoteExtractorSignals(QObject):
    """The signals available from a running extractor thread."""

    #: Extraction finished
    finished = pyqtSignal(str)
    #: Notes data
    result = pyqtSignal(str, object)
    #: Error
    error = pyqtSignal(str)


class NotesExtractorWorker(QRunnable):
    """Notes extractor worker thread."""

    def __init__(
        self,
        notion_token: str,
        page_id: str,
        recursive: bool,
        target_deck: str,
        notion_namespace: str,
        debug: bool = False,
    ):
        """Init notes extractor.

        :param notion_token: Notion token
        :param page_id: Notion page id
        :param recursive: recursive export
        :param notion_namespace: Notion namespace to form source links
        :param debug: debug log level
        """
        super().__init__()
        self.debug = debug
        self.logger = get_logger(f"worker_{page_id}", self.debug)
        self.signals = NoteExtractorSignals()
        self.notion_token = notion_token
        self.page_id = page_id
        self.recursive = recursive
        self.target_deck = target_deck
        self.notion_namespace = notion_namespace

    def run(self) -> None:
        """Extract note data from given Notion page.

        Export Notion page as HTML, extract notes data from the HTML and send
        results.
        """
        self.logger.info("Worker started")
        self.logger.info(
            f"Current page id: {self.page_id}. Current deck: {self.target_deck}"
        )
        try:
            with TemporaryDirectory() as tmp_dir:
                # Export given Notion page as HTML
                tmp_path = safe_path(Path(tmp_dir))
                export_path = tmp_path / f"{self.page_id}.zip"
                client = NotionClient(self.notion_token, self.debug)
                client.export_page(
                    page_id=self.page_id,
                    destination=export_path,
                    recursive=self.recursive,
                )
                self.logger.info(
                    "Exported file downloaded: path=%s", str(export_path)
                )
                # Extract notes data from the HTML files
                with zipfile.ZipFile(export_path) as zip_file:
                    zip_file.extractall(tmp_path)
                notes = []
                for html_path in tmp_path.rglob("*.html"):
                    notes += extract_notes_data(
                        source=Path(html_path),
                        notion_namespace=self.notion_namespace,
                        debug=self.debug,
                    )
                self.logger.info(
                    f"Notes extracted: deck:{self.target_deck}, count:{len(notes)}"
                )
        except NotionClientError as exc:
            self.logger.error("Error extracting notes", exc_info=exc)
            error_msg = f"Cannot export {self.page_id}:\n{exc}"
            self.signals.error.emit(error_msg)
        except OSError as exc:  # Long path
            self.logger.warning("Error deleting files", exc_info=exc)
            # Delete manually
            rmtree(tmp_path, ignore_errors=True)
        else:
            self.signals.result.emit(self.target_deck, notes)
        finally:
            self.signals.finished.emit(self.target_deck)


NotionSyncPlugin()
