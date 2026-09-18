"""Transcription: turn a product's video ads into scripts, in their own language.

This is the module the owner asked for by name — "transcription, and transcribing
LOCAL languages (Hindi and the other Indic languages the ads are in)". It is a
port, not an invention: every accuracy decision below was paid for once already
in v1 (``meta_main14/services/creative_intel.py``, ``transcribe_providers.py``,
``language_detect.py``) and each one exists because something was wrong before it.

THE RULES, in the order they matter:

1. **It never runs by itself.** No fetch hook, no ingest hook, no scheduler, no
   startup backfill, no "while we're here". Transcription costs real money per
   minute of audio, so the only thing that starts it is the owner pressing
   "Generate scripts", and every press writes a ``transcription_runs`` receipt.
   ``queue_product`` (which spends nothing) and ``process_run`` (which does) are
   deliberately two functions so this is enforceable, and tested.

2. **Whisper large-v3, not turbo.** A live A/B on the same Hindi ad: turbo
   garbled it, full large-v3 did not. ``GROQ_MODEL`` is pinned and a test pins it.

3. **Indic audio goes to Sarvam when a Sarvam key exists.** Same A/B, Hindi ad:
   Whisper produced "बई सवी कार उनर्स ... नापतोर", Sarvam produced "भाई सभी कार
   ओनर्स ... नापतोल" — right words, right brand, right script. ``preferred_provider``
   is the routing, and it only ever *upgrades* a Groq setting; an explicit Sarvam
   choice is respected as-is.

4. **16 kHz mono FLAC.** Both models are trained at 16 kHz mono; FLAC is lossless,
   so no codec artefact eats a Devanagari phoneme. WAV/pcm_s16le is the fallback,
   and the raw video is the fallback to that.

5. **Three hallucination guards**, because quiet audio makes Whisper invent
   speech: (a) a silence pre-check skips ASR entirely on near-silent clips;
   (b) per-segment, a segment that is BOTH probably-non-speech AND low-confidence
   is dropped; (c) whole-transcript, ``validate_transcript`` rejects impossible
   speaking rates and repetition loops. A guarded result is stored COMPLETED with
   ``low_confidence=1`` and an empty transcript — not failed — so a resume never
   re-hallucinates it and it never pollutes a language folder or a cluster.

6. **Script evidence beats the provider's own label.** ``reconcile_transcript_language``
   reads the transcript's Unicode script: Devanagari/Tamil/... output overrules a
   provider that said "ur"/"ne"/"sa". Hindi vs Marathi turns on the letter ळ and a
   marker wordlist, because they share the script and nothing else can separate
   them.

7. **Two dedupe layers, one provenance rule.** Layer 1 is the sha256 of the media
   URL minus its query string, so an fbcdn URL re-signed with a fresh ``oe=`` is
   not a new video. Layer 2 is the sha256 of the downloaded bytes: identical bytes
   copy an existing transcript for zero API calls. The provenance rule is that a
   *copy* is never a valid dedupe source — otherwise a bad transcript launders
   itself through its own copies and the accuracy work above is silently undone.

8. **Keys live in the ``settings`` table**, are read only here, are masked
   wherever they are shown, and are never logged, never echoed in a response and
   never written to a run row. ``_redact`` is applied to every provider error
   before it is stored, because HTTP error bodies quote the request.

What is deliberately NOT ported: v1's Gemini-first path. ``docs/03-architecture.md``
§5 is normative — groq whisper-large-v3, temperature 0 — and a second inference
family is a second set of hallucination behaviours to guard.

Everything here is offline-testable: the single network choke point for providers
is ``_http_post_multipart``, and media download is ``_http_get``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sqlite3
import tempfile
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import config as app_config
from . import db
from . import media_refresh
from .media_refresh import has_signed_expiry, is_url_expired
from .time_utils import age_seconds, utc_now

log = logging.getLogger("adspy2.transcription")


# ===========================================================================
# PART 1 — language detection
# Port of meta_main14/services/language_detect.py. Pure text, zero network.
# ===========================================================================

# Unicode script blocks -> (language code, language name).
SCRIPT_LANGUAGES: dict[str, tuple[str, str]] = {
    "devanagari": ("hi", "Hindi"),
    "bengali": ("bn", "Bengali"),
    "gurmukhi": ("pa", "Punjabi"),
    "gujarati": ("gu", "Gujarati"),
    "oriya": ("or", "Odia"),
    "tamil": ("ta", "Tamil"),
    "telugu": ("te", "Telugu"),
    "kannada": ("kn", "Kannada"),
    "malayalam": ("ml", "Malayalam"),
    "arabic": ("ur", "Urdu"),
    "latin": ("en", "English"),
}

LANGUAGE_NAMES: dict[str, str] = {
    "hi": "Hindi", "hi-Latn": "Hinglish", "bn": "Bengali", "pa": "Punjabi",
    "gu": "Gujarati", "or": "Odia", "ta": "Tamil", "te": "Telugu",
    "kn": "Kannada", "ml": "Malayalam", "ur": "Urdu", "en": "English",
    "und": "Unknown",
    # Codes the providers report beyond what the offline detector emits.
    "mr": "Marathi", "as": "Assamese", "ne": "Nepali", "sa": "Sanskrit",
    "si": "Sinhala", "es": "Spanish", "fr": "French", "ar": "Arabic",
}

_CODE_ALIASES: dict[str, str] = {
    "mar": "mr", "hin": "hi", "ben": "bn", "pan": "pa", "guj": "gu",
    "ori": "or", "tam": "ta", "tel": "te", "kan": "kn", "mal": "ml",
    "urd": "ur", "eng": "en", "asm": "as", "nep": "ne",
}

_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0900, 0x097F, "devanagari"),
    (0x0980, 0x09FF, "bengali"),
    (0x0A00, 0x0A7F, "gurmukhi"),
    (0x0A80, 0x0AFF, "gujarati"),
    (0x0B00, 0x0B7F, "oriya"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0x0C80, 0x0CFF, "kannada"),
    (0x0D00, 0x0D7F, "malayalam"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x0041, 0x005A, "latin"),
    (0x0061, 0x007A, "latin"),
    (0x00C0, 0x024F, "latin"),
)

# Ads Library chrome that pollutes an ad's own copy. "Open Drop-down" is here
# because 2,314 v2-scraped ads currently carry it as their entire ad_text — the
# extractor regression Group C fixes. Language detection must not call that
# English.
UI_NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^open drop[- ]?down$", re.I),
    re.compile(r"^\d+\s+ads?\s+use\s+this\s+creative(\s+and\s+text)?\.?$", re.I),
    re.compile(r"^see\s+(ad|summary)\s+details$", re.I),
    re.compile(r"^this ad has multiple versions\.?$", re.I),
    re.compile(r"^sponsored$", re.I),
    re.compile(r"^active$", re.I),
    re.compile(r"^inactive$", re.I),
    re.compile(r"^library id:?\s*\d*$", re.I),
    re.compile(r"^लाइब्रेरी id:?\s*\d*$", re.I),
    re.compile(r"^started running on .+$", re.I),
    re.compile(r"^platforms?$", re.I),
    re.compile(r"^(learn more|shop now|sign up|download|install now|book now|"
               r"contact us|get offer|send message|apply now|subscribe|"
               r"watch more|play game|get quote|order now)$", re.I),
    re.compile(r"^(like|comment|share)$", re.I),
)

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_WORD_RE = re.compile(r"[a-z']+")

# Romanized-Hindi (Hinglish) heuristic. Every entry is common in romanized
# Hindi and is not an ordinary English word.
HINGLISH_WORDS: frozenset[str] = frozenset({
    "hai", "hain", "tha", "thi", "hoga", "hogi", "honge", "raha", "rahi",
    "rahe", "gaya", "gayi", "nahi", "nahin", "nhi", "mat", "kyun", "kyu",
    "kyunki", "kaise", "kaisa", "kaisi", "kya", "kab", "kahan", "kitna",
    "kitni", "kitne", "kaun", "aap", "aapka", "aapki", "aapke", "apna",
    "apni", "apne", "tum", "tumhara", "hum", "humara", "mera", "meri",
    "mere", "tera", "teri", "uska", "uski", "sabka", "sabse", "ka", "ki",
    "ke", "ko", "se", "mein", "par", "wala", "wale", "wali", "vala",
    "vali", "karo", "kare", "karein", "karna", "karke", "kiya", "kijiye",
    "karega", "karegi", "milega", "milegi", "milta", "milti", "badhegi",
    "badhega", "badhao", "chahiye", "chahte", "dekho", "dekhe", "dekhiye",
    "suno", "boliye", "bolo", "jaano", "jaane", "jaana", "paayein",
    "paaye", "payein", "banao", "banaye", "bataye", "batao", "bataenge",
    "lagao", "lagaye", "uthao", "hatao", "bachao", "bachaye", "kamao",
    "kamaye", "khareedo", "kharido", "paisa", "paise", "paisey", "rupaye",
    "rupay", "rupee", "lakh", "lakhs", "crore", "crores", "ghar", "shaadi",
    "shadi", "pyar", "pyaar", "zindagi", "jeevan", "kismat", "kismet",
    "bhagya", "jyotish", "upay", "samasya", "samadhan", "ilaj", "dard",
    "sehat", "swasth", "baal", "chehra", "twacha", "pet", "vajan", "wazan",
    "abhi", "sirf", "turant", "jaldi", "muft", "zaroor", "zarur", "bahut",
    "bohot", "bhi", "aur", "lekin", "magar", "phir", "fir", "toh", "yeh",
    "yah", "woh", "wo", "iska", "iski", "yahan", "wahan", "accha",
    "achha", "acha", "sahi", "galat", "naya", "nayi", "purana", "purani",
    "bada", "badi", "chota", "choti", "saath", "sath", "baat", "baatein",
    "din", "raat", "aaj", "kal", "hafta", "mahina", "saal", "samay",
    "duniya", "log", "logon", "dost", "bhai", "didi", "beta", "beti",
    "maa", "pita", "parivar", "namaste", "namaskar", "dhanyawad", "shukriya",
    "swagat", "offer", "free",
})
# "offer"/"free" are English on their own; they only count alongside a real
# Hindi word, so they can never satisfy the >=2 distinct rule by themselves.
_WEAK_HINGLISH_WORDS: frozenset[str] = frozenset({"offer", "free"})

_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0xFE00, 0xFE0F),
    (0x200D, 0x200D), (0x2190, 0x21FF), (0x2B00, 0x2BFF),
)

# Hindi and Marathi share Devanagari, so script detection alone cannot separate
# them. These function words — and the Marathi-only letter ळ — do.
_MARATHI_MARKER_WORDS: frozenset[str] = frozenset({
    "आहे", "आहेत", "आहोत", "नाही", "नाहीत", "नाहीये", "आणि", "तुम्ही",
    "तुमच्या", "तुमचा", "तुमची", "तुमचे", "आपल्या", "मध्ये", "केला", "केली",
    "केले", "झाला", "झाली", "झाले", "पाहिजे", "करा", "करावे", "करून",
    "म्हणून", "म्हणजे", "म्हणाले", "असे", "असून", "होते", "होता", "मिळेल",
    "मिळते", "मिळवा", "फक्त", "खरेदी", "वापरा", "वर्षानुवर्षे", "वाढत्या",
    "चष्मे", "च्या", "ला", "ने", "मराठी", "किंमत", "स्वस्त", "घ्या", "पहा",
})
_HINDI_MARKER_WORDS: frozenset[str] = frozenset({
    "है", "हैं", "और", "नहीं", "नही", "को", "में", "का", "की", "के", "करो",
    "करें", "रहा", "रही", "रहे", "गया", "गई", "आपके", "आपको", "आपकी",
    "चाहिए", "रुपये", "सकते", "सकता", "सकती", "हूँ", "हूं", "क्या", "लेकिन",
    "बहुत", "यह", "वह", "कर", "से", "पर", "भी", "हो", "दें", "लें", "वाला",
    "वाली", "जाएगा", "होगा", "कीजिए", "दीजिए", "अभी", "सिर्फ",
})
_MARATHI_ONLY_CHAR = "ळ"
_DEVANAGARI_WORD_RE = re.compile(r"[ऀ-ॿ]+")


def normalize_language_code(code: Any) -> str:
    """Collapse provider variants (mr-IN, mar, hi_in) onto canonical codes."""
    value = str(code or "").strip().lower().replace("_", "-")
    if not value:
        return "und"
    if value == "hi-latn":
        return "hi-Latn"
    base = value.split("-", 1)[0]
    return _CODE_ALIASES.get(base, base)


def language_name(code: Any) -> str:
    raw = str(code or "und")
    if raw in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[raw]
    normalized = normalize_language_code(raw)
    return LANGUAGE_NAMES.get(normalized, raw or "Unknown")


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _EMOJI_RANGES)


def strip_noise(text: Any) -> str:
    """Remove UI-noise lines, URLs and emoji; collapse whitespace per line."""
    raw = str(text or "")
    if not raw.strip():
        return ""
    kept: list[str] = []
    for line in raw.splitlines():
        line = _URL_RE.sub(" ", line)
        line = "".join(" " if _is_emoji(ch) else ch for ch in line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if any(pattern.match(line) for pattern in UI_NOISE_LINE_PATTERNS):
            continue
        kept.append(line)
    return "\n".join(kept)


def _script_of(char: str) -> str | None:
    code = ord(char)
    for start, end, script in _SCRIPT_RANGES:
        if start <= code <= end:
            return script
    return None


def _script_shares(text: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    total = 0
    for char in text:
        script = _script_of(char)
        if script is None:
            continue
        counts[script] = counts.get(script, 0) + 1
        total += 1
    if not total:
        return {}
    return {script: round(count / total, 4) for script, count in counts.items()}


def _devanagari_language(text: str) -> str:
    """Hindi vs Marathi inside Devanagari. Ambiguous Devanagari defaults to hi."""
    words = _DEVANAGARI_WORD_RE.findall(text)
    marathi = sum(1 for word in words if word in _MARATHI_MARKER_WORDS)
    hindi = sum(1 for word in words if word in _HINDI_MARKER_WORDS)
    if _MARATHI_ONLY_CHAR in text:
        marathi += 2                      # ळ is a strong Marathi signal
    if marathi >= 2 and marathi > hindi:
        return "mr"
    return "hi"


def _hinglish_hits(text: str) -> list[str]:
    tokens = set(_WORD_RE.findall(text.lower()))
    hits = sorted(tokens & HINGLISH_WORDS)
    strong = [word for word in hits if word not in _WEAK_HINGLISH_WORDS]
    return strong + [w for w in hits if w in _WEAK_HINGLISH_WORDS] if strong else strong


def detect_text_language(text: Any) -> dict[str, Any]:
    """Language of ad-creative text. Offline, deterministic.

    Returns {code, name, confidence, method, shares, hinglish_hits}.
    """
    cleaned = strip_noise(text)
    shares = _script_shares(cleaned)
    if not cleaned or not shares:
        return {"code": "und", "name": LANGUAGE_NAMES["und"], "confidence": 0.0,
                "method": "empty", "shares": {}, "hinglish_hits": []}

    dominant_script = max(shares, key=lambda script: shares[script])
    dominant_share = shares[dominant_script]

    if dominant_script != "latin":
        code, name = SCRIPT_LANGUAGES[dominant_script]
        if dominant_script == "devanagari":
            code = _devanagari_language(cleaned)
            name = LANGUAGE_NAMES.get(code, name)
        return {
            "code": code, "name": name, "confidence": round(dominant_share, 4),
            "method": "script" if dominant_share >= 0.999 else "script-mixed",
            "shares": shares, "hinglish_hits": [],
        }

    hits = _hinglish_hits(cleaned)
    if len(hits) >= 2:
        confidence = min(0.95, 0.55 + 0.08 * len(hits))
        return {
            "code": "hi-Latn", "name": LANGUAGE_NAMES["hi-Latn"],
            "confidence": round(confidence * dominant_share, 4),
            "method": "hinglish-wordlist", "shares": shares, "hinglish_hits": hits,
        }
    return {
        "code": "en", "name": LANGUAGE_NAMES["en"],
        "confidence": round(0.9 * dominant_share, 4),
        "method": "latin-default", "shares": shares, "hinglish_hits": hits,
    }


def reconcile_transcript_language(provider_code: Any, transcript: str) -> str:
    """Reconcile a provider's self-reported language against the transcript's
    own script evidence.

    Native-script evidence (Devanagari, Tamil, ...) beats any provider label —
    that is what fixes Hindi audio labelled ur/ne/sa and Tamil labelled hi.
    Latin output falls back to the provider, so romanized Hindi collapses onto
    one "hi" folder. Deterministic and idempotent.
    """
    provider = normalize_language_code(provider_code)
    detected = detect_text_language(transcript) or {}
    detected_code = normalize_language_code(str(detected.get("code") or "und"))
    if detected_code not in {"en", "hi-Latn", "und"} and detected.get("method") in {
        "script", "script-mixed",
    }:
        return detected_code
    if provider == "hi-Latn":
        return "hi"
    return provider if provider != "und" else detected_code


# ===========================================================================
# PART 2 — hallucination guards and audio preparation
# Port of meta_main14/services/transcribe_providers.py.
# ===========================================================================

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# Full whisper-large-v3. NOT -turbo: turbo garbled Hindi in a live A/B on the
# same ad, and every guard below is tuned against large-v3's segment stats.
GROQ_MODEL = "whisper-large-v3"
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_MODEL = "saarika:v2.5"

# Guard (b): a Whisper segment is dropped only when it is BOTH probably
# non-speech AND low confidence. Either alone is a normal quiet segment.
_SEG_NO_SPEECH_PROB = 0.6
_SEG_AVG_LOGPROB = -1.0
# Guard (a): audio under -35 dB for >=1s is silence; under ~1s of non-silence
# means no speech, and ASR is skipped entirely (zero API calls).
_SILENCE_NOISE_DB = "-35dB"
_SILENCE_MIN_D = "1"
_MIN_SPEECH_SECONDS = 1.0

HTTP_TIMEOUT = 300
FFMPEG_TIMEOUT = 180
MEDIA_TIMEOUT = 120
MEDIA_MAX_BYTES = 120 * 1024 * 1024
_AUTO_HOOK_WORDS = 12
_CHUNK_OVERLAP_SECONDS = 1.0


class ProviderError(RuntimeError):
    """Any provider failure (HTTP, quota, bad payload)."""


class ProviderConfigurationError(ProviderError):
    """The provider cannot run at all — almost always a missing API key."""


# Whisper's verbose_json reports the language as a lowercase English name.
_WHISPER_LANGUAGE_CODES = {
    "english": "en", "hindi": "hi", "tamil": "ta", "telugu": "te",
    "bengali": "bn", "kannada": "kn", "malayalam": "ml", "marathi": "mr",
    "gujarati": "gu", "punjabi": "pa", "urdu": "ur", "nepali": "ne",
    "sinhala": "si", "spanish": "es", "french": "fr", "german": "de",
    "portuguese": "pt", "italian": "it", "dutch": "nl", "polish": "pl",
    "russian": "ru", "ukrainian": "uk", "turkish": "tr", "arabic": "ar",
    "indonesian": "id", "malay": "ms", "vietnamese": "vi", "thai": "th",
    "tagalog": "tl", "japanese": "ja", "korean": "ko", "chinese": "zh",
}
_LANGUAGE_CODE_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")


def auto_hook(transcript: str, max_words: int = _AUTO_HOOK_WORDS) -> str:
    """The one-line hook, for providers that do not summarize."""
    words = re.sub(r"\s+", " ", str(transcript or "")).strip().split(" ")
    snippet = " ".join(word for word in words[:max_words] if word).strip()
    if not snippet:
        return "[auto] (no speech detected)"
    suffix = "…" if len([w for w in words if w]) > max_words else ""
    return f"[auto] {snippet}{suffix}"[:500]


def _language_to_code(value: Any, transcript: str) -> str:
    label = str(value or "").strip()
    mapped = _WHISPER_LANGUAGE_CODES.get(label.lower())
    if mapped:
        return mapped
    if _LANGUAGE_CODE_RE.fullmatch(label):
        parts = label.split("-")
        base = parts[0].lower()
        # Keep script subtags (hi-Latn); drop region subtags (hi-IN).
        if len(parts) == 2 and len(parts[1]) == 4 and parts[1].isalpha():
            return f"{base}-{parts[1].title()}"
        return base
    return str((detect_text_language(transcript) or {}).get("code") or "und")


def _base_iso(language_hint: Any) -> str | None:
    """Base ISO-639-1 code for a provider hint (hi-IN -> hi, hi-Latn -> hi)."""
    code = normalize_language_code(language_hint)
    if not code or code == "und":
        return None
    return code.split("-", 1)[0].strip().lower() or None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_transcript(transcript: str, duration: Any = None) -> bool:
    """Guard (c). False when a transcript looks like an ASR hallucination.

    Rejects: an impossible speaking rate (>6 words/sec, or <0.3 words/sec on a
    clip longer than 10s), a token repeated >10x in a row, or a single 3/4/5-gram
    covering more than 40% of the words (a loop). Empty text is accepted here —
    emptiness is the no-speech path's business, not this one's.
    """
    text = str(transcript or "").strip()
    if not text:
        return True
    words = text.split()
    count = len(words)
    duration_seconds = _as_float(duration, 0.0)
    if duration_seconds > 0 and count:
        words_per_second = count / duration_seconds
        if words_per_second > 6.0:
            return False
        if duration_seconds > 10 and words_per_second < 0.3:
            return False

    run = 1
    for index in range(1, count):
        if words[index].lower() == words[index - 1].lower():
            run += 1
            if run > 10:
                return False
        else:
            run = 1

    for gram in (3, 4, 5):
        if count < gram * 2:
            continue
        counts: dict[str, int] = {}
        for index in range(count - gram + 1):
            key = " ".join(word.lower() for word in words[index:index + gram])
            counts[key] = counts.get(key, 0) + 1
        top = max(counts.values())
        # Only a REPEATED n-gram signals a loop; all-distinct is a short clip.
        if top >= 2 and (top * gram) / count > 0.4:
            return False
    return True


def _duration_hint(value: Any) -> str | None:
    seconds = _as_float(value, 0.0)
    return f"{seconds:.0f}s" if seconds > 0 else None


def _safe_unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                   # noqa: BLE001 - optional
        return None


def _find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg") or _bundled_ffmpeg()


def ffmpeg_available() -> bool:
    """Shown in the UI: without ffmpeg the raw video is uploaded instead of a
    16 kHz FLAC track, which is slower, costlier and measurably less accurate."""
    return _find_ffmpeg() is not None


def _media_duration_seconds(ffmpeg: str, media_path: str) -> float | None:
    """Parse "Duration: HH:MM:SS.cc" out of ffmpeg -i stderr. No ffprobe needed."""
    try:
        result = subprocess.run(
            [ffmpeg, "-i", str(media_path)], capture_output=True, timeout=30,
            check=False,
        )
        match = re.search(
            rb"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or b""
        )
        if not match:
            return None
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:                                   # noqa: BLE001
        return None


def _audio_encode_args(fmt: str = "flac") -> tuple[list[str], str, str]:
    """(ffmpeg args, temp suffix, mime) for a 16 kHz mono track.

    Whisper and Sarvam are both trained at 16 kHz mono. FLAC is lossless, so no
    AAC/MP3 artefact blurs an Indic phoneme; WAV/pcm_s16le is the fallback.
    """
    if fmt == "wav":
        return ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"], ".wav", "audio/wav"
    return ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac"], ".flac", "audio/flac"


def _nonsilent_seconds(ffmpeg: str, media_path: str) -> float | None:
    duration = _media_duration_seconds(ffmpeg, media_path)
    if not duration or duration <= 0:
        return None
    try:
        result = subprocess.run(
            [ffmpeg, "-i", str(media_path), "-af",
             f"silencedetect=n={_SILENCE_NOISE_DB}:d={_SILENCE_MIN_D}",
             "-f", "null", "-"],
            capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
        )
    except Exception:                                   # noqa: BLE001 - best effort
        return None
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    silence = sum(
        float(match) for match in re.findall(r"silence_duration:\s*([\d.]+)", stderr)
    )
    return max(0.0, duration - silence)


def _is_silent_media(media_path: str) -> bool:
    """Guard (a). True only when we are CONFIDENT the clip has <~1s of speech.

    An unparseable duration returns False, so an unknown clip is transcribed
    rather than silently dropped.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False
    seconds = _nonsilent_seconds(ffmpeg, media_path)
    return seconds is not None and seconds < _MIN_SPEECH_SECONDS


def _split_audio(
    audio_path: str, chunk_seconds: int, max_chunks: int,
    *, overlap_seconds: float = _CHUNK_OVERLAP_SECONDS,
) -> list[str]:
    """Cut audio into <=chunk_seconds 16 kHz mono FLAC pieces with 1s overlap.

    Returns [] when the audio already fits, ffmpeg is missing, or any cut fails
    — the caller then sends the file whole. The in-flight chunk is tracked
    separately from `chunks` so a failed cut does not leak a partial FLAC.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return []
    duration = _media_duration_seconds(ffmpeg, audio_path)
    if not duration or duration <= chunk_seconds:
        return []
    args, suffix, _mime = _audio_encode_args("flac")
    chunks: list[str] = []
    offset = 0.0
    step = max(1.0, float(chunk_seconds) - float(overlap_seconds))
    pending: str | None = None
    try:
        while offset < duration and len(chunks) < max_chunks:
            handle = tempfile.NamedTemporaryFile(
                prefix="adspy2-chunk-", suffix=suffix, delete=False
            )
            handle.close()
            pending = handle.name
            result = subprocess.run(
                [ffmpeg, "-y", "-ss", f"{offset:.2f}", "-t", str(chunk_seconds),
                 "-i", str(audio_path), *args, handle.name],
                capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
            )
            if result.returncode != 0 or os.path.getsize(handle.name) <= 0:
                raise OSError("ffmpeg chunking failed")
            chunks.append(handle.name)
            pending = None
            offset += step
        return chunks
    except Exception:                                   # noqa: BLE001
        _safe_unlink(pending)
        for path in chunks:
            _safe_unlink(path)
        return []


def _join_overlapping(pieces: Sequence[str]) -> str:
    """Join chunk transcripts, dropping the duplicated overlap tail."""
    joined = ""
    for piece in pieces:
        piece = str(piece or "").strip()
        if not piece:
            continue
        if not joined:
            joined = piece
            continue
        previous = joined.split()
        current = piece.split()
        overlap = 0
        limit = min(len(previous), len(current), 12)
        for size in range(limit, 0, -1):
            if [w.lower() for w in previous[-size:]] == [w.lower() for w in current[:size]]:
                overlap = size
                break
        joined = " ".join(previous + current[overlap:])
    return joined.strip()


def _extract_audio(media_path: str) -> tuple[str, str] | None:
    """A 16 kHz mono FLAC track, or None when ffmpeg is missing or fails."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    for fmt in ("flac", "wav"):
        args, suffix, mime = _audio_encode_args(fmt)
        handle = tempfile.NamedTemporaryFile(
            prefix="adspy2-audio-", suffix=suffix, delete=False
        )
        handle.close()
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-i", str(media_path), *args, handle.name],
                capture_output=True, timeout=FFMPEG_TIMEOUT, check=False,
            )
            if result.returncode != 0 or os.path.getsize(handle.name) <= 0:
                raise OSError("ffmpeg extraction failed")
        except Exception:                               # noqa: BLE001 - try wav, then raw
            _safe_unlink(handle.name)
            continue
        return handle.name, mime
    return None


def _upload_media(media_path: str, mime: str) -> tuple[str, str, str | None]:
    """(upload_path, upload_mime, cleanup_path) with the audio shrink applied."""
    extracted = _extract_audio(media_path)
    if extracted:
        return extracted[0], extracted[1], extracted[0]
    return media_path, (str(mime or "").strip() or "video/mp4"), None


def _no_speech_result(model: str, duration: Any = None) -> dict[str, Any]:
    """A terminal no-speech / guarded result.

    Stored COMPLETED (not failed) with low_confidence=1, language 'und' and an
    empty transcript, so a resume never re-hallucinates it, it drops out of the
    real language folders, and clustering never pools it with anything.
    """
    return {
        "language": "und",
        "transcript": "",
        "hook": auto_hook(""),
        "duration": _duration_hint(duration),
        "model": model,
        "low_confidence": True,
    }


# ===========================================================================
# PART 3 — providers
# ===========================================================================
def _http_post_multipart(
    url: str, *, fields: dict[str, str], file_path: str, file_mime: str,
    headers: dict[str, str], timeout: int = HTTP_TIMEOUT,
) -> dict[str, Any]:
    """POST a multipart upload; return parsed JSON. The single provider choke
    point — mocked wholesale in tests, so no test can reach the network."""
    boundary = f"----adspy2-{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode("utf-8")
    filename = os.path.basename(str(file_path)) or "media.mp4"
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {file_mime}\r\n\r\n"
    ).encode("utf-8")
    body += Path(file_path).read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    request = urllib.request.Request(
        url, data=bytes(body), method="POST",
        headers={
            **headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "AdSpy2/transcription",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ProviderError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError("request timed out") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderError(f"non-JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderError("response JSON was not an object")
    return payload


class GroqProvider:
    """Groq-hosted Whisper large-v3. verbose_json carries the spoken language
    and the per-segment stats guard (b) needs."""

    name = "groq"
    model = GROQ_MODEL

    def __init__(self, api_key: str = ""):
        self.api_key = str(api_key or "").strip()

    def transcribe(
        self, media_path: str, mime: str, language_hint: str | None = None
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderConfigurationError(
                "No Groq API key configured (Settings -> API keys, or GROQ_API_KEY)."
            )
        model_label = f"groq:{self.model}"
        # Whisper takes long files, so Groq is never chunked — only shrunk to a
        # 16 kHz mono FLAC track.
        upload_path, upload_mime, cleanup = _upload_media(media_path, mime)
        try:
            if _is_silent_media(upload_path):                    # guard (a)
                return _no_speech_result(model_label)
            fields = {
                "model": self.model,
                "response_format": "verbose_json",
                # temperature 0 disables Whisper's temperature-fallback ladder,
                # the main source of looped hallucinations on quiet audio.
                "temperature": "0",
            }
            base = _base_iso(language_hint)
            if base:
                fields["language"] = base
            payload = _http_post_multipart(
                GROQ_TRANSCRIPTIONS_URL, fields=fields, file_path=upload_path,
                file_mime=upload_mime,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        except ProviderConfigurationError:
            raise
        except ProviderError as exc:
            raise ProviderError(f"Groq {exc}") from exc
        finally:
            _safe_unlink(cleanup)

        duration = payload.get("duration")
        segments = payload.get("segments")
        if isinstance(segments, list) and segments:                # guard (b)
            kept = [
                segment for segment in segments
                if isinstance(segment, dict) and not (
                    _as_float(segment.get("no_speech_prob"), 0.0) > _SEG_NO_SPEECH_PROB
                    and _as_float(segment.get("avg_logprob"), 0.0) < _SEG_AVG_LOGPROB
                )
            ]
            if not kept:
                return _no_speech_result(model_label, duration)
            transcript = " ".join(
                str(segment.get("text") or "").strip() for segment in kept
            ).strip()
        else:
            transcript = str(payload.get("text") or "").strip()

        if not transcript:
            return _no_speech_result(model_label, duration)
        if not validate_transcript(transcript, duration):          # guard (c)
            return _no_speech_result(model_label, duration)
        return {
            "language": normalize_language_code(
                _language_to_code(payload.get("language"), transcript)
            ),
            "transcript": transcript[:20000],
            "hook": auto_hook(transcript),
            "duration": _duration_hint(duration),
            "duration_seconds": _as_float(duration, 0.0) or None,
            "model": model_label,
        }


class SarvamProvider:
    """Sarvam AI saarika — Indic-specialised speech-to-text.

    Multipart `file` upload with `model` and `language_code`, authenticated with
    the `api-subscription-key` header; the response carries `transcript` and
    `language_code` ("hi-IN"). Sarvam's realtime STT rejects audio over 30s, so
    longer creatives are cut into overlapping chunks and re-joined.
    """

    name = "sarvam"
    model = SARVAM_MODEL
    CHUNK_SECONDS = 28
    MAX_CHUNKS = 12

    def __init__(self, api_key: str = ""):
        self.api_key = str(api_key or "").strip()

    def transcribe(
        self, media_path: str, mime: str, language_hint: str | None = None
    ) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderConfigurationError(
                "No Sarvam API key configured (Settings -> API keys, or SARVAM_API_KEY)."
            )
        model_label = f"sarvam:{self.model}"
        upload_path, upload_mime, cleanup = _upload_media(media_path, mime)
        base = _base_iso(language_hint)
        language_code = f"{base}-IN" if base else "unknown"
        chunk_paths: list[str] = []
        language_raw = None
        transcripts: list[str] = []
        try:
            if _is_silent_media(upload_path):                      # guard (a)
                return _no_speech_result(model_label)
            chunks = _split_audio(upload_path, self.CHUNK_SECONDS, self.MAX_CHUNKS)
            if chunks:
                chunk_paths = chunks
                _args, _suffix, chunk_mime = _audio_encode_args("flac")
                parts = [(path, chunk_mime) for path in chunks]
            else:
                parts = [(upload_path, upload_mime)]
            for part_path, part_mime in parts:
                payload = self._transcribe_one(part_path, part_mime, language_code)
                piece = str(payload.get("transcript") or "").strip()
                if piece:
                    transcripts.append(piece)
                if language_raw is None and payload.get("language_code"):
                    language_raw = payload.get("language_code")
        except ProviderConfigurationError:
            raise
        except ProviderError as exc:
            raise ProviderError(f"Sarvam {exc}") from exc
        finally:
            for path in chunk_paths:
                _safe_unlink(path)
            _safe_unlink(cleanup)

        transcript = _join_overlapping(transcripts)
        if not transcript:
            return _no_speech_result(model_label)
        if not validate_transcript(transcript):                    # guard (c)
            return _no_speech_result(model_label)
        return {
            "language": normalize_language_code(
                _language_to_code(language_raw, transcript)
            ),
            "transcript": transcript[:20000],
            "hook": auto_hook(transcript),
            "duration": None,
            "model": model_label,
        }

    def _transcribe_one(
        self, file_path: str, file_mime: str, language_code: str = "unknown"
    ) -> dict[str, Any]:
        return _http_post_multipart(
            SARVAM_STT_URL,
            fields={"model": self.model, "language_code": language_code},
            file_path=file_path, file_mime=file_mime,
            headers={"api-subscription-key": self.api_key},
        )


# Languages where Sarvam measurably beats Whisper. Same Hindi ad, live A/B:
# Whisper "बई सवी कार उनर्स ... नापतोर" vs Sarvam "भाई सभी कार ओनर्स ... नापतोल".
INDIC_LANGUAGES = frozenset({
    "hi", "hi-Latn", "mr", "bn", "ta", "te", "kn", "ml", "gu", "pa", "or",
    "as", "ur", "ne", "sa",
})

DEFAULT_PROVIDER = "groq"


def preferred_provider(
    configured: str, language_hint: Any, keys: dict[str, str]
) -> str:
    """Route Indic audio to Sarvam when a Sarvam key exists; else keep the setting.

    Only ever *upgrades* a Groq setting — an explicit 'sarvam' choice is
    respected as-is, and a non-Indic or unknown hint keeps the setting. The
    caller passes `keys` rather than the module reading them, so routing is a
    pure function and testable without touching the settings table.
    """
    configured = str(configured or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    if configured not in PROVIDER_NAMES:
        # v1's Gemini path is not ported (module docstring); a stale setting
        # falls back to the default rather than failing every creative.
        configured = DEFAULT_PROVIDER
    if configured != "groq":
        return configured
    if not str(keys.get("sarvam_api_key") or "").strip():
        return configured
    return "sarvam" if normalize_language_code(language_hint) in INDIC_LANGUAGES else configured


def runnable_provider(name: str, keys: dict[str, str]) -> str:
    """The provider that can actually run with the keys on hand.

    ``preferred_provider`` is routing policy over a possibly partial key dict;
    this is the last step before a call, over the REAL key set. A Sarvam-only
    install used to store every English-hinted ad as `failed`, because the
    'groq' default had no key and nothing looked at the key it did have. If the
    chosen provider has no key and the other one does, use the other one; if
    neither has one the provider raises ProviderConfigurationError as before.
    """
    chosen = str(name or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    has_groq = bool(str(keys.get("groq_api_key") or "").strip())
    has_sarvam = bool(str(keys.get("sarvam_api_key") or "").strip())
    if chosen == "groq" and not has_groq and has_sarvam:
        return "sarvam"
    if chosen == "sarvam" and not has_sarvam and has_groq:
        return "groq"
    return chosen


PROVIDER_NAMES = frozenset({"groq", "sarvam"})
PROVIDER_SETTING = "transcription_provider"


def configured_provider(conn: sqlite3.Connection | None = None) -> str:
    """The `transcription_provider` setting, reduced to a provider this module
    can run. Anything else (v1's 'gemini', an empty row) is the default."""
    connection = conn if conn is not None else db.get_db()
    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (PROVIDER_SETTING,)
    ).fetchone()
    value = str(row["value"] if row is not None else "").strip().lower()
    return value if value in PROVIDER_NAMES else DEFAULT_PROVIDER


def get_provider(name: str, keys: dict[str, str] | None = None):
    """Resolve a provider by name. Keys come from the settings table, with an
    environment fallback — never from a request, never from a template."""
    normalized = str(name or DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    keys = keys or {}
    if normalized == "groq":
        return GroqProvider(api_key=str(keys.get("groq_api_key") or ""))
    if normalized == "sarvam":
        return SarvamProvider(api_key=str(keys.get("sarvam_api_key") or ""))
    raise ProviderConfigurationError(f"Unknown transcription provider: {name!r}")


# ===========================================================================
# PART 4 — API keys
# Read here and nowhere else. Masked when shown, redacted before logging.
# ===========================================================================
KEY_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("groq_api_key", "Groq", "GROQ_API_KEY"),
    ("sarvam_api_key", "Sarvam", "SARVAM_API_KEY"),
)


def api_keys(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """The raw keys. The ONLY function that returns them unmasked; nothing else
    in this module — and nothing outside it — may put the return value into a
    log line, a template, a run row or an HTTP response."""
    connection = conn if conn is not None else db.get_db()
    keys: dict[str, str] = {}
    for setting_key, _label, env_name in KEY_SETTINGS:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = ?", (setting_key,)
        ).fetchone()
        stored = str(row["value"] if row is not None else "").strip()
        keys[setting_key] = stored or os.environ.get(env_name, "").strip()
    return keys


def _redact(text: Any, keys: dict[str, str] | None = None) -> str:
    """Strip anything key-shaped out of a message before it is stored or logged.

    Provider error bodies quote the request, and a stored `error` column is
    read back into a template. Both the configured keys and any long
    `sk-`/`gsk_`-style token are replaced.
    """
    value = str(text or "")
    for secret in (keys or {}).values():
        secret = str(secret or "").strip()
        if len(secret) >= 8:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"\b(?:sk|gsk|sk-proj)[-_][A-Za-z0-9_\-]{12,}", "[redacted]", value)
    value = re.sub(
        r"(?i)\b(api[-_ ]?key|authorization|bearer|subscription[-_ ]?key)\b\s*[:=]?\s*\S+",
        r"\1 [redacted]",
        value,
    )
    return value[:500]


def provider_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """What the "Generate scripts" button needs to know, with no secret in it.

    When nothing is configured the button is disabled and `reason` is the
    sentence shown on it — the owner's rule: explain, never fail silently.
    """
    connection = conn if conn is not None else db.get_db()
    keys = api_keys(connection)
    groq = bool(keys.get("groq_api_key"))
    sarvam = bool(keys.get("sarvam_api_key"))
    configured = [label for key, label, _env in KEY_SETTINGS if keys.get(key)]
    setting = configured_provider(connection)
    if groq and sarvam:
        routing = (
            "Every ad goes to Sarvam." if setting == "sarvam" else
            "Indic ads go to Sarvam; everything else to Whisper large-v3."
        )
    elif sarvam:
        routing = "Every ad goes to Sarvam (the only key configured)."
    elif groq:
        routing = (
            "Every ad goes to Whisper large-v3. Add a Sarvam key to route Hindi "
            "and the other Indic languages to Sarvam, which transcribes them "
            "measurably better."
        )
    else:
        routing = ""
    raw_setting_row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (PROVIDER_SETTING,)
    ).fetchone()
    raw_setting = str(raw_setting_row["value"] if raw_setting_row is not None else "").strip().lower()
    note = ""
    if raw_setting and raw_setting not in PROVIDER_NAMES:
        note = (
            f"Settings still name '{raw_setting}' as the default provider; that "
            "path is not part of v2 (docs/03-architecture.md §5), so runs use "
            "Whisper large-v3 with Sarvam routing instead."
        )
    return {
        "ready": groq or sarvam,
        "groq": groq,
        "sarvam": sarvam,
        "configured": configured,
        "provider": setting,
        "routing": routing,
        "note": note,
        "ffmpeg": ffmpeg_available(),
        "refresh": media_refresh.refresh_status(),
        "reason": "" if (groq or sarvam) else (
            "No transcription API key is configured. Add a Groq or Sarvam key "
            "under Settings -> API keys and this button turns on."
        ),
        "model": GROQ_MODEL,
    }


# ===========================================================================
# PART 5 — media
# ===========================================================================
def media_url_hash(url: Any) -> str:
    """Dedupe layer 1: sha256 of the URL's PATH — no host, no query.

    fbcdn re-signs the same file with a fresh `oe=`/`_nc_ohc=` every few hours,
    so the query string is a timestamp, not an identity. And the HOST rotates
    between re-scans of the same ad (`video.fjai2-4.fna.fbcdn.net` today,
    `video.fbom19-1…` tomorrow — 1,081 of 1,084 multi-version ads on the owner's
    database changed host and kept the path). Hashing host+path therefore
    queued the same video again on every re-scan; the path alone is the file.

    A URL with no path at all (never seen from fbcdn, but a hash must not be
    empty) falls back to the whole URL minus its query.
    """
    text = str(url or "").strip()
    try:
        path = urllib.parse.urlsplit(text).path.strip()
    except ValueError:
        path = ""
    base = path if path and path != "/" else text.split("?", 1)[0]
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def legacy_media_url_hash(url: Any) -> str:
    """The pre-006 identity (URL minus query, host included). Only used to
    MATCH rows written before the path-only hash; never written any more."""
    base = str(url or "").split("?", 1)[0].strip()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _media_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


_VIDEO_HINT_RE = re.compile(r"\.(mp4|m4v|mov|webm)(\?|$)|video", re.I)
_IMAGE_HINT_RE = re.compile(r"\.(jpe?g|png|gif|webp)(\?|$)", re.I)


def looks_like_video(url: Any) -> bool:
    text = str(url or "")
    if not text or _IMAGE_HINT_RE.search(text):
        return False
    return bool(_VIDEO_HINT_RE.search(text))


def pick_video_url(media_urls: Any, media_type: Any = "video") -> str:
    """The video URL out of an ad's media list, or ''.

    Prefers an obvious video URL. For an ad the extractor CALLED a video the
    first entry is the video even without an extension (fbcdn does not always
    carry one); for an `unknown`/`image` ad nothing is guessed — an
    `unknown` ad whose first URL is a `.jpg` used to be downloaded and
    uploaded to Whisper as audio (6,658 such ads on the owner's database).
    """
    urls = _media_list(media_urls)
    for url in urls:
        if looks_like_video(url):
            return url
    if str(media_type or "").strip().lower() == "video" and urls:
        return "" if _IMAGE_HINT_RE.search(urls[0]) else urls[0]
    return ""


def pick_poster_url(media_urls: Any) -> str:
    """The poster/thumbnail entry the extractor stored after the video, if any.
    Signed like the video, so it expires the same way — a fallback only."""
    urls = _media_list(media_urls)
    for url in urls[1:]:
        if _IMAGE_HINT_RE.search(url) or not looks_like_video(url):
            return url
    return ""


def _http_get(url: str, timeout: int = MEDIA_TIMEOUT) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url, method="GET",
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MEDIA_MAX_BYTES + 1)
        if len(data) > MEDIA_MAX_BYTES:
            raise ProviderError("media larger than the 120 MB cap")
        mime = response.headers.get("Content-Type", "video/mp4").split(";")[0].strip()
    return data, mime or "video/mp4"


def fetch_media(url: str) -> tuple[bytes, str]:
    """Download ONE URL, once. Every failure is a ProviderError whose message
    starts with `media`; an HTTP failure keeps its status code in the message
    (`media HTTP 403`) because ``_fetch_with_refresh`` reads it.

    There is deliberately no retry in here. The old "drop the signature query
    and try the bare path" retry never once succeeded — an unsigned fbcdn path
    answers 403 like the expired one did — so it only doubled the time a
    hundred dead links took to fail. Re-signing lives in ``_fetch_with_refresh``.
    """
    target = str(url or "").strip()
    if not target:
        raise ProviderError("no media URL")
    try:
        return _http_get(target)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"media HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"media network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError("media download timed out") from exc


_EXPIRY_HTTP_RE = re.compile(r"\bmedia HTTP (403|404|410)\b")

# Seam for tests: monkeypatch `transcription.refresh_video_url` and nothing
# ever launches a browser.
refresh_video_url = media_refresh.refresh_video_url


def _refresh_reason_text(reason: str) -> str:
    if reason == "login_wall":
        return ("Facebook demanded a login on the public Ad Library page; the "
                "refresh stopped rather than retry. Re-scan the page instead.")
    if reason == "no_video":
        return "the Ad Library page shows no video for this ad (image-only ad?)"
    if reason == "playwright_unavailable":
        return media_refresh.refresh_status()["reason"]
    if reason == "missing_library_id":
        return "no Ad Library id is stored for this creative, so its link cannot be refreshed"
    return reason or "unknown"


def _fetch_with_refresh(
    conn: sqlite3.Connection, transcript_id: int, url: str, library_id: str,
    ad_ids: Sequence[int],
) -> tuple[bytes, str]:
    """``fetch_media`` plus v1's four refresh rules, ported exactly:

    1. no stored URL           -> refresh first; `no_video` is terminal (image ad)
    2. stored URL past its oe= -> refresh BEFORE the first download
    3. 403/404/410 on a signed URL -> exactly ONE refresh, then one retry
    4. a refreshed URL is written back onto the transcript row AND onto the
       ad's own media_urls[0], so the next run — and the drawer preview —
       start from a live link. The identity (path hash) does not change.

    When Playwright is missing the rules degrade to a plain download, and the
    error the owner sees says WHY it failed and what to do about it.
    """
    target = str(url or "").strip()
    can_refresh = media_refresh.playwright_available()

    def refresh(context: str) -> str:
        outcome = refresh_video_url(library_id)
        fresh = str(outcome.get("url") or "").strip()
        if not fresh:
            reason = str(outcome.get("error") or "unknown")
            raise ProviderError(f"media refresh failed ({context}): {_refresh_reason_text(reason)}")
        _store_refreshed_url(conn, transcript_id, fresh, outcome.get("thumbnail"), ad_ids)
        return fresh

    refreshed = False
    if not target:
        if not can_refresh:
            raise ProviderError(
                "no media URL stored for this creative and "
                + _refresh_reason_text("playwright_unavailable")
            )
        target = refresh("no stored link")
        refreshed = True
    elif is_url_expired(target) and can_refresh:
        target = refresh("expired link")
        refreshed = True

    try:
        return fetch_media(target)
    except ProviderError as exc:
        message = str(exc)
        if refreshed or not _EXPIRY_HTTP_RE.search(message):
            raise
        if not has_signed_expiry(target):
            raise
        if not can_refresh:
            raise ProviderError(
                f"{message}: the signed fbcdn link has expired. "
                + _refresh_reason_text("playwright_unavailable")
            ) from exc
        fresh = refresh(f"after {message}")
        return fetch_media(fresh)


def _store_refreshed_url(
    conn: sqlite3.Connection, transcript_id: int | None, fresh_url: str,
    thumbnail: Any, ad_ids: Sequence[int],
) -> None:
    """Rule 4: the fresh link goes onto the transcript row and onto each linked
    ad's media_urls[0] (poster kept/replaced at [1]). The hash is untouched —
    the same video, newly signed."""
    now = utc_now()
    with _txn(conn):
        if transcript_id is not None:
            conn.execute(
                "UPDATE transcripts SET media_url=?, refreshed_at=?, updated_at=? WHERE id=?",
                (fresh_url, now, now, int(transcript_id)),
            )
        for ad_id in ad_ids:
            row = conn.execute(
                "SELECT media_urls FROM ads WHERE id = ?", (int(ad_id),)
            ).fetchone()
            if row is None:
                continue
            urls = _media_list(row["media_urls"])
            poster = str(thumbnail or "").strip() or pick_poster_url(row["media_urls"])
            rebuilt = [fresh_url]
            if poster:
                rebuilt.append(poster)
            rebuilt += [u for u in urls[2:] if u not in rebuilt]
            conn.execute(
                "UPDATE ads SET media_urls=?, updated_at=? WHERE id=?",
                (json.dumps(rebuilt), now, int(ad_id)),
            )


# --- posters ---------------------------------------------------------------
def media_dir(conn: sqlite3.Connection | None = None) -> Path:
    """`<db folder>/media` — next to whichever dataset file is open, so tests
    (temp DB) never write into the repo and the two datasets keep their own."""
    connection = conn if conn is not None else db.get_db()
    try:
        path = str(connection.execute("PRAGMA database_list").fetchone()[2] or "")
    except sqlite3.Error:                               # pragma: no cover
        path = ""
    base = Path(path).resolve().parent if path else app_config.DATA_DIR
    return base / "media"


def thumbs_dir(conn: sqlite3.Connection | None = None) -> Path:
    return media_dir(conn) / "thumbs"


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def thumb_url_for(content_sha256: Any, thumb_path: Any) -> str:
    """URL of the local poster, or '' when none was made."""
    sha = str(content_sha256 or "").strip().lower()
    if not thumb_path or not _SHA256_RE.fullmatch(sha):
        return ""
    return f"/media/thumbs/{sha}.jpg"


def _make_thumbnail(media_path: str, content_sha256: str, folder: Path) -> str | None:
    """One 320px poster frame per content hash, taken at t=1s. Silent on any
    failure (no ffmpeg, an audio-only file, a corrupt download) — the table
    just shows no picture."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg or not _SHA256_RE.fullmatch(str(content_sha256 or "")):
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{content_sha256}.jpg"
        if target.is_file() and target.stat().st_size > 0:
            return str(target)
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", "1", "-i", str(media_path),
             "-frames:v", "1", "-vf", "scale=320:-2", str(target)],
            capture_output=True, timeout=30, check=False,
        )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
            _safe_unlink(str(target))
            return None
        return str(target)
    except Exception:                                   # noqa: BLE001 - best effort
        return None


def _parse_duration_seconds(value: Any) -> float | None:
    """Provider `duration` comes back as a float (Groq), a '19s' hint string
    (our own `_duration_hint`), or None (Sarvam)."""
    if value is None:
        return None
    text = str(value).strip().lower().rstrip("s").strip()
    seconds = _as_float(text, 0.0)
    return seconds if seconds > 0 else None


def duration_label(seconds: Any) -> str:
    total = _as_float(seconds, 0.0)
    if total <= 0:
        return ""
    whole = int(round(total))
    return f"{whole // 60}:{whole % 60:02d}"


# --- product-level link refresh (free; no provider call) --------------------
# One job per product at a time, tracked in memory: the panel polls it the way
# it polls a run. Runs are for money; this spends none, so it gets no receipt
# row — but it does write the fresh links, which is the point.
_refresh_jobs: dict[int, dict[str, Any]] = {}
_refresh_lock = threading.Lock()


def refresh_job(product_id: int) -> dict[str, Any] | None:
    with _refresh_lock:
        job = _refresh_jobs.get(int(product_id))
        return dict(job) if job else None


def refresh_product_media(
    conn: sqlite3.Connection, product_id: int, *, limit: int = 200,
) -> dict[str, Any]:
    """Re-sign every expired video link this product's next run would need.

    Inline (the caller decides about threads). Touches: `ads.media_urls[0]`
    for each refreshed ad and `media_url` on any pending transcript row for
    that creative. Skips creatives that are already transcribed — there is
    nothing to download for them. Stops on the first `login_wall`.
    """
    job = {
        "product_id": int(product_id), "status": "running", "total": 0,
        "done": 0, "refreshed": 0, "failed": 0, "skipped": 0,
        "started_at": utc_now(), "finished_at": None, "error": "",
        "last_reason": "",
    }
    with _refresh_lock:
        _refresh_jobs[int(product_id)] = job

    def update(**fields: Any) -> None:
        with _refresh_lock:
            job.update(fields)

    try:
        if not media_refresh.playwright_available():
            update(status="failed", error=media_refresh.refresh_status()["reason"],
                   finished_at=utc_now())
            return refresh_job(product_id) or job

        _backfill_path_hashes(conn)
        targets: list[dict[str, Any]] = []
        by_hash: dict[str, dict[str, Any]] = {}
        for candidate in _candidates(conn, product_id, None):
            url = str(candidate["source_url"] or "")
            if candidate["already_linked"] or not url:
                continue
            group = by_hash.get(candidate["url_hash"])
            if group is not None:
                # Every ad sharing the creative gets the fresh link, not just
                # the first one the query happened to return.
                group["ad_ids"].append(int(candidate["ad_id"]))
                if not group["library_id"] and candidate["library_id"]:
                    group["library_id"] = str(candidate["library_id"])
                continue
            existing = _existing_transcript(conn, url)
            if existing is not None and str(existing["status"]) == "completed":
                continue
            if not is_url_expired(url):
                continue
            group = {
                "ad_ids": [int(candidate["ad_id"])],
                "library_id": str(candidate["library_id"] or ""),
                "transcript_id": int(existing["id"]) if existing is not None else None,
                "url_hash": candidate["url_hash"],
            }
            by_hash[candidate["url_hash"]] = group
            targets.append(group)
            if len(targets) >= limit:
                break
        targets = [t for t in targets if t["library_id"]]
        update(total=len(targets))

        for target in targets:
            outcome = refresh_video_url(target["library_id"])
            fresh = str(outcome.get("url") or "").strip()
            if fresh:
                ad_ids = list(target["ad_ids"])
                if target["transcript_id"] is not None:
                    ad_ids = sorted(set(ad_ids) | set(transcript_ad_ids(conn, target["transcript_id"])))
                _store_refreshed_url(
                    conn, target["transcript_id"], fresh, outcome.get("thumbnail"), ad_ids
                )
                update(done=job["done"] + 1, refreshed=job["refreshed"] + 1)
                continue
            reason = str(outcome.get("error") or "unknown")
            update(done=job["done"] + 1, failed=job["failed"] + 1,
                   last_reason=_refresh_reason_text(reason))
            if reason == "login_wall":
                update(status="failed", error=_refresh_reason_text(reason),
                       finished_at=utc_now())
                return refresh_job(product_id) or job
        update(status="completed", finished_at=utc_now())
    except Exception as exc:                            # noqa: BLE001
        log.exception("media refresh for product %s crashed", product_id)
        update(status="failed", error=_redact(exc), finished_at=utc_now())
    return refresh_job(product_id) or job


def start_media_refresh(
    product_id: int, *, conn: sqlite3.Connection | None = None,
    background: bool = True,
) -> dict[str, Any]:
    """The "Refresh links" button. Free — no provider is called."""
    connection = conn if conn is not None else db.get_db()
    status = media_refresh.refresh_status()
    if not status["available"]:
        return {"ok": False, "reason": "unavailable", "message": status["reason"]}
    current = refresh_job(product_id)
    if current and current.get("status") == "running":
        return {"ok": False, "reason": "already_running", "job": current}
    if not background:
        return {"ok": True, "job": refresh_product_media(connection, product_id)}

    path = str(connection.execute("PRAGMA database_list").fetchone()[2] or app_config.db_path())
    with _refresh_lock:
        _refresh_jobs[int(product_id)] = {
            "product_id": int(product_id), "status": "running", "total": 0,
            "done": 0, "refreshed": 0, "failed": 0, "skipped": 0,
            "started_at": utc_now(), "finished_at": None, "error": "", "last_reason": "",
        }
    thread = threading.Thread(
        target=_refresh_in_thread, args=(path, int(product_id)),
        name=f"adspy2-media-refresh-{product_id}", daemon=True,
    )
    thread.start()
    return {"ok": True, "job": refresh_job(product_id)}


def _refresh_in_thread(database_path: str, product_id: int) -> None:
    connection = db.connect(database_path)
    try:
        refresh_product_media(connection, product_id)
    finally:
        connection.close()


# ===========================================================================
# PART 6 — queueing, dedupe and linking
# ===========================================================================
@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE on an explicit connection. Mirrors db.transaction(), but
    the background run worker owns its own handle and has no Flask context."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:                           # pragma: no cover
            pass
        raise
    else:
        conn.execute("COMMIT")


def normalize_language_filter(languages: Any) -> set[str] | None:
    """None / 'all' / '' -> no filter. 'unknown' aliases the internal 'und'."""
    if languages is None:
        return None
    if isinstance(languages, str):
        if languages.strip().lower() in {"", "all", "*"}:
            return None
        languages = [part for part in languages.split(",") if part.strip()]
    if not isinstance(languages, (list, tuple, set)):
        return None
    normalized: set[str] = set()
    for value in languages:
        code = str(value or "").strip().lower()
        if not code:
            continue
        if code == "all":
            return None
        normalized.add("und" if code == "unknown" else code)
    return normalized or None


def apply_media_language(
    conn: sqlite3.Connection, ad_id: int, language: str, now: str
) -> None:
    """Write the transcript's language onto the ad. Media evidence is the
    strongest signal there is, so it becomes `final_language` outright."""
    conn.execute(
        """
        INSERT INTO ad_languages(ad_id, media_language, final_language, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(ad_id) DO UPDATE SET
            media_language = excluded.media_language,
            final_language = excluded.media_language,
            updated_at     = excluded.updated_at
        """,
        (int(ad_id), str(language), str(language), now),
    )


def link_transcript_ad(
    conn: sqlite3.Connection, transcript_id: int, ad_id: int,
    link_source: str, now: str,
) -> bool:
    """Record that this ad uses this transcript's creative. True when new."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO transcript_ads(transcript_id, ad_id, link_source, created_at)
        VALUES(?,?,?,?)
        """,
        (int(transcript_id), int(ad_id), str(link_source), now),
    )
    return bool(cursor.rowcount)


def language_hint_for_ad(conn: sqlite3.Connection, ad_id: int) -> str:
    """The per-ad decoding hint handed to the provider.

    The ad's reconciled final_language, falling back to its detected text
    language, falling back to 'hi' — this is an India-market tool and an
    unhinted Whisper call on Hindi audio is exactly the case Sarvam was added
    for.
    """
    row = conn.execute(
        "SELECT final_language, text_language FROM ad_languages WHERE ad_id = ?",
        (int(ad_id),),
    ).fetchone()
    code = ""
    if row is not None:
        code = str(row["final_language"] or row["text_language"] or "").strip()
    if not code:
        # Nothing recorded yet — read the ad's own copy. Free, offline, and it
        # is what puts a Marathi ad on Sarvam's Marathi decoder instead of the
        # blanket 'hi' default. ad_languages has 0 rows today, so this is the
        # branch that actually fires.
        detected = conn.execute(
            "SELECT ad_text, headline, description FROM ads WHERE id = ?",
            (int(ad_id),),
        ).fetchone()
        if detected is not None:
            blob = "\n".join(
                str(detected[key] or "")
                for key in ("headline", "ad_text", "description")
            )
            code = str(detect_text_language(blob).get("code") or "")
    code = normalize_language_code(code) if code else "und"
    return code if code and code != "und" else "hi"


def detect_ad_text_language(
    conn: sqlite3.Connection, ad_id: int, now: str | None = None
) -> str:
    """Fill `ad_languages` from the ad's own copy. Offline; costs nothing.

    v1's `backfill_text_languages`. `ad_languages` has 0 rows today, which is
    why the Products screen's language chips are entirely v1 inheritance — this
    is what starts filling them from v2's own data.
    """
    row = conn.execute(
        "SELECT ad_text, headline, description FROM ads WHERE id = ?", (int(ad_id),)
    ).fetchone()
    if row is None:
        return "und"
    blob = "\n".join(
        str(row[key] or "") for key in ("headline", "ad_text", "description")
    )
    detected = detect_text_language(blob)
    code = normalize_language_code(detected.get("code"))
    stamp = now or utc_now()
    conn.execute(
        """
        INSERT INTO ad_languages(ad_id, text_language, text_confidence, text_method,
                                 final_language, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(ad_id) DO UPDATE SET
            text_language   = excluded.text_language,
            text_confidence = excluded.text_confidence,
            text_method     = excluded.text_method,
            -- media evidence, once it exists, always outranks text evidence
            final_language  = CASE
                WHEN COALESCE(ad_languages.media_language,'') <> ''
                THEN ad_languages.media_language ELSE excluded.text_language END,
            updated_at      = excluded.updated_at
        """,
        (int(ad_id), code, float(detected.get("confidence") or 0.0),
         str(detected.get("method") or ""), code, stamp),
    )
    return code


def backfill_text_languages(
    conn: sqlite3.Connection, limit: int = 5000, *, product_id: int | None = None,
) -> int:
    """Detect the text language of every ad that has none yet. Free, offline.

    `product_id` narrows it to one product — the "Detect languages" button on
    the transcription panel, which is how the language checkboxes stop reading
    "Unknown" for every ad that has no transcript yet. Called ONLY from a POST:
    read paths never write, and a run never does this implicitly either (the
    provider hint already reads the ad's copy live, see language_hint_for_ad).
    """
    params: list[Any] = []
    sql = """
        SELECT DISTINCT a.id AS id FROM ads a
        LEFT JOIN ad_languages al ON al.ad_id = a.id
    """
    if product_id is not None:
        sql += " JOIN ad_products ap ON ap.ad_id = a.id AND ap.product_id = ?"
        params.append(int(product_id))
    sql += " WHERE al.ad_id IS NULL ORDER BY a.id LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    now = utc_now()
    with _txn(conn):
        for row in rows:
            detect_ad_text_language(conn, int(row["id"]), now)
    return len(rows)


def _find_content_duplicate(
    conn: sqlite3.Connection, content_sha256: str, exclude_id: int
) -> dict[str, Any] | None:
    """Dedupe layer 2: a COMPLETED transcript whose downloaded bytes matched.

    THE PROVENANCE RULE: a row whose model says `dedupe:` is a COPY, and a copy
    is never a valid source. Without this a bad transcript launders itself
    through its own copies and every accuracy guard above is quietly undone.
    Copies always have a higher id than their original, so ORDER BY id finds
    the original first.
    """
    if not content_sha256:
        return None
    rows = conn.execute(
        """
        SELECT id, language, text, hook_summary, model, low_confidence,
               duration_seconds, provider_language, thumb_path
        FROM transcripts
        WHERE content_sha256 = ? AND status = 'completed'
          AND language IS NOT NULL AND text IS NOT NULL
        ORDER BY id
        """,
        (str(content_sha256),),
    ).fetchall()
    for row in rows:
        if int(row["id"]) == int(exclude_id):
            continue
        if str(row["model"] or "").startswith("dedupe:"):
            continue
        return {
            "id": int(row["id"]),
            "language": str(row["language"]),
            "text": str(row["text"]),
            "hook_summary": row["hook_summary"],
            "low_confidence": int(row["low_confidence"] or 0),
            "duration_seconds": row["duration_seconds"],
            "provider_language": row["provider_language"],
            "thumb_path": row["thumb_path"],
        }
    return None


def _candidates(
    conn: sqlite3.Connection, product_id: int, language_filter: set[str] | None
) -> list[dict[str, Any]]:
    """The ads a run would consider, with the media hash that identifies each
    creative. One definition, shared by ``queue_product`` (which writes) and
    ``estimate_new`` (which only counts), so the confirm dialog can never quote
    a different number from the one the run acts on.
    """
    rows = conn.execute(
        """
        SELECT a.id AS id, a.media_urls AS media_urls, a.library_id AS library_id,
               lower(COALESCE(a.media_type,'unknown')) AS media_type,
               lower(COALESCE(NULLIF(al.final_language,''),
                              NULLIF(al.text_language,''), 'und')) AS language,
               EXISTS(
                   SELECT 1 FROM transcript_ads ta
                   JOIN transcripts t ON t.id = ta.transcript_id
                   WHERE ta.ad_id = a.id AND t.status = 'completed'
               ) AS already_linked,
               (al.ad_id IS NULL) AS language_unknown
        FROM ad_products ap
        JOIN ads a            ON a.id = ap.ad_id
        LEFT JOIN ad_languages al ON al.ad_id = a.id
        WHERE ap.product_id = ?
          AND lower(COALESCE(a.media_type,'unknown')) IN ('video','unknown','')
        ORDER BY a.last_captured_at DESC, a.id DESC
        """,
        (int(product_id),),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        language = str(row["language"] or "und")
        media_type = str(row["media_type"] or "unknown")
        source_url = pick_video_url(row["media_urls"], media_type)
        has_any_url = bool(_media_list(row["media_urls"]))
        candidates.append({
            "ad_id": int(row["id"]),
            "library_id": str(row["library_id"] or ""),
            "media_type": media_type,
            "language": language,
            "source_url": source_url,
            "url_hash": media_url_hash(source_url) if source_url else "",
            "wanted": language_filter is None or language in language_filter,
            # The ad already has a COMPLETED transcript through transcript_ads
            # — v1 linked 738 ads that way by content hash, each ad carrying
            # its own opaque fbcdn path. Without this check every one of them
            # was queued again as a "new" creative: product 68 quoted 72 to
            # send with 155 already paid for.
            "already_linked": bool(row["already_linked"]),
            # An `unknown` ad whose only URL is a picture is not a video.
            "not_video": bool(has_any_url and not source_url),
            "language_unknown": bool(row["language_unknown"]),
        })
    return candidates


def _existing_transcript(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    """The transcript row for this creative, whichever identity it was stored
    under: the path hash (new rows), the host+path hash (rows written before
    migration 006), or a media_path_hash backfilled onto an imported row.
    A completed row wins over a pending one when both exist."""
    path_hash = media_url_hash(url)
    legacy = legacy_media_url_hash(url)
    try:
        path = urllib.parse.urlsplit(str(url or "")).path.strip()
    except ValueError:
        path = ""
    # The third clause is for pre-006 rows not yet backfilled (media_path_hash
    # NULL): the same file under a rotated host has a different legacy hash but
    # the same path inside its stored media_url. `_backfill_path_hashes` runs
    # on every write path, so this clause matches nothing once it has run.
    return conn.execute(
        """
        SELECT id, status, language FROM transcripts
        WHERE media_path_hash = ? OR media_url_hash IN (?, ?)
           OR (media_path_hash IS NULL AND ? <> '' AND media_url IS NOT NULL
               AND instr(media_url, ?) > 0)
        ORDER BY (status = 'completed') DESC, id
        LIMIT 1
        """,
        (path_hash, path_hash, legacy, path if len(path) > 8 else "", path),
    ).fetchone()


def _backfill_path_hashes(conn: sqlite3.Connection, limit: int = 5000) -> int:
    """Give pre-006 rows their path hash so lookups hit the index. Runs on write
    paths only (queue_product, refresh_product_media); idempotent; a no-op once
    every row that has a media_url carries a media_path_hash."""
    rows = conn.execute(
        """
        SELECT id, media_url FROM transcripts
        WHERE media_path_hash IS NULL AND media_url IS NOT NULL AND media_url <> ''
        ORDER BY id LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    if not rows:
        return 0
    with _txn(conn):
        for row in rows:
            conn.execute(
                "UPDATE transcripts SET media_path_hash = ? WHERE id = ?",
                (media_url_hash(row["media_url"]), int(row["id"])),
            )
    return len(rows)


def estimate_new(
    conn: sqlite3.Connection, product_id: int, *, languages: Any = None
) -> int:
    """How many creatives a run would actually SEND to a provider. Writes nothing.

    This is the number on the confirm dialog, and it is deliberately not
    "video ads": ads already transcribed (by hash OR by an existing link), ads
    sharing a creative with each other, and ads with no stored video all drop
    out. Quoting anything larger would overstate the bill.
    """
    language_filter = normalize_language_filter(languages)
    seen: set[str] = set()
    count = 0
    for candidate in _candidates(conn, product_id, language_filter):
        if not candidate["wanted"] or not candidate["url_hash"]:
            continue
        if candidate["already_linked"]:
            continue
        if candidate["url_hash"] in seen:
            continue
        seen.add(candidate["url_hash"])
        row = _existing_transcript(conn, candidate["source_url"])
        if row is not None and str(row["status"]) in {"completed", "processing"}:
            continue
        count += 1
    return count


def media_state(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    """The pre-flight numbers the panel prints before anything is pressed.

    Reads only. `to_send` is estimate_new's number; `expired` is how many of
    those creatives carry a video link whose fbcdn signature has already
    lapsed — on the owner's database that is all of them, which is why the
    panel offers "Refresh links" (Playwright) or "Re-scan pages" (extension)
    before the paid button.
    """
    state = {
        "candidates": 0, "to_send": 0, "already_done": 0, "no_media": 0,
        "not_video": 0, "expired": 0, "fresh": 0, "unsigned": 0,
        "pending": 0, "failed": 0, "unknown_language_ads": 0,
    }
    seen: set[str] = set()
    for candidate in _candidates(conn, product_id, None):
        state["candidates"] += 1
        if candidate["language_unknown"] and (candidate["source_url"] or candidate["already_linked"]):
            state["unknown_language_ads"] += 1
        if candidate["already_linked"]:
            state["already_done"] += 1
            continue
        url = str(candidate["source_url"] or "")
        if not url:
            if candidate["not_video"]:
                state["not_video"] += 1
            else:
                state["no_media"] += 1
            continue
        if candidate["url_hash"] in seen:
            continue
        seen.add(candidate["url_hash"])
        existing = _existing_transcript(conn, url)
        status = str(existing["status"]) if existing is not None else ""
        if status == "completed":
            state["already_done"] += 1
            continue
        if status == "pending":
            state["pending"] += 1
        elif status == "failed":
            state["failed"] += 1
        elif status == "processing":
            continue
        state["to_send"] += 1
        if is_url_expired(url):
            state["expired"] += 1
        elif has_signed_expiry(url):
            state["fresh"] += 1
        else:
            state["unsigned"] += 1
    state["refresh_job"] = refresh_job(product_id)
    return state


def queue_product(
    conn: sqlite3.Connection, product_id: int, *, languages: Any = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Create pending transcript rows for a product's video ads. SPENDS NOTHING.

    Deliberately separate from ``process_run``: this half is free and idempotent,
    that half costs money. Keeping them apart is what makes "transcription never
    runs implicitly" a property a test can assert rather than a promise.

    An ad whose creative is already transcribed — same path hash, or an
    existing completed link — is not re-queued. A hash match links the ad to
    the existing transcript and hands it the language on the spot, which is how
    one video's cost is amortised over the forty ads that share it.
    """
    limit = max(1, min(int(limit or 200), 1000))
    language_filter = normalize_language_filter(languages)
    _backfill_path_hashes(conn)
    ads = _candidates(conn, product_id, language_filter)

    queued_by_language: dict[str, int] = {}
    result = {
        "queued": 0, "already_done": 0, "linked": 0, "pending": 0,
        "failed_previously": 0, "no_media": 0, "not_video": 0,
        "skipped_other_language": 0, "candidates": len(ads),
    }
    now = utc_now()
    with _txn(conn):
        for ad in ads:
            if result["queued"] >= limit:
                break
            ad_id = int(ad["ad_id"])
            language = str(ad["language"] or "und")
            if not ad["wanted"]:
                result["skipped_other_language"] += 1
                continue
            if ad["already_linked"]:
                result["already_done"] += 1
                continue
            source_url = str(ad["source_url"] or "")
            if not source_url:
                # Counted, never guessed: no stored media, or a picture only.
                result["not_video" if ad["not_video"] else "no_media"] += 1
                continue
            url_hash = str(ad["url_hash"])
            existing = _existing_transcript(conn, source_url)
            if existing is not None:
                status = str(existing["status"])
                # Whatever its state, the ad shares this creative: link it now
                # so a completion fans its language out to every ad at once.
                newly_linked = link_transcript_ad(
                    conn, int(existing["id"]), ad_id, "url_hash", now
                )
                if status == "completed":
                    result["already_done"] += 1
                    if existing["language"] and newly_linked:
                        apply_media_language(
                            conn, ad_id, str(existing["language"]), now
                        )
                        result["linked"] += 1
                elif status == "failed":
                    result["failed_previously"] += 1
                else:
                    result["pending"] += 1
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO transcripts(
                    media_url_hash, media_path_hash, media_url, library_id,
                    status, created_at, updated_at
                ) VALUES(?,?,?,?,'pending',?,?)
                """,
                (url_hash, url_hash, source_url, ad["library_id"] or None, now, now),
            )
            if cursor.lastrowid:
                link_transcript_ad(conn, int(cursor.lastrowid), ad_id, "url_hash", now)
            result["queued"] += 1
            label = "unknown" if language == "und" else language
            queued_by_language[label] = queued_by_language.get(label, 0) + 1
    result["queued_by_language"] = queued_by_language
    return result


def pending_transcripts(
    conn: sqlite3.Connection, product_id: int, limit: int = 200
) -> list[dict[str, Any]]:
    """The rows a run must process: this product's pending transcripts."""
    rows = conn.execute(
        """
        SELECT DISTINCT t.id AS id, t.media_url AS media_url,
               t.media_url_hash AS media_url_hash, t.library_id AS library_id
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        WHERE ap.product_id = ? AND t.status = 'pending'
        ORDER BY t.id
        LIMIT ?
        """,
        (int(product_id), int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def transcript_ad_ids(conn: sqlite3.Connection, transcript_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT ad_id FROM transcript_ads WHERE transcript_id = ? ORDER BY ad_id",
        (int(transcript_id),),
    ).fetchall()
    return [int(row["ad_id"]) for row in rows]


def retry_failed(conn: sqlite3.Connection, product_id: int) -> int:
    """Flip a product's failed transcripts back to pending. Queues, never runs."""
    with _txn(conn):
        cursor = conn.execute(
            """
            UPDATE transcripts SET status='pending', error=NULL, updated_at=?
            WHERE status='failed' AND id IN (
                SELECT ta.transcript_id FROM transcript_ads ta
                JOIN ad_products ap ON ap.ad_id = ta.ad_id
                WHERE ap.product_id = ?
            )
            """,
            (utc_now(), int(product_id)),
        )
    return int(cursor.rowcount or 0)


STALE_SECONDS = 900
STALLED_AFTER_SECONDS = 300


def recover_stale(
    conn: sqlite3.Connection, product_id: int, *, stale_seconds: int = STALE_SECONDS,
) -> dict[str, int]:
    """Undo what a server restart mid-run leaves behind. Queues, never runs.

    A gunicorn restart kills the run's daemon thread: its `processing` rows
    stay `processing` forever (pending_transcripts only picks `pending`,
    retry_failed only `failed`) and the run stays `running`, so the panel polls
    for eternity. v1 had `recover_stale_transcripts` for exactly this.

    Only rows/runs older than `stale_seconds` are touched, and a run whose
    thread is alive in THIS process is never touched whatever its heartbeat —
    a two-minute download on a slow link is not a crash.
    """
    now = utc_now()
    recovered = 0
    failed_runs = 0
    with _txn(conn):
        live_runs = conn.execute(
            """
            SELECT id, heartbeat_at, started_at, created_at FROM transcription_runs
            WHERE product_id = ? AND status IN ('queued','running')
            """,
            (int(product_id),),
        ).fetchall()
        for run in live_runs:
            thread = _threads.get(int(run["id"]))
            if thread is not None and thread.is_alive():
                continue
            last = run["heartbeat_at"] or run["started_at"] or run["created_at"]
            age = age_seconds(last)
            if age is None or age < stale_seconds:
                continue
            conn.execute(
                """
                UPDATE transcription_runs
                SET status='failed', error=?, finished_at=?, heartbeat_at=?
                WHERE id = ?
                """,
                ("stalled — no progress for a while (server restarted mid-run?). "
                 "Press Generate scripts to resume; finished creatives are not billed again.",
                 now, now, int(run["id"])),
            )
            failed_runs += 1
        cursor = conn.execute(
            """
            UPDATE transcripts SET status='pending', error=NULL, updated_at=?
            WHERE status='processing'
              AND updated_at < ?
              AND id IN (
                  SELECT ta.transcript_id FROM transcript_ads ta
                  JOIN ad_products ap ON ap.ad_id = ta.ad_id
                  WHERE ap.product_id = ?
              )
            """,
            (now, _iso_seconds_ago(stale_seconds), int(product_id)),
        )
        recovered = int(cursor.rowcount or 0)
    return {"recovered": recovered, "failed_runs": failed_runs}


def _iso_seconds_ago(seconds: int) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=max(0, int(seconds)))
    return moment.replace(microsecond=0).isoformat()


def requeue_transcript(
    conn: sqlite3.Connection, product_id: int, transcript_id: int
) -> bool:
    """"Retranscribe this one": back to pending with its identity cleared so
    the next run really re-hears it (content_sha256 NULL means layer-2 dedupe
    cannot short-circuit it against its own old bytes) and re-clusters it.
    v1's retranscribe_product at single-row scope. Queues only. False when the
    transcript does not belong to this product."""
    owned = conn.execute(
        """
        SELECT 1 FROM transcript_ads ta
        JOIN ad_products ap ON ap.ad_id = ta.ad_id
        WHERE ta.transcript_id = ? AND ap.product_id = ?
        LIMIT 1
        """,
        (int(transcript_id), int(product_id)),
    ).fetchone()
    if owned is None:
        return False
    now = utc_now()
    with _txn(conn):
        conn.execute(
            """
            UPDATE transcripts
            SET status='pending', error=NULL, content_sha256=NULL, cluster_id=NULL,
                low_confidence=0, updated_at=?
            WHERE id = ? AND status <> 'processing'
            """,
            (now, int(transcript_id)),
        )
        conn.execute(
            """
            UPDATE script_clusters SET member_count = (
                SELECT COUNT(*) FROM transcripts t WHERE t.cluster_id = script_clusters.id
            )
            """
        )
    return True


def request_cancel(conn: sqlite3.Connection, product_id: int) -> int | None:
    """Ask the product's live run to stop after the creative in flight. The
    run finishes `cancelled`; pending rows stay pending for the next press."""
    row = conn.execute(
        """
        SELECT id FROM transcription_runs
        WHERE product_id = ? AND status IN ('queued','running')
        ORDER BY id DESC LIMIT 1
        """,
        (int(product_id),),
    ).fetchone()
    if row is None:
        return None
    with _txn(conn):
        conn.execute(
            "UPDATE transcription_runs SET cancel_requested_at = ? WHERE id = ?",
            (utc_now(), int(row["id"])),
        )
    return int(row["id"])


def _cancel_requested(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute(
        "SELECT cancel_requested_at FROM transcription_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    return bool(row is not None and row["cancel_requested_at"])


# ===========================================================================
# PART 7 — script clustering
# "50 ads are really 5 videos" — the number the tool exists for.
# ===========================================================================
# Script identity is EXACT-ish BY DESIGN. Two tiers:
#   1. sha256 of the canonical form, keyed by language (fast path);
#   2. character-level difflib ratio >= _NEAR_EXACT_RATIO against a cluster
#      representative, in the SAME language, behind a length guard (ASR noise).
# There is deliberately NO token-set tier: set overlap is order-free and
# length-blind, so it cannot tell "the same script" from "the same opening hook
# and a completely different body" — which is how unrelated ads used to merge.
#
# 0.98 over a ~2000-char script tolerates ~40 characters of drift (roughly 15
# spelling variants: नजर/नज़र, यह/ये, बटन/बटण) and nothing more. Measured on
# v1's corpus: same-creative ASR variants sit at 0.98+, the closest pair of
# genuinely different scripts at 0.947.
_NEAR_EXACT_RATIO = 0.98
_LENGTH_RATIO_FLOOR = 0.90
_MIN_FUZZY_WORDS = 12
CLUSTER_ALGO_VERSION = 2

# ZWSP/ZWNJ/ZWJ/word-joiner/BOM: providers emit these inconsistently inside
# Devanagari conjuncts, which would otherwise look like a real difference.
_INVISIBLE_CHARS = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF))
# Purely orthographic marks rendered inconsistently for the same sound:
# Devanagari nukta (ज़/ज, फ़/फ, ड़/ड) and Arabic hamza-above.
_ORTHOGRAPHIC_MARKS = dict.fromkeys((0x093C, 0x0654))
# A tiny CLOSED set of disfluencies — NOT a stopword list. Dropping real content
# words is strictly harmful under an exact rule.
_DISFLUENCY_TOKENS = frozenset({
    "um", "umm", "uh", "uhh", "hmm", "mmm", "erm", "ah", "aah",
    "आं", "अं", "हम्म", "उम",
})
# P punctuation, S symbols, C control/format, N numbers. Numbers go because the
# same price renders as "399", "३९९" or spelled out. Marks (Mn/Mc) and letters
# are KEPT — that is the Devanagari fix.
_DROPPED_CATEGORIES = frozenset({"P", "S", "C", "N"})


def canonical_script(text: Any) -> str:
    """Canonical comparison form of a transcript.

    THE BUG THIS REPLACES: the old normalizer stripped punctuation with
    ``re.sub(r"[^\\w\\s]|_", " ", ...)``. Python's ``\\w`` does not match Unicode
    categories Mn/Mc, so every Devanagari matra, virama, anusvara and nukta was
    DELETED — "यह प्लस वन नज़र है" became "यह प लस वन नज र ह". Hindi and Marathi
    were reduced to vowel-less consonant skeletons and then compared as an
    order-free token set, which is exactly how unrelated ads merged. Punctuation
    is now removed by Unicode CATEGORY, so marks and letters survive.
    """
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    value = value.translate(_INVISIBLE_CHARS)
    value = unicodedata.normalize(
        "NFC", unicodedata.normalize("NFD", value).translate(_ORTHOGRAPHIC_MARKS)
    )
    value = "".join(
        " " if unicodedata.category(char)[0] in _DROPPED_CATEGORIES else char
        for char in value
    )
    return " ".join(
        token for token in value.split() if token not in _DISFLUENCY_TOKENS
    )


def script_signature(canonical: str) -> str:
    return hashlib.sha256(str(canonical or "").encode("utf-8")).hexdigest()


def _length_compatible(first: str, second: str) -> bool:
    """A 10-word script is never a 200-word script, however well it prefixes it."""
    chars_a, chars_b = len(first), len(second)
    words_a, words_b = len(first.split()), len(second.split())
    if not (chars_a and chars_b and words_a and words_b):
        return False
    return (
        min(chars_a, chars_b) / max(chars_a, chars_b) >= _LENGTH_RATIO_FLOOR
        and min(words_a, words_b) / max(words_a, words_b) >= _LENGTH_RATIO_FLOOR
    )


def near_exact(first: str, second: str) -> float:
    """Similarity of two CANONICAL scripts; 0.0 when any guard rejects the pair,
    so the caller has a single threshold to compare against."""
    if not first or not second:
        return 0.0
    if len(first.split()) < _MIN_FUZZY_WORDS or len(second.split()) < _MIN_FUZZY_WORDS:
        return 0.0                                   # short scripts: exact tier only
    if not _length_compatible(first, second):
        return 0.0
    # autojunk=False matters: the default heuristic fires past 200 elements and
    # would silently ignore frequent characters in a long script.
    matcher = difflib.SequenceMatcher(None, first, second, autojunk=False)
    if matcher.real_quick_ratio() < _NEAR_EXACT_RATIO:
        return 0.0
    if matcher.quick_ratio() < _NEAR_EXACT_RATIO:
        return 0.0
    return matcher.ratio()


def recluster(conn: sqlite3.Connection, product_id: int | None = None) -> dict[str, int]:
    """Assign every unclustered COMPLETED transcript to a script cluster.

    Offline, free, idempotent and incremental: clustered rows are never
    revisited, exact re-runs are no-ops, and two languages never merge. Safe to
    call after every run, which is where it is called from.
    """
    assigned = 0
    created = 0
    with _txn(conn):
        by_signature: dict[tuple[str, str], int] = {}
        by_language: dict[str, list[tuple[int, str]]] = {}
        for row in conn.execute(
            """
            SELECT sc.id AS id, sc.language AS language, sc.signature AS signature,
                   COALESCE(rt.text,'') AS representative
            FROM script_clusters sc
            LEFT JOIN transcripts rt ON rt.id = sc.representative_transcript_id
            """
        ).fetchall():
            cluster_id = int(row["id"])
            language = str(row["language"] or "und")
            if row["signature"]:
                by_signature[(language, str(row["signature"]))] = cluster_id
            by_language.setdefault(language, []).append(
                (cluster_id, canonical_script(row["representative"]))
            )

        params: list[Any] = []
        sql = """
            SELECT t.id AS id, t.language AS language, t.text AS text
            FROM transcripts t
            WHERE t.status = 'completed' AND t.cluster_id IS NULL
              AND t.text IS NOT NULL AND t.text != ''
              -- a guarded row (hallucination / no speech) stays its own script
              AND COALESCE(t.low_confidence, 0) = 0
              -- never pool 'und' rows into one comparable bucket
              AND COALESCE(t.language, 'und') != 'und'
        """
        if product_id is not None:
            sql += """
              AND t.id IN (
                  SELECT ta.transcript_id FROM transcript_ads ta
                  JOIN ad_products ap ON ap.ad_id = ta.ad_id
                  WHERE ap.product_id = ?
              )
            """
            params.append(int(product_id))
        sql += " ORDER BY t.id"

        now = utc_now()
        for row in conn.execute(sql, params).fetchall():
            transcript_id = int(row["id"])
            language = str(row["language"] or "und")
            canonical = canonical_script(row["text"])
            signature = script_signature(canonical)

            cluster_id = by_signature.get((language, signature))
            if cluster_id is None and canonical:
                best_id, best_score = None, 0.0
                for existing_id, representative in by_language.get(language, []):
                    score = near_exact(canonical, representative)
                    if score >= _NEAR_EXACT_RATIO and score > best_score:
                        best_id, best_score = existing_id, score
                cluster_id = best_id
            if cluster_id is None:
                cursor = conn.execute(
                    """
                    INSERT INTO script_clusters(
                        language, representative_transcript_id, signature,
                        canonical_length, member_count, algo_version,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,0,?,?,?)
                    """,
                    (language, transcript_id, signature, len(canonical),
                     CLUSTER_ALGO_VERSION, now, now),
                )
                cluster_id = int(cursor.lastrowid)
                created += 1
                by_language.setdefault(language, []).append((cluster_id, canonical))
            by_signature.setdefault((language, signature), cluster_id)
            conn.execute(
                "UPDATE transcripts SET cluster_id = ?, updated_at = ? WHERE id = ?",
                (cluster_id, now, transcript_id),
            )
            assigned += 1

        if assigned or created:
            # Recomputed, not incremented, so it stays exact even after a
            # requeue has nulled some cluster_ids.
            conn.execute(
                """
                UPDATE script_clusters SET member_count = (
                    SELECT COUNT(*) FROM transcripts t WHERE t.cluster_id = script_clusters.id
                )
                """
            )
    return {"clustered": assigned, "new_clusters": created}


# ===========================================================================
# PART 8 — runs
# The only thing that spends money, and it only ever starts from a button.
# ===========================================================================
RUN_LIMIT = 200
_threads: dict[int, threading.Thread] = {}


def create_run(
    conn: sqlite3.Connection, product_id: int, *, languages: Any = None,
    total: int = 0, message: str = "",
) -> int:
    codes = normalize_language_filter(languages)
    with _txn(conn):
        cursor = conn.execute(
            """
            INSERT INTO transcription_runs(
                product_id, status, languages, total, message, created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (int(product_id), "queued", ",".join(sorted(codes)) if codes else "",
             int(total), message or None, utc_now()),
        )
    return int(cursor.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM transcription_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    return _decorate_run(dict(row)) if row is not None else None


def latest_run(conn: sqlite3.Connection, product_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM transcription_runs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
        (int(product_id),),
    ).fetchone()
    return _decorate_run(dict(row)) if row is not None else None


def _decorate_run(run: dict[str, Any]) -> dict[str, Any]:
    total = int(run.get("total") or 0)
    processed = int(run.get("processed") or 0)
    run["percent"] = int(round(100 * processed / total)) if total else (
        100 if run.get("status") in {"completed", "failed", "cancelled"} else 0
    )
    run["is_live"] = run.get("status") in {"queued", "running"}
    run["language_labels"] = [
        language_name(code) for code in str(run.get("languages") or "").split(",") if code
    ]
    run["cancelling"] = bool(run["is_live"] and run.get("cancel_requested_at"))
    # No heartbeat for five minutes on a live run: the server probably restarted
    # under it. Shown as a hint, never acted on from a read path — the next
    # Generate press runs recover_stale and resumes.
    last = run.get("heartbeat_at") or run.get("started_at") or run.get("created_at")
    age = age_seconds(last) if run["is_live"] else None
    run["stalled"] = bool(age is not None and age >= STALLED_AFTER_SECONDS)
    run["stalled_minutes"] = int(age // 60) if run["stalled"] and age is not None else 0
    return run


_RUN_COUNTERS = frozenset({
    "processed", "completed", "deduped", "failed", "skipped", "api_calls",
    "clusters_created", "total",
})


def _bump(conn: sqlite3.Connection, run_id: int, **deltas: int) -> None:
    """Increment run counters. Column names are whitelisted, not interpolated
    from anything a request can reach — this SQL is the only place in the module
    that builds a statement by string."""
    deltas = {key: value for key, value in deltas.items() if key in _RUN_COUNTERS}
    # No deltas = a plain heartbeat, stamped before a long download so the
    # panel's "stalled" hint does not fire on a slow link.
    sets = "".join(f"{column} = {column} + ?, " for column in deltas)
    with _txn(conn):
        conn.execute(
            f"UPDATE transcription_runs SET {sets}heartbeat_at = ? WHERE id = ?",
            [*deltas.values(), utc_now(), int(run_id)],
        )


def _finish(
    conn: sqlite3.Connection, run_id: int, status: str, *,
    message: str = "", error: str = "", provider: str = "",
) -> None:
    with _txn(conn):
        conn.execute(
            """
            UPDATE transcription_runs
            SET status = ?, message = ?, error = ?,
                provider = CASE WHEN ? <> '' THEN ? ELSE provider END,
                finished_at = ?, heartbeat_at = ?
            WHERE id = ?
            """,
            (status, message or None, error or None, provider, provider,
             utc_now(), utc_now(), int(run_id)),
        )


def process_run(
    conn: sqlite3.Connection, run_id: int, *, keys: dict[str, str] | None = None,
    configured_provider: str = DEFAULT_PROVIDER, transcribe=None,
) -> dict[str, Any]:
    """Do the work of one run. THIS is the function that spends money.

    `transcribe` is the seam: a callable (media_path, mime, hint, provider_name)
    -> result dict. It defaults to the real providers; tests pass a fake and
    therefore never touch the network, and neither does any code path that has
    not been handed a key.
    """
    run = get_run(conn, run_id)
    if run is None:
        return {"ok": False, "error": "no such run"}
    product_id = int(run["product_id"])
    keys = keys if keys is not None else api_keys(conn)
    if not (keys.get("groq_api_key") or keys.get("sarvam_api_key")):
        _finish(conn, run_id, "failed",
                error="No transcription API key is configured.")
        return {"ok": False, "error": "no_api_key"}

    with _txn(conn):
        conn.execute(
            "UPDATE transcription_runs SET status='running', started_at=?, heartbeat_at=? "
            "WHERE id = ?",
            (utc_now(), utc_now(), int(run_id)),
        )

    rows = pending_transcripts(conn, product_id, RUN_LIMIT)
    with _txn(conn):
        conn.execute(
            "UPDATE transcription_runs SET total = ? WHERE id = ?",
            (len(rows), int(run_id)),
        )

    providers_used: set[str] = set()
    cancelled = False
    thumbs = thumbs_dir(conn)
    for row in rows:
        if _cancel_requested(conn, run_id):
            cancelled = True
            break
        transcript_id = int(row["id"])
        ad_ids = transcript_ad_ids(conn, transcript_id)
        hint = language_hint_for_ad(conn, ad_ids[0]) if ad_ids else "hi"
        provider_name = runnable_provider(
            preferred_provider(configured_provider, hint, keys), keys
        )
        providers_used.add(provider_name)
        _bump(conn, run_id)                              # heartbeat before a long download
        try:
            outcome = _process_one(
                conn, transcript_id, str(row["media_url"] or ""), ad_ids, hint,
                provider_name, keys, transcribe,
                library_id=str(row.get("library_id") or ""), thumbs=thumbs,
            )
        except Exception as exc:                          # noqa: BLE001
            outcome = "failed"
            _fail_transcript(conn, transcript_id, _redact(exc, keys))
            log.warning("transcript %s failed: %s", transcript_id, _redact(exc, keys))
        _bump(conn, run_id, processed=1, **{outcome: 1},
              **({"api_calls": 1} if outcome == "completed" else {}))

    clustered = recluster(conn, product_id)
    processed = int((get_run(conn, run_id) or {}).get("processed") or 0)
    if cancelled:
        message = (
            f"Cancelled after {processed} of {len(rows)} creative(s); "
            f"{len(rows) - processed} left pending for the next press."
        )
    else:
        message = (
            f"{len(rows)} creative(s) processed; "
            f"{clustered['new_clusters']} new script(s)."
        )
    _finish(
        conn, run_id, "cancelled" if cancelled else "completed",
        message=message,
        provider=",".join(sorted(providers_used)),
    )
    with _txn(conn):
        conn.execute(
            "UPDATE transcription_runs SET clusters_created = ? WHERE id = ?",
            (int(clustered["new_clusters"]), int(run_id)),
        )
    return {"ok": True, "processed": processed, "cancelled": cancelled, **clustered}


def _process_one(
    conn: sqlite3.Connection, transcript_id: int, media_url: str,
    ad_ids: Sequence[int], hint: str, provider_name: str,
    keys: dict[str, str], transcribe, *, library_id: str = "",
    thumbs: Path | None = None,
) -> str:
    """One creative, start to finish. Returns the run counter to bump."""
    now = utc_now()
    with _txn(conn):
        conn.execute(
            "UPDATE transcripts SET status='processing', updated_at=? WHERE id=?",
            (now, transcript_id),
        )
    if not library_id and ad_ids:
        row = conn.execute(
            "SELECT library_id FROM ads WHERE id = ?", (int(ad_ids[0]),)
        ).fetchone()
        library_id = str(row["library_id"] or "") if row is not None else ""
    if not media_url and not (library_id and media_refresh.playwright_available()):
        _fail_transcript(conn, transcript_id, "no media URL stored for this creative")
        return "skipped"

    data, mime = _fetch_with_refresh(conn, transcript_id, media_url, library_id, ad_ids)
    content_sha256 = hashlib.sha256(data).hexdigest()
    with _txn(conn):
        conn.execute(
            "UPDATE transcripts SET content_sha256=?, updated_at=? WHERE id=?",
            (content_sha256, utc_now(), transcript_id),
        )

    duplicate = _find_content_duplicate(conn, content_sha256, transcript_id)
    if duplicate is not None:
        _record_dedupe(conn, transcript_id, duplicate, ad_ids)
        return "deduped"

    handle = tempfile.NamedTemporaryFile(prefix="adspy2-media-", suffix=".mp4",
                                         delete=False)
    try:
        handle.write(data)
        handle.close()
        if transcribe is not None:
            result = transcribe(handle.name, mime, hint, provider_name)
        else:
            provider = get_provider(provider_name, keys)
            result = provider.transcribe(handle.name, mime, hint)
        extras = _media_extras(handle.name, result, content_sha256, thumbs)
    finally:
        _safe_unlink(handle.name)

    _record_success(conn, transcript_id, result, ad_ids, extras)
    return "completed"


def _media_extras(
    media_path: str, result: dict[str, Any], content_sha256: str,
    thumbs: Path | None,
) -> dict[str, Any]:
    """Duration and poster for the per-video table. Groq reports a duration;
    Sarvam does not, so ffmpeg reads it off the file. Both are best-effort and
    never fail the creative."""
    duration = _parse_duration_seconds(
        result.get("duration_seconds") if result.get("duration_seconds") is not None
        else result.get("duration")
    )
    if duration is None:
        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            duration = _media_duration_seconds(ffmpeg, media_path)
    thumb = _make_thumbnail(media_path, content_sha256, thumbs) if thumbs is not None else None
    return {"duration_seconds": duration, "thumb_path": thumb}


def _record_success(
    conn: sqlite3.Connection, transcript_id: int, result: dict[str, Any],
    ad_ids: Sequence[int], extras: dict[str, Any] | None = None,
) -> None:
    """Store a real transcription, reconciled, and fan its language out.

    Both codes are kept: `provider_language` is what the provider said,
    `language` is what the script evidence says — the per-video table shows
    "provider said hi · script says mr" so the decision is visible."""
    text = str(result.get("transcript") or "")
    low_confidence = 1 if result.get("low_confidence") else 0
    provider_language = normalize_language_code(result.get("language"))
    # Script evidence beats the provider's own label — see rule 6.
    language = reconcile_transcript_language(result.get("language"), text)
    extras = extras or {}
    now = utc_now()
    with _txn(conn):
        conn.execute(
            """
            UPDATE transcripts
            SET status='completed', language=?, text=?, hook_summary=?,
                low_confidence=?, model=?, provider=?, error=NULL, updated_at=?,
                provider_language=?, duration_seconds=?, thumb_path=?
            WHERE id = ?
            """,
            (language, text, result.get("hook") or auto_hook(text), low_confidence,
             str(result.get("model") or ""),
             str(result.get("model") or "").split(":", 1)[0], now,
             provider_language, extras.get("duration_seconds"), extras.get("thumb_path"),
             transcript_id),
        )
        if not low_confidence and language != "und":
            for ad_id in ad_ids:
                apply_media_language(conn, int(ad_id), language, now)


def _record_dedupe(
    conn: sqlite3.Connection, transcript_id: int, source: dict[str, Any],
    ad_ids: Sequence[int],
) -> None:
    """Complete a row by COPYING one whose bytes were identical. Zero API calls.

    `model='dedupe:content'` is what the provenance rule in
    ``_find_content_duplicate`` keys on, so a copy can never become a source.
    """
    now = utc_now()
    with _txn(conn):
        conn.execute(
            """
            UPDATE transcripts
            SET status='completed', language=?, text=?, hook_summary=?,
                low_confidence=?, model='dedupe:content', provider='dedupe',
                error=NULL, updated_at=?,
                provider_language=?, duration_seconds=?, thumb_path=?
            WHERE id = ?
            """,
            (source["language"], source["text"], source.get("hook_summary"),
             int(source.get("low_confidence") or 0), now,
             source.get("provider_language"), source.get("duration_seconds"),
             source.get("thumb_path"), transcript_id),
        )
        if not source.get("low_confidence") and source["language"] != "und":
            for ad_id in ad_ids:
                apply_media_language(conn, int(ad_id), source["language"], now)


def _fail_transcript(conn: sqlite3.Connection, transcript_id: int, error: str) -> None:
    with _txn(conn):
        conn.execute(
            "UPDATE transcripts SET status='failed', error=?, updated_at=? WHERE id=?",
            (_redact(error)[:500], utc_now(), transcript_id),
        )


def start_run(
    product_id: int, *, languages: Any = None, conn: sqlite3.Connection | None = None,
    background: bool = True, transcribe=None,
) -> dict[str, Any]:
    """The button's entry point: queue, then process.

    `background=False` runs inline, which is what the tests use. `background=True`
    hands the work to a daemon thread with its OWN connection, because a run over
    forty creatives outlives any sensible request.
    """
    connection = conn if conn is not None else db.get_db()
    status = provider_status(connection)
    if not status["ready"]:
        return {"ok": False, "reason": "no_api_key", "message": status["reason"]}

    # A restart mid-run leaves `processing` rows and a `running` receipt behind;
    # put them back before queueing so the resume actually resumes.
    recover_stale(connection, product_id)
    live = latest_run(connection, product_id)
    if live is not None and live["is_live"]:
        # One run per product at a time: a double click must not bill twice.
        return {"ok": False, "reason": "already_running", "run": live}

    queued = queue_product(connection, product_id, languages=languages)
    pending = pending_transcripts(connection, product_id, RUN_LIMIT)
    if not pending:
        return {"ok": False, "reason": "nothing_to_do", "queued": queued}

    run_id = create_run(
        connection, product_id, languages=languages, total=len(pending),
        message=f"{len(pending)} creative(s) queued.",
    )
    provider_setting = configured_provider(connection)
    if not background:
        keys = api_keys(connection)
        process_run(connection, run_id, keys=keys, transcribe=transcribe,
                    configured_provider=provider_setting)
        return {"ok": True, "run_id": run_id, "queued": queued,
                "run": get_run(connection, run_id)}

    path = str(connection.execute("PRAGMA database_list").fetchone()[2] or app_config.db_path())
    thread = threading.Thread(
        target=_run_in_thread, args=(path, run_id, provider_setting),
        name=f"adspy2-transcribe-{run_id}", daemon=True,
    )
    _threads[run_id] = thread
    thread.start()
    return {"ok": True, "run_id": run_id, "queued": queued,
            "run": get_run(connection, run_id)}


def _run_in_thread(
    database_path: str, run_id: int, provider_setting: str = DEFAULT_PROVIDER
) -> None:
    connection = db.connect(database_path)
    try:
        process_run(connection, run_id, configured_provider=provider_setting)
    except Exception as exc:                              # noqa: BLE001
        log.exception("transcription run %s crashed", run_id)
        try:
            _finish(connection, run_id, "failed", error=_redact(exc))
        except sqlite3.Error:                             # pragma: no cover
            pass
    finally:
        _threads.pop(run_id, None)
        connection.close()


# ===========================================================================
# PART 9 — what the product screen shows
# Read-only shapes for templates/_transcripts.html. Nothing here writes.
# ===========================================================================
def _split_library_ids(raw: Any) -> list[str]:
    seen: list[str] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def product_videos(
    conn: sqlite3.Connection, product_id: int, *, language: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """One row per creative (transcript) this product's ads carry — the
    per-video table: poster, language (and how it was decided), duration,
    script, cluster, status, model.

    Sorted by cluster then by how many ads carry it, so the scripts that run
    the most ads sit at the top and identical scripts sit together.
    """
    params: list[Any] = [int(product_id)]
    where = ["ap.product_id = ?"]
    code = str(language or "").strip().lower()
    if code:
        where.append("lower(COALESCE(t.language,'und')) = ?")
        params.append("und" if code == "unknown" else code)
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT t.id AS transcript_id, t.status, t.language, t.provider_language,
               t.text, t.hook_summary, t.duration_seconds, t.thumb_path,
               t.content_sha256, t.model, t.provider, t.low_confidence,
               t.cluster_id, t.error, t.media_url, t.refreshed_at, t.updated_at,
               COUNT(DISTINCT a.id)                     AS ad_count,
               COUNT(DISTINCT a.page_id)                AS page_count,
               GROUP_CONCAT(DISTINCT a.library_id)      AS library_ids,
               MAX(a.media_urls)                        AS media_urls
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        JOIN ads a             ON a.id = ta.ad_id
        WHERE {' AND '.join(where)}
        GROUP BY t.id
        ORDER BY (t.cluster_id IS NULL) ASC, t.cluster_id ASC, ad_count DESC, t.id ASC
        LIMIT ?
        """,
        params,
    ).fetchall()
    videos: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        lang = str(row.get("language") or "und").lower()
        provider_lang = str(row.get("provider_language") or "").lower()
        guarded = bool(row.get("low_confidence"))
        status = str(row.get("status") or "pending")
        hook = str(row.get("hook_summary") or "").strip()
        if hook.startswith("[auto]"):
            hook = hook[6:].strip()
        text = str(row.get("text") or "")
        library_ids = _split_library_ids(row.get("library_ids"))
        media_url = str(row.get("media_url") or "")
        row.update({
            "language_code": lang,
            "language_name": language_name(lang),
            "provider_language_code": provider_lang,
            "provider_language_name": language_name(provider_lang) if provider_lang else "",
            "language_overruled": bool(provider_lang and provider_lang != lang and lang != "und"),
            "guarded": guarded,
            "status_label": "guarded" if (guarded and status == "completed") else status,
            "duration_label": duration_label(row.get("duration_seconds")),
            "hook": hook or (text.split("\n", 1)[0][:120] if text else ""),
            "transcript": text,
            "script_label": f"S-{row['cluster_id']}" if row.get("cluster_id") else "",
            "library_ids": library_ids,
            "library_id": library_ids[0] if library_ids else "",
            "thumb_url": thumb_url_for(row.get("content_sha256"), row.get("thumb_path")),
            "link_state": media_refresh.expiry_label(media_url) if media_url else "none",
            "model_label": str(row.get("model") or ""),
        })
        videos.append(row)
    return videos


def video_language_summary(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    """"12 videos: 8 Hindi, 3 Marathi, 1 Tamil" — completed creatives by
    reconciled language. Guarded no-speech rows are counted separately."""
    rows = conn.execute(
        """
        SELECT lower(COALESCE(t.language,'und')) AS code,
               COALESCE(t.low_confidence,0)       AS guarded,
               COUNT(DISTINCT t.id)               AS n
        FROM transcripts t
        JOIN transcript_ads ta ON ta.transcript_id = t.id
        JOIN ad_products ap    ON ap.ad_id = ta.ad_id
        WHERE ap.product_id = ? AND t.status = 'completed'
        GROUP BY code, guarded
        """,
        (int(product_id),),
    ).fetchall()
    parts: dict[str, int] = {}
    guarded = 0
    total = 0
    for row in rows:
        count = int(row["n"] or 0)
        total += count
        if int(row["guarded"] or 0) or str(row["code"]) == "und":
            guarded += count
            continue
        parts[str(row["code"])] = parts.get(str(row["code"]), 0) + count
    ordered = sorted(parts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "total": total,
        "spoken": total - guarded,
        "guarded": guarded,
        "parts": [{"code": code, "name": language_name(code), "count": count}
                  for code, count in ordered],
        "sentence": (
            f"{total} video{'' if total == 1 else 's'}: "
            + ", ".join(f"{count} {language_name(code)}" for code, count in ordered)
            + (f", {guarded} with no clear speech" if guarded else "")
        ) if total else "",
    }


def ad_transcript(conn: sqlite3.Connection, ad_id: int) -> dict[str, Any] | None:
    """The one ad's transcript + language evidence, for the ad drawer. None
    when the ad has neither a transcript nor a detected language."""
    lang_row = conn.execute(
        "SELECT text_language, media_language, final_language FROM ad_languages WHERE ad_id = ?",
        (int(ad_id),),
    ).fetchone()
    row = conn.execute(
        """
        SELECT t.id AS transcript_id, t.status, t.language, t.provider_language,
               t.text, t.hook_summary, t.duration_seconds, t.low_confidence,
               t.cluster_id, t.model, t.error, t.content_sha256, t.thumb_path,
               (SELECT COUNT(DISTINCT ta2.ad_id) FROM transcript_ads ta2
                 WHERE ta2.transcript_id = t.id)                              AS ad_count,
               (SELECT COUNT(DISTINCT ta3.ad_id) FROM transcripts t3
                 JOIN transcript_ads ta3 ON ta3.transcript_id = t3.id
                 WHERE t3.cluster_id = t.cluster_id AND t.cluster_id IS NOT NULL) AS cluster_ads,
               (SELECT ap.product_id FROM ad_products ap
                 WHERE ap.ad_id = ? ORDER BY ap.product_id LIMIT 1)          AS product_id
        FROM transcript_ads ta
        JOIN transcripts t ON t.id = ta.transcript_id
        WHERE ta.ad_id = ?
        ORDER BY (t.status = 'completed') DESC, t.id DESC
        LIMIT 1
        """,
        (int(ad_id), int(ad_id)),
    ).fetchone()
    if lang_row is None and row is None:
        return None
    result: dict[str, Any] = {
        "text_language": "", "media_language": "", "final_language": "",
        "text_language_name": "", "media_language_name": "", "final_language_name": "",
        "transcript": None,
    }
    if lang_row is not None:
        for key in ("text_language", "media_language", "final_language"):
            code = str(lang_row[key] or "").lower()
            result[key] = code
            result[f"{key}_name"] = language_name(code) if code else ""
    if row is not None:
        data = dict(row)
        lang = str(data.get("language") or "und").lower()
        provider_lang = str(data.get("provider_language") or "").lower()
        hook = str(data.get("hook_summary") or "").strip()
        if hook.startswith("[auto]"):
            hook = hook[6:].strip()
        data.update({
            "language_code": lang,
            "language_name": language_name(lang),
            "provider_language_name": language_name(provider_lang) if provider_lang else "",
            "language_overruled": bool(provider_lang and provider_lang != lang and lang != "und"),
            "guarded": bool(data.get("low_confidence")),
            "duration_label": duration_label(data.get("duration_seconds")),
            "hook": hook,
            "script_label": f"S-{data['cluster_id']}" if data.get("cluster_id") else "",
            "thumb_url": thumb_url_for(data.get("content_sha256"), data.get("thumb_path")),
        })
        result["transcript"] = data
        if not result["final_language"] and data.get("status") == "completed" and lang != "und":
            result["final_language"] = lang
            result["final_language_name"] = language_name(lang)
    return result


def ad_transcript_for(ad_id: Any) -> dict[str, Any] | None:
    """Template-global form of ``ad_transcript`` (request connection). Registered
    by app/routes/products.py so _drawer.html can show an ad's script without
    the ad-drawer route (app/routes/pages.py) having to know about this module."""
    try:
        return ad_transcript(db.get_db(), int(ad_id))
    except (TypeError, ValueError, sqlite3.Error):
        return None


__all__ = [
    "GROQ_MODEL", "SARVAM_MODEL", "INDIC_LANGUAGES", "ProviderError",
    "ProviderConfigurationError", "GroqProvider", "SarvamProvider",
    "ad_transcript", "ad_transcript_for", "api_keys", "auto_hook",
    "backfill_text_languages", "canonical_script", "configured_provider",
    "create_run", "detect_ad_text_language", "detect_text_language",
    "duration_label", "estimate_new", "fetch_media", "get_provider", "get_run",
    "language_hint_for_ad", "language_name", "latest_run", "legacy_media_url_hash",
    "looks_like_video", "media_dir", "media_state", "media_url_hash", "near_exact",
    "normalize_language_code", "normalize_language_filter", "pick_poster_url",
    "pick_video_url", "preferred_provider", "process_run", "product_videos",
    "provider_status", "queue_product", "recluster", "reconcile_transcript_language",
    "recover_stale", "refresh_job", "refresh_product_media", "refresh_video_url",
    "request_cancel", "requeue_transcript", "retry_failed", "runnable_provider",
    "script_signature",
    "start_media_refresh", "start_run", "strip_noise", "thumb_url_for",
    "thumbs_dir", "validate_transcript", "video_language_summary",
]
