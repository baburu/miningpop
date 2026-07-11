# meikipop/gui/popup.py
import json
import logging
import threading
from typing import List, Optional

from PyQt6.QtCore import QTimer, QPoint, QSize, QEvent, pyqtSignal
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QFontInfo
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QApplication, QPushButton, QSizePolicy
)

from meikipop.anki.ankiconnect import AnkiConnectClient, AnkiConnectError, MineableWord, render_field_mapping
from meikipop.config.config import config, IS_MACOS
from meikipop.dictionary.lookup import DictionaryEntry, KanjiEntry
from meikipop.gui.magpie_manager import magpie_manager

# macOS-specific imports for focus management
if IS_MACOS:
    try:
        import Quartz
    except ImportError:
        Quartz = None

logger = logging.getLogger(__name__)

MINE_BUTTON_SIZE = 20
MINE_BUTTON_GAP = 6  # horizontal space reserved for the button next to the header text


class Popup(QWidget):
    # Marshals AnkiConnect results (which happen on a background thread) back to the GUI thread.
    mine_finished = pyqtSignal(object, str, str)  # (button, status, message)

    def __init__(self, shared_state, input_loop):
        super().__init__()
        self._latest_data = None
        self._last_latest_data = None
        self._data_lock = threading.Lock()
        self._previous_active_window_on_mac = None

        self.shared_state = shared_state
        self.input_loop = input_loop

        self.is_visible = False
        self.is_pinned = False  # True while the mouse is hovering the popup itself
        self._entry_widgets = []  # currently-built per-entry row widgets, kept so we can tear them down

        # --- TOGGLE LOGIC STATES ---
        self.toggle_active = False
        self._hotkey_was_active_last_tick = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.process_latest_data_loop)
        self.timer.start(10)

        self.mine_finished.connect(self._apply_mine_feedback)

        self.probe_label = QLabel()
        self.probe_label.setWordWrap(True)
        self.probe_label.setTextFormat(Qt.TextFormat.RichText)

        self.is_calibrated = False
        self.header_chars_per_line = 50
        self.def_chars_per_line = 50

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent;")

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.frame = QFrame()
        self._apply_frame_stylesheet()
        main_layout.addWidget(self.frame)
        # Track mouse enter/leave on the frame to implement "pin while hovered".
        self.frame.installEventFilter(self)

        self.content_layout = QVBoxLayout(self.frame)
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_layout.setSpacing(4)

        self.hide()

    # ------------------------------------------------------------------ #
    # Pin-on-hover: while the cursor is over the popup itself, stop it
    # from chasing the mouse / auto-hiding so the user can click "+".
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):
        if obj is self.frame:
            if event.type() == QEvent.Type.Enter:
                self.is_pinned = True
            elif event.type() == QEvent.Type.Leave:
                self.is_pinned = False
        return super().eventFilter(obj, event)

    def _apply_frame_stylesheet(self):
        bg_color = QColor(config.color_background)
        r, g, b = bg_color.red(), bg_color.green(), bg_color.blue()
        a = config.background_opacity
        self.probe_label.setFont(QFont(config.font_family))
        accent = config.color_highlight_word
        self.frame.setStyleSheet(f"""
            QFrame {{
                background-color: rgba({r}, {g}, {b}, {a});
                color: {config.color_foreground};
                border-radius: 8px;
                border: 1px solid #555;
            }}
            QLabel {{
                background-color: transparent;
                border: none;
                font-family: "{config.font_family}";
            }}
            hr {{
                border: none;
                height: 1px;
            }}
            QPushButton.mineButton {{
                background-color: transparent;
                color: {accent};
                border: 1px solid {accent};
                border-radius: {MINE_BUTTON_SIZE // 2}px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton.mineButton:hover {{
                background-color: {accent};
                color: {config.color_background};
            }}
            QPushButton.mineButton:disabled {{
                border: 1px solid #888;
                color: #888;
            }}
        """)

    def _calibrate_empirically(self):
        logger.debug("--- Calibrating Font Metrics Empirically (One-Time) ---")

        actual_font = self.probe_label.font()
        font_info = QFontInfo(actual_font)
        logger.debug(f"[FONT DEBUG] Requested font family: '{config.font_family}' (or default)")
        logger.debug(f"[FONT DEBUG]   -> Actual resolved font family: '{font_info.family()}'")
        logger.debug(f"[FONT DEBUG]   -> Actual style name: '{font_info.styleName()}'")
        logger.debug(f"[FONT DEBUG]   -> Actual point size: {font_info.pointSize()}")
        logger.debug(f"[FONT DEBUG]   -> Actual pixel size: {font_info.pixelSize()}")
        logger.debug(f"[FONT DEBUG]   -> Is it bold? {font_info.bold()}")

        margins = self.content_layout.contentsMargins()
        border_width = 1
        horizontal_padding = margins.left() + margins.right() + (border_width * 2)

        screen = QApplication.primaryScreen()
        self.max_content_width = (int(screen.geometry().width() * 0.4)) - horizontal_padding

        header_font = QFont(config.font_family)
        header_font.setPixelSize(config.font_size_header)
        header_metrics = QFontMetrics(header_font)
        # Reserve room for the mine button so header text doesn't get squeezed against it.
        self.header_chars_per_line = self._find_chars_for_width(
            header_metrics, "Header", self.max_content_width - MINE_BUTTON_SIZE - MINE_BUTTON_GAP
        )

        def_font = QFont(config.font_family)
        def_font.setPixelSize(config.font_size_definitions)
        def_metrics = QFontMetrics(def_font)
        self.def_chars_per_line = self._find_chars_for_width(def_metrics, "Definition", self.max_content_width)

        logger.debug(f"[CALIBRATE] Max content width: {self.max_content_width}px")
        logger.debug(f"[CALIBRATE] Empirically found {self.header_chars_per_line} header chars/line")
        logger.debug(f"[CALIBRATE] Empirically found {self.def_chars_per_line} definition chars/line")
        self.is_calibrated = True

    def _find_chars_for_width(self, metrics: QFontMetrics, name: str, width_budget: float) -> int:
        low = 1
        high = 500
        best_fit = 1
        width_budget = max(width_budget, 1)

        while low <= high:
            mid = (low + high) // 2
            if mid == 0: break

            test_string = 'x' * mid
            current_width = metrics.horizontalAdvance(test_string)

            if current_width <= width_budget:
                best_fit = mid
                low = mid + 1
            else:
                high = mid - 1

        return best_fit if best_fit > 0 else 50

    def set_latest_data(self, data):
        with self._data_lock:
            # If the user is currently hovering the popup, ignore updates
            if self.is_pinned:
                return
                
            # If the popup is locked on via toggle:
            if self.toggle_active:
                # If we already have a searched word locked in, ignore any new updates
                # (preventing other words from overwriting it as the mouse travels)
                if self._latest_data:
                    return
            
            self._latest_data = data

    def get_latest_data(self):
        with self._data_lock:
            return self._latest_data

    def process_latest_data_loop(self):
        if not self.is_calibrated:
            self._calibrate_empirically()

        latest_data = self.get_latest_data()
        if latest_data and latest_data != self._last_latest_data:
            new_size = self._build_entries(latest_data)
            self.setFixedSize(new_size)
        self._last_latest_data = latest_data

        data_present = bool(self._latest_data)
        hotkey_active = self.input_loop.is_virtual_hotkey_down()

        # --- TOGGLE FUNCTIONALITY ---
        # Detect the moment the hotkey is first tapped (rising edge)
        if hotkey_active and not self._hotkey_was_active_last_tick:
            if self.is_visible:
                # If popup is visible, press hotkey to toggle it OFF
                self.toggle_active = False
                self.hide_popup()
            else:
                # If popup is hidden, press hotkey to toggle it ON and lock position
                self.toggle_active = True
                mouse_pos = QCursor.pos()
                self.move_to(mouse_pos.x(), mouse_pos.y())
                
        self._hotkey_was_active_last_tick = hotkey_active

        # Keep popup visible if pinned, hotkey active, or locked on via toggle
        should_show = data_present and config.is_enabled and (
            self.is_pinned or hotkey_active or self.toggle_active
        )

        if should_show:
            self.show_popup()
        else:
            self.hide_popup()

        # Follow cursor only if we are not hovering over the popup and we are NOT locked on via toggle.
        # This allows you to safely move your cursor onto the stationary frozen popup!
        if not self.is_pinned and not self.toggle_active:
            if hotkey_active:
                mouse_pos = QCursor.pos()
                self.move_to(mouse_pos.x(), mouse_pos.y())

    # ------------------------------------------------------------------ #
    # Row building
    # ------------------------------------------------------------------ #
    def _clear_entry_widgets(self):
        for w in self._entry_widgets:
            self.content_layout.removeWidget(w)
            w.deleteLater()
        self._entry_widgets = []

    def _make_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: rgba(255,255,255,40); max-height: 1px; border: none;")
        return line

    def _build_entries(self, entries: List) -> QSize:
        self._clear_entry_widgets()
        max_ratio = 0.0
        calc_parts = []  # (kind, text_for_ratio) just to compute overall width like before

        for i, entry in enumerate(entries):
            if i > 0:
                sep = self._make_separator()
                self.content_layout.addWidget(sep)
                self._entry_widgets.append(sep)

            if isinstance(entry, KanjiEntry):
                row, ratio = self._build_kanji_row(entry)
            else:
                row, ratio = self._build_word_row(entry)

            max_ratio = max(max_ratio, ratio)
            self.content_layout.addWidget(row)
            self._entry_widgets.append(row)

        optimal_content_width = self.max_content_width * min(1.0, max_ratio)
        optimal_content_width = max(optimal_content_width, 220)

        for w in self._entry_widgets:
            w.setFixedWidth(int(optimal_content_width))

        self.frame.adjustSize()
        self.content_layout.activate()
        final_height = self.frame.sizeHint().height()

        margins = self.content_layout.contentsMargins()
        border_width = 1
        horizontal_padding = margins.left() + margins.right() + (border_width * 2)

        final_size = QSize(int(optimal_content_width) + horizontal_padding, final_height)
        return final_size

    def _build_kanji_row(self, entry: KanjiEntry):
        c_word = config.color_highlight_word
        c_read = config.color_highlight_reading
        c_text = config.color_foreground
        fs_head = config.font_size_header
        fs_def = config.font_size_definitions

        readings_str = ", ".join(entry.readings)
        header_calc = f"{entry.character} {readings_str}"
        ratio = max(len(header_calc) / self.header_chars_per_line, 0.7)

        header_html = (
            f'<span style="font-size:{fs_head}px; color:{c_word}; padding-right: 8px;">{entry.character}</span>'
            f'<span style="font-size:{fs_head - 2}px; color:{c_read};">[{readings_str}]</span>'
        )

        meanings_str = ", ".join(entry.meanings)
        body_html = f'<span style="font-size:{fs_def}px; color:{c_text};">{meanings_str}</span>'

        if config.show_examples and entry.examples:
            ex_parts = []
            for ex in entry.examples:
                ex_parts.append(
                    f"<span style='font-size:{fs_head - 2}px; color:{c_word}'>{ex['w']}</span> "
                    f"<span style='font-size:{fs_def}px; color:{c_read}'>[{ex['r']}]</span> "
                    f"<span style='font-size:{fs_def}px; color:{c_text}'>{ex['m']}</span>"
                )
            body_html += f'<div>{"; ".join(ex_parts)}</div>'

        if config.show_components and entry.components:
            comp_parts = [
                f"<span style='font-size:{fs_def}px; color:{c_word}'>{c.get('c', '')}</span> "
                f"<span style='font-size:{fs_def}px; color:{c_text}'>{c.get('m', '')}</span>"
                for c in entry.components
            ]
            body_html += f'<div>{", ".join(comp_parts)}</div>'

        row = self._assemble_row(header_html, body_html, mine_button=None)
        return row, ratio

    def _build_word_row(self, entry: DictionaryEntry):
        c_word = config.color_highlight_word
        c_read = config.color_highlight_reading
        c_text = config.color_foreground

        header_calc = entry.written_form
        if entry.reading:
            header_calc += f" [{entry.reading}]"
        ratio = len(header_calc) / self.header_chars_per_line

        header_html = f'<span style="color: {c_word}; font-size:{config.font_size_header}px;">{entry.written_form}</span>'
        if entry.reading:
            header_html += f' <span style="color: {c_read}; font-size:{config.font_size_header - 2}px;">[{entry.reading}]</span>'
        if entry.deconjugation_process and config.show_deconjugation:
            deconj_str = " ← ".join(p for p in entry.deconjugation_process if p)
            if deconj_str:
                header_html += f' <span style="color:{c_text}; font-size:{config.font_size_definitions - 2}px; opacity:0.8;">({deconj_str})</span>'
        if config.show_frequency and entry.freq < 999_999:
            header_html += f' <span style="color:{c_text}; font-size:{config.font_size_definitions - 2}px; opacity:0.6;">#{entry.freq}</span>'

        def_text_parts_calc = []
        def_text_parts_html = []
        for idx, sense in enumerate(entry.senses):
            glosses = sense.get('glosses', [])
            glosses_str = ", ".join(glosses) if (glosses and config.show_all_glosses) else (glosses[0] if glosses else "")
            pos_list = sense.get('pos', [])
            tags_list = sense.get('tags', [])
            sense_calc = f"({idx + 1})" if config.show_all_glosses else ""
            sense_html = f"<b>({idx + 1})</b> " if config.show_all_glosses else ""
            if config.show_pos and pos_list:
                pos_str = f' ({", ".join(pos_list)})'
                sense_calc += pos_str
                sense_html += f'<span style="color:{c_text}; opacity:0.7;"><i>{pos_str}</i></span> '
            if config.show_tags and tags_list:
                tags_str = f' [{", ".join(tags_list)}]'
                sense_calc += tags_str
                sense_html += f'<span style="color:{c_text}; font-size:{config.font_size_definitions - 2}px; opacity:0.7;">{tags_str}</span> '
            sense_calc += glosses_str
            sense_html += glosses_str
            def_text_parts_calc.append(sense_calc)
            def_text_parts_html.append(sense_html)

        if config.compact_mode:
            separator = "; "
            full_def_text_html = separator.join(def_text_parts_html)
            def_ratio = len(separator.join(def_text_parts_calc)) / self.def_chars_per_line
            ratio = max(ratio, def_ratio)
        else:
            separator = "<br>"
            full_def_text_html = separator.join(def_text_parts_html)
            for def_text_calc in def_text_parts_calc:
                ratio = max(ratio, len(def_text_calc) / self.def_chars_per_line)

        body_html = f'<span style="font-size:{config.font_size_definitions}px;">{full_def_text_html}</span>'

        mine_button = self._make_mine_button(entry)
        row = self._assemble_row(header_html, body_html, mine_button=mine_button)
        return row, ratio

    def _assemble_row(self, header_html: str, body_html: str, mine_button: Optional[QPushButton]) -> QWidget:
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(MINE_BUTTON_GAP)

        header_label = QLabel(header_html)
        header_label.setTextFormat(Qt.TextFormat.RichText)
        header_label.setWordWrap(False)
        header_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top.addWidget(header_label, stretch=1)

        if mine_button is not None:
            top.addWidget(mine_button, alignment=Qt.AlignmentFlag.AlignTop)

        outer.addLayout(top)

        body_label = QLabel(body_html)
        body_label.setTextFormat(Qt.TextFormat.RichText)
        body_label.setWordWrap(True)
        outer.addWidget(body_label)

        return row

    # ------------------------------------------------------------------ #
    # Mining
    # ------------------------------------------------------------------ #
    def _make_mine_button(self, entry: DictionaryEntry) -> QPushButton:
        button = QPushButton("+")
        button.setProperty("class", "mineButton")
        button.setObjectName("mineButton")  # for the stylesheet selector below
        button.setFixedSize(MINE_BUTTON_SIZE, MINE_BUTTON_SIZE)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        
        # Prevent button click focus shifts from generating focusOut dismissal triggers
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        button.setStyleSheet(self._mine_button_stylesheet())
        button.setToolTip("Mine into Anki")
        button.clicked.connect(lambda _checked=False, e=entry, b=button: self._mine_entry(e, b))
        return button

    def _mine_button_stylesheet(self) -> str:
        accent = config.color_highlight_word
        bg = config.color_background
        return f"""
            QPushButton#mineButton {{
                background-color: transparent;
                color: {accent};
                border: 1px solid {accent};
                border-radius: {MINE_BUTTON_SIZE // 2}px;
                font-weight: bold;
            }}
            QPushButton#mineButton:hover {{
                background-color: {accent};
                color: {bg};
            }}
            QPushButton#mineButton:disabled {{
                border: 1px solid #888;
                color: #888;
                background-color: transparent;
            }}
        """

    def _mine_entry(self, entry: DictionaryEntry, button: QPushButton):
        if not config.anki_enabled:
            self.mine_finished.emit(button, "err", "Anki mining is disabled — enable it in Settings → Anki")
            return

        try:
            mapping = json.loads(config.anki_field_mapping or '{}')
        except json.JSONDecodeError:
            mapping = {}

        if not config.anki_deck or not config.anki_model or not mapping:
            self.mine_finished.emit(button, "err", "Set a deck, note type and field mapping in Settings → Anki first")
            return

        glosses = [s['glosses'][0] for s in entry.senses if s.get('glosses')]
        glossary = glosses[0] if glosses else ""
        glossary_full = "; ".join(glosses)

        surface_len = max(getattr(entry, "surface_length", 0), len(entry.written_form), 1)
        idx = entry.sentence_index
        surface = entry.sentence[idx: idx + surface_len] if entry.sentence else entry.written_form
        sentence_cloze = (
            entry.sentence[:idx] + f"<b>{surface}</b>" + entry.sentence[idx + len(surface):]
            if entry.sentence else surface
        )

        pos_all = sorted({p for s in entry.senses for p in s.get('pos', [])})

        word = MineableWord(
            expression=entry.written_form,
            reading=entry.reading,
            glossary=glossary,
            glossary_full=glossary_full,
            sentence=entry.sentence,
            sentence_cloze=sentence_cloze,
            frequency=str(entry.freq) if entry.freq < 999_999 else "",
            part_of_speech=", ".join(pos_all),
        )
        fields = render_field_mapping(mapping, word)

        deck = config.anki_deck
        model = config.anki_model
        url = config.anki_connect_url
        tag = config.anki_tag
        check_dupes = config.anki_check_duplicates
        expression_field = next((f for f, t in mapping.items() if "{expression}" in t), None)

        button.setEnabled(False)
        button.setText("…")

        def worker():
            try:
                client = AnkiConnectClient(url)
                if check_dupes and expression_field and client.is_duplicate(deck, model, expression_field, entry.written_form):
                    self.mine_finished.emit(button, "dup", f"'{entry.written_form}' is already in {deck}")
                    return
                client.add_note(deck, model, fields, tags=[tag] if tag else [])
                self.mine_finished.emit(button, "ok", f"Added '{entry.written_form}' to {deck}")
            except AnkiConnectError as e:
                logger.warning(f"Mining '{entry.written_form}' failed: {e}")
                self.mine_finished.emit(button, "err", str(e))

        threading.Thread(target=worker, daemon=True, name="AnkiMine").start()

    def _apply_mine_feedback(self, button: QPushButton, status: str, message: str):
        logger.info(f"Mine result [{status}]: {message}")
        if status == "ok":
            button.setText("✓")
            button.setToolTip("Added to Anki")
        elif status == "dup":
            button.setText("=")
            button.setToolTip("Already in Anki")
            button.setEnabled(True)
        else:
            button.setText("!")
            button.setToolTip(f"Mining failed: {message}")
            button.setEnabled(True)

    # ------------------------------------------------------------------ #
    # Positioning / visibility (unchanged from upstream)
    # ------------------------------------------------------------------ #
    def move_to(self, x, y):
        cursor_point = QPoint(x, y)
        screen = QApplication.screenAt(cursor_point) or QApplication.primaryScreen()
        screen_geo = screen.geometry()
        popup_size = self.size()
        offset = 15

        ratio = screen.devicePixelRatio()
        x, y = magpie_manager.transform_raw_to_visual((int(x), int(y)), ratio)

        mode = config.popup_position_mode

        if mode == 'visual_novel_mode':
            screen_height = screen_geo.height()
            cursor_y_in_screen = y - screen_geo.top()
            if cursor_y_in_screen > (2 * screen_height / 3):
                is_below = False
            elif cursor_y_in_screen < (screen_height / 3):
                is_below = True
            else:
                is_below = cursor_y_in_screen < (screen_height / 2)
            final_y = (y + offset) if is_below else (y - popup_size.height() - offset)

            if final_y < screen_geo.top(): final_y = screen_geo.top()
            if final_y + popup_size.height() > screen_geo.bottom():
                final_y = screen_geo.bottom() - popup_size.height()

            screen_width = screen_geo.width()
            cursor_x_in_screen = x - screen_geo.left()
            pos_right = x + offset
            pos_center = x - popup_size.width() / 2.0
            pos_left = x - popup_size.width() - offset

            if cursor_x_in_screen < screen_width / 2.0:
                ratio = cursor_x_in_screen / (screen_width / 2.0)
                final_x = pos_right * (1 - ratio) + pos_center * ratio
            else:
                ratio = (cursor_x_in_screen - (screen_width / 2.0)) / (screen_width / 2.0)
                final_x = pos_center * (1 - ratio) + pos_left * ratio

        elif mode == 'flip_horizontally':
            preferred_x = x + offset
            final_x = preferred_x if preferred_x + popup_size.width() <= screen_geo.right() else x - popup_size.width() - offset

            final_y = y + offset
            if final_y + popup_size.height() > screen_geo.bottom(): final_y = screen_geo.bottom() - popup_size.height()
            if final_y < screen_geo.top(): final_y = screen_geo.top()

        elif mode == 'flip_vertically':
            final_x = x + offset
            if final_x + popup_size.width() > screen_geo.right(): final_x = screen_geo.right() - popup_size.width()
            if final_x < screen_geo.left(): final_x = screen_geo.left()

            preferred_y = y + offset
            final_y = preferred_y if preferred_y + popup_size.height() <= screen_geo.bottom() else y - popup_size.height() - offset

        else:  # 'flip_both'
            preferred_x = x + offset
            final_x = preferred_x if preferred_x + popup_size.width() <= screen_geo.right() else x - popup_size.width() - offset

            preferred_y = y + offset
            final_y = preferred_y if preferred_y + popup_size.height() <= screen_geo.bottom() else y - popup_size.height() - offset

        final_x = max(screen_geo.left(), min(final_x, screen_geo.right() - popup_size.width()))
        final_y = max(screen_geo.top(), min(final_y, screen_geo.bottom() - popup_size.height()))

        self.move(int(final_x), int(final_y))

    def hide_popup(self):
        if not self.is_visible:
            return
        self.hide()
        self.is_visible = False
        self.toggle_active = False  # Reset toggle when manually hiding
        
        # Reset the latest data to None when the popup is fully dismissed,
        # so that a fresh scan can be triggered later.
        with self._data_lock:
            self._latest_data = None
            
        QTimer.singleShot(50, lambda: self._release_lock_safely())
        self._restore_focus_on_mac()

    def _release_lock_safely(self):
        logger.debug("hide_popup releasing lock...")
        self.shared_state.screen_lock.release()
        logger.debug("...successfully released lock by hide_popup")

    def show_popup(self):
        if self.is_visible:
            return
        logger.debug("show_popup acquiring lock...")
        self.shared_state.screen_lock.acquire()
        logger.debug("...successfully acquired lock by show_popup")

        self._store_active_window_on_mac()
        self.show()
        if IS_MACOS:
            self.raise_()

        self.is_visible = True

    def reapply_settings(self):
        logger.debug("Popup: Re-applying settings and triggering font recalibration.")
        self._apply_frame_stylesheet()
        self.is_calibrated = False

    def _store_active_window_on_mac(self):
        if not IS_MACOS or not Quartz:
            return
        try:
            active_app = Quartz.NSWorkspace.sharedWorkspace().frontmostApplication()
            if active_app:
                self._previous_active_window_on_mac = active_app
        except Exception as e:
            logger.warning(f"Failed to store active window: {e}")
            self._previous_active_window_on_mac = None

    def _restore_focus_on_mac(self):
        if not IS_MACOS or not Quartz or not self._previous_active_window_on_mac:
            return
        try:
            self._previous_active_window_on_mac.activateWithOptions_(Quartz.NSApplicationActivateAllWindows)
        except Exception as e:
            logger.warning(f"Failed to restore focus: {e}")
        finally:
            self._previous_active_window_on_mac = None