"""Cairn voices, loaded from voices/manifest.yaml at startup (UC-REG-12).

A voice changes tone only. core.md holds the rules every voice follows, and
nothing in a voice file can override it. The user's choice is stored as
users.voice. The ids must match the users_voice_known check constraint
(migration 0009) and schemas.Voice, so the app refuses to start if the
manifest drifts from either.

The prompt blocks follow voices/assemble_prompt.py: shared core first (cached
across every user and voice), then the voice (cached across everyone on that
voice), then per-turn context (not cached).
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

import yaml

from .schemas import Voice

# The repository's voices/ folder. Deployments that don't ship the repository
# layout point CAIRN_VOICES_DIR at a copy of that folder.
DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[2] / "voices"

SAFETY_MODES = ("normal", "overwhelm", "acute_distress", "risk_of_harm")

_ID_LINE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)
_LABEL_LINE = re.compile(r'^Onboarding label:\s*"(.+)"\s*$', re.MULTILINE)
_REFERENCE = re.compile(r'^## Reference response\s*\nUser:\s*"(.+)"\s*\n\s*\nCairn:\s*"(.+)"\s*$', re.MULTILINE)


class VoiceError(ValueError):
    """The voices folder can't be loaded. The message names the file and the problem."""


@dataclass(frozen=True)
class VoiceSpec:
    id: Voice
    label: str
    tagline: str
    text: str
    sample_situation: str   # the user's line in the reference response
    sample: str             # Cairn's reply to it, in this voice


@dataclass(frozen=True)
class VoiceCatalog:
    version: str
    core: str
    default: Voice
    voices: dict[Voice, VoiceSpec]

    def __getitem__(self, voice_id: Voice | str) -> VoiceSpec:
        return self.voices[Voice(voice_id)]

    @property
    def sample_situation(self) -> str:
        return self.voices[self.default].sample_situation

    def system_blocks(self, voice_id: Voice | str, safety_mode: str, case_context: str,
                      citations: str) -> list[dict]:
        """The system prompt for one turn, for the user's stored voice.

        An unknown voice is an error, not a silent fallback. The database only
        stores known ids, so one here means the manifest and schema drifted.
        """
        if safety_mode not in SAFETY_MODES:
            raise ValueError(f"unknown safety_mode {safety_mode!r}")
        voice = self[voice_id]
        return [
            {"type": "text", "text": self.core, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"<voice>\n{voice.text}\n</voice>", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": (
                f"<safety_mode>{safety_mode}</safety_mode>\n"
                f"<case_context>\n{case_context}\n</case_context>\n"
                f"<citations>\n{citations}\n</citations>"
            )},
        ]


def _read(root: pathlib.Path, name: object, what: str) -> str:
    if not isinstance(name, str) or not name:
        raise VoiceError(f"manifest.yaml: {what} needs a file name")
    path = (root / name).resolve()
    if root.resolve() not in path.parents:
        raise VoiceError(f"manifest.yaml: {what} file {name} is outside the voices folder")
    if not path.is_file():
        raise VoiceError(f"manifest.yaml: {what} file {name} does not exist")
    # Kept byte for byte, so the cached prompt prefix matches voices/assemble_prompt.py.
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise VoiceError(f"{name}: file is empty")
    return text


def _voice(root: pathlib.Path, entry: dict) -> VoiceSpec:
    vid = entry.get("id")
    try:
        voice_id = Voice(vid)
    except ValueError:
        raise VoiceError(f"manifest.yaml: voice id {vid!r} is not one of {[v.value for v in Voice]}. "
                         "Add it to schemas.Voice and the users.voice check constraint first.") from None
    label, tagline = entry.get("label"), entry.get("tagline")
    if not (isinstance(label, str) and label.strip() and isinstance(tagline, str) and tagline.strip()):
        raise VoiceError(f"manifest.yaml: voice {vid} needs a label and a tagline")
    name = entry.get("file")
    text = _read(root, name, f"voice {vid}")

    file_id = _ID_LINE.search(text)
    if not file_id or file_id.group(1) != vid:
        raise VoiceError(f"{name}: the id line must say {vid}")
    file_label = _LABEL_LINE.search(text)
    if not file_label or file_label.group(1) != f"{label}: {tagline}":
        raise VoiceError(f"{name}: the onboarding label must match manifest.yaml ({label}: {tagline})")
    reference = _REFERENCE.search(text)
    if not reference:
        raise VoiceError(f"{name}: needs a reference response with a User line and a Cairn line")
    return VoiceSpec(id=voice_id, label=label, tagline=tagline, text=text,
                     sample_situation=reference.group(1), sample=reference.group(2))


def load_voices(directory: str | pathlib.Path | None = None) -> VoiceCatalog:
    root = pathlib.Path(directory or DEFAULT_DIR)
    manifest_path = root / "manifest.yaml"
    if not manifest_path.is_file():
        raise VoiceError(f"{manifest_path} does not exist. Set CAIRN_VOICES_DIR to the voices folder.")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise VoiceError(f"manifest.yaml is not valid YAML: {e}") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("voices"), list):
        raise VoiceError("manifest.yaml needs a voices list")

    core = _read(root, manifest.get("core"), "core")
    voices: dict[Voice, VoiceSpec] = {}
    for entry in manifest["voices"]:
        if not isinstance(entry, dict):
            raise VoiceError("manifest.yaml: each voice needs id, label, tagline, and file")
        spec = _voice(root, entry)
        if spec.id in voices:
            raise VoiceError(f"manifest.yaml: voice {spec.id.value} is listed twice")
        voices[spec.id] = spec

    missing = [v.value for v in Voice if v not in voices]
    if missing:
        raise VoiceError(f"manifest.yaml: no entry for {missing}. Every stored voice must be loadable.")
    try:
        default = Voice(manifest.get("default_voice"))
    except ValueError:
        raise VoiceError(f"manifest.yaml: default_voice {manifest.get('default_voice')!r} is not a listed voice") \
            from None
    if tuple(manifest.get("safety_modes") or ()) != SAFETY_MODES:
        raise VoiceError(f"manifest.yaml: safety_modes must be {list(SAFETY_MODES)}, as core.md describes")
    situations = {v.sample_situation for v in voices.values()}
    if len(situations) != 1:
        raise VoiceError("every voice's reference response must answer the same user message (UC-REG-12)")
    return VoiceCatalog(version=str(manifest.get("version", "")), core=core, default=default, voices=voices)
