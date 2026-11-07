"""Il MediaInfo in forma di testo, come lo mostrano i tracker (il campo
media_info di UNIT3D), nella stessa forma di nazgarr/library/mediainfo.py
summarize, ridotto a quello che serve per confrontare due release nel dupe
check di un upload (docs/SPEC.md §9 "Dupe check"): il video (formato, chi
l'ha scritto, HDR) e le tracce audio (lingua, titolo, formato).

Il testo viene dal tracker: si leggono solo righe "Chiave : valore" delle
sezioni Video e Audio, nient'altro, e solo il primo file di un report con
più file (quello di un season pack ne ha uno per episodio, ognuno con la
sua sezione General)."""

import re

# Una sezione: "General", "Video", "Audio #2", "Text #1", "Menu"...
_HEADER = re.compile(r"^(General|Video|Audio|Text|Menu|Image|Other)(?:\s*#\s*\d+)?$")
_LINE = re.compile(r"^([^:]+?)\s*:\s*(.*)$")

_VIDEO_FIELDS = {
    "Format": "format",
    "Writing library": "writing_library",
    "Encoding settings": "encoding_settings",
    "HDR format": "hdr_format",
    "Transfer characteristics": "transfer_characteristics",
}
_AUDIO_FIELDS = {"Format": "format", "Language": "language", "Title": "title"}

# La lingua come la scrive MediaInfo in testo (in inglese) -> il codice
# ISO 639-1 che pymediainfo dà per un file locale, per confrontarle. Una
# lingua che non è qui resta senza codice, e le lingue di quel torrent sono
# ignote (non diverse): meglio nessun confronto che uno sbagliato.
LANGUAGE_CODES = {
    "arabic": "ar", "basque": "eu", "bengali": "bn", "bulgarian": "bg", "catalan": "ca", "chinese": "zh",
    "mandarin": "zh", "croatian": "hr", "czech": "cs", "danish": "da", "dutch": "nl", "english": "en",
    "estonian": "et", "filipino": "tl", "tagalog": "tl", "finnish": "fi", "french": "fr", "galician": "gl",
    "german": "de", "greek": "el", "hebrew": "he", "hindi": "hi", "hungarian": "hu", "icelandic": "is",
    "indonesian": "id", "irish": "ga", "italian": "it", "japanese": "ja", "kannada": "kn", "korean": "ko",
    "latin": "la", "latvian": "lv", "lithuanian": "lt", "malay": "ms", "malayalam": "ml", "marathi": "mr",
    "norwegian": "no", "norwegian bokmal": "no", "persian": "fa", "polish": "pl", "portuguese": "pt",
    "punjabi": "pa", "romanian": "ro", "russian": "ru", "serbian": "sr", "slovak": "sk", "slovenian": "sl",
    "spanish": "es", "swedish": "sv", "tamil": "ta", "telugu": "te", "thai": "th", "turkish": "tr",
    "ukrainian": "uk", "urdu": "ur", "vietnamese": "vi", "welsh": "cy",
}


def language_code(language: str | None) -> str | None:
    """Il codice ISO 639-1 di una lingua di MediaInfo, scritta col codice
    (un file locale: "it", "en-US") o col nome (il testo di un tracker:
    "Italian", "English (US)"). None se non si sa."""
    value = str(language or "").strip()
    if not value:
        return None
    head = re.split(r"[-_]", value)[0]
    if head.isalpha() and len(head) in (2, 3) and head.islower():  # un codice, non un nome
        code = _ALPHA3.get(head, head)
        return "no" if code == "nb" else code
    return LANGUAGE_CODES.get(value.split("(")[0].strip().lower())


# Le lingue più comuni scritte col codice a tre lettere (ISO 639-2), che
# qualche MediaInfo dà al posto di quello a due.
_ALPHA3 = {
    "ita": "it", "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "por": "pt",
    "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "rus": "ru", "dut": "nl", "nld": "nl", "swe": "sv",
    "dan": "da", "nor": "no", "nob": "no", "fin": "fi", "pol": "pl", "cze": "cs", "ces": "cs", "hun": "hu",
    "tur": "tr", "gre": "el", "ell": "el", "heb": "he", "ara": "ar", "hin": "hi", "tha": "th", "ukr": "uk",
    "rum": "ro", "ron": "ro",
}


def summarize(text: str | None) -> dict | None:
    """{"video": {...} | None, "audio": [{...}, ...]} del primo file del
    report, con le chiavi di mediainfo.summarize (la lingua già come codice
    ISO 639-1, o None); None se il testo non ha né video né audio."""
    if not text:
        return None
    video: dict | None = None
    audio: list[dict] = []
    current: dict | None = None
    fields: dict = {}
    generals = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        header = _HEADER.match(line)
        if header:
            kind = header.group(1)
            current = None
            if kind == "General":
                generals += 1
                if generals > 1:
                    break  # il file dopo, in un report di un pack
            elif kind == "Video" and video is None:
                current, fields = {}, _VIDEO_FIELDS
                video = current
            elif kind == "Audio":
                current, fields = {}, _AUDIO_FIELDS
                audio.append(current)
            continue
        if current is None:
            continue
        match = _LINE.match(line)
        if not match:
            continue
        field = fields.get(match.group(1).strip())
        if field and field not in current:
            current[field] = match.group(2).strip()[:500]
    if video is None and not audio:
        return None
    if video is not None:
        video["encoding_settings"] = bool(video.get("encoding_settings"))
    for track in audio:
        track["language"] = language_code(track.get("language"))
    return {"video": video, "audio": audio}
