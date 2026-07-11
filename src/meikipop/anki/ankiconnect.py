# meikipop/anki/ankiconnect.py
"""
Minimal AnkiConnect client. No third-party dependencies (urllib only),
matching the rest of meikipop's lean dependency footprint.

AnkiConnect API docs: https://foosoft.net/projects/anki-connect/
"""
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ANKICONNECT_VERSION = 6


class AnkiConnectError(Exception):
    """Raised when AnkiConnect is unreachable or returns an error."""
    pass


@dataclass
class MineableWord:
    """Everything a field-mapping marker might reference for one dictionary entry."""
    expression: str
    reading: str
    glossary: str          # first/primary gloss, plain text
    glossary_full: str      # all senses, semicolon separated
    sentence: str
    sentence_cloze: str     # sentence with the word replaced by a cloze marker-friendly wrapper
    frequency: str
    part_of_speech: str
    extra: Dict[str, str] = field(default_factory=dict)


class AnkiConnectClient:
    def __init__(self, url: str = "http://127.0.0.1:8765", timeout: float = 3.0):
        self.url = url
        self.timeout = timeout

    def _invoke(self, action: str, **params):
        payload = json.dumps({"action": action, "version": ANKICONNECT_VERSION, "params": params}).encode("utf-8")
        req = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise AnkiConnectError(
                f"Couldn't reach AnkiConnect at {self.url}. Is Anki running with the AnkiConnect add-on installed?"
            ) from e
        except json.JSONDecodeError as e:
            raise AnkiConnectError("AnkiConnect returned an invalid response.") from e

        if len(body) != 2 or "error" not in body or "result" not in body:
            raise AnkiConnectError("AnkiConnect response missing 'error'/'result' fields.")
        if body["error"] is not None:
            raise AnkiConnectError(str(body["error"]))
        return body["result"]

    def is_available(self) -> bool:
        try:
            self._invoke("version")
            return True
        except AnkiConnectError:
            return False

    def deck_names(self) -> List[str]:
        return self._invoke("deckNames")

    def model_names(self) -> List[str]:
        return self._invoke("modelNames")

    def model_field_names(self, model_name: str) -> List[str]:
        return self._invoke("modelFieldNames", modelName=model_name)

    def find_notes(self, query: str) -> List[int]:
        return self._invoke("findNotes", query=query)

    def is_duplicate(self, deck: str, model: str, expression_field: str, expression_value: str) -> bool:
        """Best-effort duplicate check against the mapped expression field."""
        if not expression_field or not expression_value:
            return False
        escaped = expression_value.replace('"', '\\"')
        query = f'deck:"{deck}" note:"{model}" "{expression_field}:{escaped}"'
        try:
            return len(self.find_notes(query)) > 0
        except AnkiConnectError:
            return False

    def add_note(
        self,
        deck: str,
        model: str,
        fields: Dict[str, str],
        tags: Optional[List[str]] = None,
        allow_duplicate: bool = True,
    ) -> int:
        note = {
            "deckName": deck,
            "modelName": model,
            "fields": fields,
            "tags": tags or [],
            "options": {
                "allowDuplicate": allow_duplicate,
                "duplicateScope": "deck",
            },
        }
        return self._invoke("addNote", note=note)


def render_field_mapping(mapping: Dict[str, str], word: MineableWord) -> Dict[str, str]:
    """
    Resolve {marker} placeholders in each configured Anki field's template
    against a MineableWord. Unknown markers are left untouched so mapping
    mistakes are visible in Anki instead of silently dropped.
    """
    substitutions = {
        "expression": word.expression,
        "reading": word.reading,
        "glossary": word.glossary,
        "glossary-full": word.glossary_full,
        "sentence": word.sentence,
        "sentence-cloze": word.sentence_cloze,
        "frequency": word.frequency,
        "pos": word.part_of_speech,
    }
    substitutions.update(word.extra)

    rendered = {}
    for anki_field, template in mapping.items():
        text = template
        for marker, value in substitutions.items():
            text = text.replace(f"{{{marker}}}", value or "")
        rendered[anki_field] = text
    return rendered
