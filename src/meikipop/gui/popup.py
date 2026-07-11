# meikipop/gui/popup.py
import json
import logging
import threading
import sys  # Explicitly imported to prevent path/exiting conflicts
from typing import List, Optional

from PyQt6.QtCore import QTimer, QPoint, QSize, QEvent, pyqtSignal
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QFontInfo
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QApplication, QPushButton, QSizePolicy, QScrollArea
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
        try:
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

            # --- NO-REPETITION CLIPBOARD TRACKER ---
            self._last_copied_word = ""

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

            # Base layout
            main_layout = QVBoxLayout(self)
            main_layout.setContentsMargins(0, 0, 0, 0)

            # Outer styled Frame
            self.frame = QFrame()
            self._apply_frame_stylesheet()
            
            # --- FIXED SIZE & SCROLLBAR WORKFLOW ---
            self.resize(450, 600)  # Safe default initialization size to prevent layout assertions

            # Vertical Scroll Area Setup
            self.scroll_area = QScrollArea()
            self.scroll_area.setWidgetResizable(True)
            self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.scroll_area.setStyleSheet("background: transparent; border: none;")

            # Inner Scroll Content Widget
            self.scroll_content = QWidget()
            self.scroll_content.setStyleSheet("background: transparent; border: none;")
            self.content_layout = QVBoxLayout(self.scroll_content)
            self.content_layout.setContentsMargins(10, 10, 10, 10)
            self.content_layout.setSpacing(6)

            self.scroll_area.setWidget(self.scroll_content)

            # Frame layout containing only the Scroll Area
            frame_layout = QVBoxLayout(self.frame)
            frame_layout.setContentsMargins(0, 0, 0, 0)
            frame_layout.addWidget(self.scroll_area)

            main_layout.addWidget(self.frame)
            self.hide()
        except Exception as e:
            logger.exception("CRITICAL ERROR IN POPUP INIT: %s", e)
            print(f"\nCRITICAL ERROR IN POPUP INIT:\n{e}\n", flush=True)
            import traceback
            traceback.print_exc()
            sys.exit(1)

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
                background-color: transparent;
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
                if self._latest_data:
                    return
            
            self._latest_data = data

    def get_latest_data(self):
        with self._data_lock:
            return self._latest_data

    def _show_loading_state(self):
        """Displays a clean scanning state instantly on Shift keypress."""
        self._clear_entry_widgets()
        
        loading_label = QLabel("Scanning screen...")
        loading_label.setStyleSheet("color: #888; font-size: 14px; font-style: italic; padding: 10px;")
        
        self.content_layout.addWidget(loading_label)
        self._entry_widgets.append(loading_label)
        
        self.setFixedSize(450, 150)  # Start with a safe height for the loading state (width 450)
        self.show_popup()

    def process_latest_data_loop(self):
        try:
            if not self.is_calibrated:
                self._calibrate_empirically()

            latest_data = self.get_latest_data()
            if latest_data and latest_data != self._last_latest_data:
                self._build_entries(latest_data)
                
                # Locked strictly to exactly 450x600 pixels (Requirement 6 & Size Fix)
                self.setFixedSize(450, 600)

                # Re-trigger position alignment now that we know the final height of the data
                mouse_pos = QCursor.pos()
                self.move_to(mouse_pos.x(), mouse_pos.y())

                # === AUTO-COPY FULL SENTENCE TO CLIPBOARD WITHOUT REPETITIONS ===
                if len(latest_data) > 0:
                    first_entry = latest_data[0]
                    scanned_word = getattr(first_entry, 'written_form', '') or getattr(first_entry, 'character', '')
                    sentence_text = getattr(first_entry, 'sentence', '').strip()
                    
                    # Use the full sentence if available; otherwise, fall back to the single word
                    target_text = sentence_text if sentence_text else scanned_word
                    
                    # Only copy if it is a new line (prevents spamming clipboard history)
                    if target_text and target_text != self._last_copied_word:
                        QApplication.clipboard().setText(target_text)
                        self._last_copied_word = target_text

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
                    # If popup is hidden, toggle it ON instantly
                    self.toggle_active = True
                    mouse_pos = QCursor.pos()
                    self.move_to(mouse_pos.x(), mouse_pos.y())
                    
                    # If data has not yet arrived, show the loading state immediately!
                    if not data_present:
                        self._show_loading_state()
                    
            self._hotkey_was_active_last_tick = hotkey_active

            # Keep popup visible if pinned, hotkey active, or locked on via toggle
            should_show = data_present and config.is_enabled and (
                self.is_pinned or hotkey_active or self.toggle_active
            )

            # In manual toggle mode, if we are loading, we still show the popup container
            if self.toggle_active and not data_present:
                should_show = True

            if should_show:
                self.show_popup()
            else:
                self.hide_popup()

            # Follow cursor only if we are not hovering over the popup and we are NOT locked on via toggle.
            if not self.is_pinned and not self.toggle_active:
                if hotkey_active:
                    mouse_pos = QCursor.pos()
                    self.move_to(mouse_pos.x(), mouse_pos.y())
        except Exception as e:
            logger.exception("CRITICAL ERROR IN POPUP UPDATE LOOP: %s", e)
            print(f"\nCRITICAL ERROR IN POPUP UPDATE LOOP:\n{e}\n", flush=True)

    # ------------------------------------------------------------------ #
    # Row building
    # ------------------------------------------------------------------ #
    def _clear_entry_widgets(self):
        # Recursively clear all layouts and widgets safely to avoid memory or orphaning leaks (Requirement 6)
        while self.content_layout.count() > 0:
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._entry_widgets = []

    def _make_separator(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: rgba(255,255,255,40); max-height: 1px; border: none;")
        return line

    def _build_entries(self, entries: List) -> int:
        self._clear_entry_widgets()

        for i, entry in enumerate(entries):
            if i > 0:
                sep = self._make_separator()
                self.content_layout.addWidget(sep)
                self._entry_widgets.append(sep)

            if isinstance(entry, KanjiEntry):
                row, ratio = self._build_kanji_row(entry)
            else:
                row, ratio = self._build_word_row(entry)

            self.content_layout.addWidget(row)
            self._entry_widgets.append(row)

        # Inject vertical stretch spacer at the bottom (Requirement 5: Wasted Space Fix)
        # This tightly packs all content rows to the top, eliminating empty spacing gaps.
        self.content_layout.addStretch(1)
        self.content_layout.activate()
        
        # Calculate the natural layout height to tell the caller how big to size the window
        natural_height = self.content_layout.sizeHint().height() + 30
        return natural_height

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

        def_text_parts_html = []
        for idx, sense in enumerate(entry.senses):
            glosses = sense.get('glosses', [])
            glosses_str = ", ".join(glosses) if (glosses and config.show_all_glosses) else (glosses[0] if glosses else "")
            tags_list = sense.get('tags', [])
            
            # Formulate styled line blocks instead of clumping with raw line breaks
            sense_html = f'<div style="margin-bottom: 5px; line-height: 1.45;">'
            sense_html += f'<b>{idx + 1}.</b> ' if config.show_all_glosses else ""
            
            # Part-of-Speech tags completely excluded to satisfy Requirement 2
            
            if config.show_tags and tags_list:
                tags_str = f'[{", ".join(tags_list)}] '
                sense_html += f'<span style="color:{c_text}; font-size:{config.font_size_definitions - 2}px; opacity:0.7;">{tags_str}</span>'
            
            sense_html += f'{glosses_str}</div>'
            def_text_parts_html.append(sense_html)

        full_def_text_html = "".join(def_text_parts_html)
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

        # --- DYNAMIC RICH NESTED LIST GENERATION ---
        html_senses = []
        for sense in entry.senses:
            glosses = sense.get('glosses', [])
            if not glosses:
                continue
            
            # POS strings are excluded entirely from Anki card definitions for clean listings (Requirement 2)
            
            if len(glosses) == 1:
                # Single meaning/synonym
                html_senses.append(f"<li>{glosses[0]}</li>")
            else:
                # Multiple synonyms under this sense (rendered as circular sub-bullets)
                bullets = "".join(f"<li>{g}</li>" for g in glosses)
                html_senses.append(f"<li><ul style='list-style-type: circle; margin-top: 2px; margin-bottom: 2px; padding-left: 20px;'>{bullets}</ul></li>")
        
        # Combine into a structured, padded ordered list (1., 2., 3...)
        beautiful_glossary = f"<ol style='margin-top: 2px; margin-bottom: 2px; padding-left: 20px; line-height: 1.45;'>{''.join(html_senses)}</ol>"

        # Map both the short and full glossary variables to our rich list structure
        # Formats the furigana into standard Kanji[Kana] layout for Anki's native filter (Requirement 2)
        if entry.written_form != entry.reading:
            furigana_reading = f"{entry.written_form}[{entry.reading}]"
        else:
            furigana_reading = entry.reading

        glossary = beautiful_glossary
        glossary_full = beautiful_glossary

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
            reading=furigana_reading,
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
    # Positioning / visibility (using fixed 450x600 size constants)
    # ------------------------------------------------------------------ #
    def move_to(self, x, y):
        cursor_point = QPoint(x, y)
        screen = QApplication.screenAt(cursor_point) or QApplication.primaryScreen()
        screen_geo = screen.geometry()
        
        # Locked strictly to exactly 450x600 pixels (Requirement 6 & Size Fix)
        popup_width = 450
        popup_height = 600
        offset = 15

        ratio = screen.devicePixelRatio()
        
        # Standard cursor-arrow offset adjustments (+4px, +6px) 
        # to compensate for logical arrow tips on High-DPI monitors (Requirement 6: Precision Fix)
        adjusted_x = int(x) + 4
        adjusted_y = int(y) + 6
        
        x, y = magpie_manager.transform_raw_to_visual((adjusted_x, adjusted_y), ratio)

        # Standard centered positioning under the cursor
        final_x = x - (popup_width / 2)
        final_y = y + offset

        # Smooth boundary adjustment: slide popup inside screen if it goes off bottom/sides
        if final_y + popup_height > screen_geo.bottom():
            final_y = screen_geo.bottom() - popup_height - 10

        if final_x < screen_geo.left():
            final_x = screen_geo.left() + 10
        if final_x + popup_width > screen_geo.right():
            final_x = screen_geo.right() - popup_width - 10

        # Protect against clipping top-of-screen bounds
        if final_y < screen_geo.top():
            final_y = screen_geo.top()

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