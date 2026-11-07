"""Dupe check di un upload contro i risultati di un tracker (docs/SPEC.md §9
"Upload flow v2", "Dupe check"). Funzioni pure: nessuna rete, nessun DB.

Due metà:
- release_of(): quello che si sa di un torrent del tracker, letto una volta
  dalla ricerca e salvato col suo risultato (dupes_json[i].release): tipo e
  risoluzione dai campi del tracker, codec, HDR e lingue audio dal suo
  MediaInfo, il resto (e quello che manca) dal nome; stessi byte o quanto
  è diversa la dimensione; i seeder;
- judge(): il verdetto, contro i tratti della sorgente (quelli del nome
  della release: MediaInfo, nome, override). Si rifà a ogni cambio degli
  override (nazgarr/upload/decision.py propose) senza chiedere niente al tracker.

I tratti che fanno lo "slot" di una release, col loro peso (decisione
dell'utente, 2026-11-07): copertura (stagione/episodio), risoluzione, tipo
(REMUX, WEB-DL, WEBRip, encode... tutti distinti), famiglia del codec
(x264 = H.264 = AVC, x265 = H.265 = HEVC, AV1 a parte), HDR (DV, HDR),
lingue audio (lo stesso insieme, commenti esclusi). Il servizio di
streaming non conta. Un tratto noto e diverso da una parte e dall'altra:
"different". Se no, il punteggio è il peso dei tratti noti e uguali sul
totale; un tratto ignoto vale zero, tranne le lingue, che ignote escono dal
totale (sul tracker italiano c'è comunque l'italiano).

Verdetti:
- identical: stessi byte (dimensione totale o dei video). Reseed, dopo un
  full hash check; se il torrent è morto, il reseed lo resuscita.
- same_slot: il punteggio arriva alla soglia (upload_dupe_threshold). Skip,
  ma solo se ha seeder: un dupe morto non ferma l'upload.
- trump: come same_slot, ma un encode di dimensione diversa di oltre il 10%
  (in più o in meno): un altro encode, che può sostituirlo. Upload.
- possible: niente di diverso, ma troppo poco di noto per arrivare alla
  soglia. Upload, con un avviso.
- different: almeno un tratto diverso. Upload.
Video entro il 2% della dimensione aggiungono 15 punti: con ogni probabilità
lo stesso flusso in un altro contenitore.
"""

import re
from dataclasses import asdict, dataclass, fields

from nazgarr.adapters.tracker.base import TorrentCandidate
from nazgarr.core.file_types import is_video
from nazgarr.library.guess import as_list
from nazgarr.library.guess import guess as guess_name
from nazgarr.library.mediainfo_text import language_code


@dataclass(frozen=True)
class ReleaseTraits:
    resolution: str | None
    source: str | None  # web | bluray | hdtv | dvd | None
    remux: bool
    hdr: frozenset[str]  # sottoinsieme di {"HDR", "DV"}
    seasons: frozenset[int]
    episodes: frozenset[int]
    repack: bool
    group: str | None


_SOURCES = {
    "web": "web", "web-dl": "web", "webrip": "web",
    "blu-ray": "bluray", "ultra hd blu-ray": "bluray", "hd-dvd": "bluray",
    "hdtv": "hdtv", "ultra hdtv": "hdtv", "tv": "hdtv",
    "dvd": "dvd",
}


def traits_of(name: str) -> ReleaseTraits:
    # Lo stesso guessit del resto dell'app (nazgarr/library/guess.py), col nome ripulito.
    guess = guess_name(name)
    others = {str(o) for o in as_list(guess.get("other"))}
    hdr = set()
    if others & {"HDR10", "HDR10+", "HDR"}:
        hdr.add("HDR")
    if "Dolby Vision" in others or " dv " in f" {name.lower().replace('.', ' ')} ":
        hdr.add("DV")
    source = _SOURCES.get(str(guess.get("source") or "").lower())
    return ReleaseTraits(
        resolution=guess.get("screen_size"),
        source=source,
        remux="Remux" in others,
        hdr=frozenset(hdr),
        seasons=frozenset(s for s in as_list(guess.get("season")) if isinstance(s, int)),
        episodes=frozenset(e for e in as_list(guess.get("episode")) if isinstance(e, int)),
        repack=bool(guess.get("proper_count")) or bool(others & {"Proper", "Repack"}),
        group=guess.get("release_group"),
    )


# --- I tratti dello slot --------------------------------------------------------

TRAITS = ("coverage", "resolution", "type", "codec", "hdr", "languages")
WEIGHTS = {"coverage": 15, "resolution": 20, "type": 20, "codec": 15, "hdr": 10, "languages": 20}
DEFAULT_THRESHOLD = 70
THRESHOLD_SETTING = "upload_dupe_threshold"
SIZE_CLOSE = 0.02
SIZE_CLOSE_BONUS = 15
ENCODE_GAP = 0.10
# I tipi in cui chi carica sceglie il bitrate: lì una dimensione diversa è un altro encode.
ENCODE_TYPES = frozenset({"ENCODE", "BRRIP", "WEBRIP", "DVDRIP"})

VERDICTS = ("identical", "same_slot", "trump", "possible", "different")
_VERDICT_ORDER = {verdict: i for i, verdict in enumerate(VERDICTS)}

_CODEC_FAMILY = {
    "avc": "AVC", "h.264": "AVC", "h264": "AVC", "x264": "AVC",
    "hevc": "HEVC", "h.265": "HEVC", "h265": "HEVC", "x265": "HEVC",
    "av1": "AV1", "vc-1": "VC-1", "vc1": "VC-1",
    "mpeg video": "MPEG-2", "mpeg-2": "MPEG-2", "mpeg2": "MPEG-2",
    "xvid": "MPEG-4", "divx": "MPEG-4", "mpeg-4 visual": "MPEG-4", "vp9": "VP9",
}
_RESOLUTION = re.compile(r"\d{3,4}[pi]")


def codec_family(codec: str | None) -> str | None:
    """La famiglia del codec video: x264, H.264 e AVC sono lo stesso slot
    (decisione dell'utente, 2026-11-07), come x265, H.265 e HEVC."""
    key = str(codec or "").strip().lower()
    if not key:
        return None
    return _CODEC_FAMILY.get(key, key.upper())


def hdr_family(label: str | None) -> frozenset[str]:
    """{"DV", "HDR"} da un'etichetta HDR ("DV HDR10+", "DV.P7.HDR10",
    "HLG"); vuoto per SDR o niente."""
    words = set(str(label or "").upper().replace(".", " ").split())
    out = set()
    if words & {"DV", "DOVI"}:
        out.add("DV")
    if any(word.startswith("HDR") or word == "HLG" for word in words):
        out.add("HDR")
    return frozenset(out)


def _resolution(value: str | None) -> str | None:
    value = str(value or "").strip().lower()
    return value if _RESOLUTION.fullmatch(value) else None


def _name_type(name: str) -> str | None:
    """Il tipo (chiave dei profili) dal nome, se il nome lo dice: senza una
    sorgente né un segno di remux non è "ENCODE", è ignoto."""
    from nazgarr.upload import naming  # import qui: naming importa questo modulo

    guess = guess_name(name)
    release = naming.release_type(guess, name)
    if release == "ENCODE" and not guess.get("source"):
        return None
    return release


def audio_languages(tracks: list[dict] | None) -> frozenset[str] | None:
    """Le lingue (ISO 639-1) delle tracce audio, commenti esclusi. None se
    una traccia non dice la sua lingua, o non ce ne sono: ignote."""
    from nazgarr.upload import naming  # import qui: naming importa questo modulo

    codes = [language_code(t.get("language")) for t in tracks or [] if not naming.is_commentary(t)]
    if not codes or not all(codes):
        return None
    return frozenset(codes)


@dataclass(frozen=True)
class SourceSummary:
    """Quello che serve della sorgente locale per confrontarla."""

    name: str  # il nome da cui si leggono i tratti (name_source dell'analisi)
    total_size_bytes: int  # tutti i file, video e non (come la dimensione di un torrent)
    video_sizes: tuple[int, ...]
    kind: str  # movie | episode | season_pack | complete_pack
    seasons: frozenset[int]
    episode: int | None
    # Dai valori del nome della release (nazgarr/upload/naming.py
    # release_values: MediaInfo, nome, override): valgono più del nome, che
    # spesso non li dice (un nome rinominato da Plex/Radarr). None: dal nome.
    resolution: str | None = None
    release_type: str | None = None  # chiave dei profili: REMUX, ENCODE, WEBDL...
    codec: str | None = None  # famiglia (codec_family)
    hdr: frozenset[str] | None = None
    languages: frozenset[str] | None = None


@dataclass(frozen=True)
class SourceTraits:
    resolution: str | None
    type: str | None
    codec: str | None
    hdr: frozenset[str] | None
    languages: frozenset[str] | None
    repack: bool
    group: str | None


def source_traits(source: SourceSummary) -> SourceTraits:
    traits = traits_of(source.name)
    return SourceTraits(
        resolution=_resolution(source.resolution) or _resolution(traits.resolution),
        type=source.release_type or _name_type(source.name),
        codec=source.codec or codec_family(guess_name(source.name).get("video_codec")),
        hdr=source.hdr if source.hdr is not None else (traits.hdr or None),
        languages=source.languages,
        repack=traits.repack,
        group=traits.group,
    )


@dataclass(frozen=True)
class TrackerRelease:
    """Quello che si sa di un torrent del tracker, letto una volta dalla
    ricerca e salvato col suo risultato: il verdetto si rifà da qui."""

    types: tuple[str, ...] | None = None  # le chiavi dei profili col suo type_id (o dal nome)
    resolution: str | None = None
    codec: str | None = None
    hdr: tuple[str, ...] | None = None  # None: non si sa; () è SDR
    languages: tuple[str, ...] | None = None
    seasons: tuple[int, ...] = ()
    episodes: tuple[int, ...] = ()
    repack: bool = False
    group: str | None = None
    same_bytes: bool = False
    size_delta: float | None = None  # (sorgente - torrent) / torrent, sui video se il tracker li elenca
    seeders: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TrackerRelease":
        known = {f.name for f in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        for key in ("types", "hdr", "languages"):
            if values.get(key) is not None:
                values[key] = tuple(values[key])
        for key in ("seasons", "episodes"):
            values[key] = tuple(values.get(key) or ())
        return cls(**values)


def type_keys(type_map: dict | None) -> dict[int, tuple[str, ...]]:
    """type_id del tracker -> le chiavi dei profili che ci vanno (la tabella
    type_id del profilo, al contrario: più tipi possono avere lo stesso id,
    e lì sono lo stesso slot)."""
    out: dict[int, list[str]] = {}
    for key, value in (type_map or {}).items():
        if isinstance(value, int) and not isinstance(value, bool):
            out.setdefault(value, []).append(str(key))
    return {value: tuple(sorted(keys)) for value, keys in out.items()}


def _sizes(candidate: TorrentCandidate, source: SourceSummary) -> tuple[bool, float | None]:
    same_bytes = candidate.size_bytes == source.total_size_bytes
    their_videos = sorted(size for name, size in (candidate.file_sizes or {}).items() if is_video(name))
    if not same_bytes and their_videos:
        same_bytes = their_videos == sorted(source.video_sizes)
    if their_videos and source.video_sizes:
        reference, mine = sum(their_videos), sum(source.video_sizes)
    else:
        reference, mine = candidate.size_bytes, source.total_size_bytes
    delta = round((mine - reference) / reference, 4) if isinstance(reference, int) and reference > 0 else None
    return same_bytes, delta


def release_of(
    candidate: TorrentCandidate, source: SourceSummary, keys: dict[int, tuple[str, ...]] | None = None
) -> TrackerRelease:
    from nazgarr.upload import naming  # import qui: naming importa questo modulo

    traits = traits_of(candidate.name)
    # getattr: un plugin può restituire candidati senza i campi aggiunti dopo (docs/SDK.md).
    summary = getattr(candidate, "media_info_summary", None) or {}
    video = summary.get("video") if isinstance(summary.get("video"), dict) else None
    type_id = getattr(candidate, "type_id", None)
    types = (keys or {}).get(type_id) if type_id is not None else None
    if not types:
        named = _name_type(candidate.name)
        types = (named,) if named else None
    if video and video.get("format"):
        codec = codec_family(video["format"])
    else:
        codec = codec_family(guess_name(candidate.name).get("video_codec"))
    if video:
        hdr = tuple(sorted(hdr_family(naming.mi_hdr(video))))
    else:
        hdr = tuple(sorted(traits.hdr)) if traits.hdr else None  # un nome senza HDR non dice SDR
    languages = audio_languages(summary.get("audio")) if summary else None
    same_bytes, delta = _sizes(candidate, source)
    return TrackerRelease(
        types=types,
        resolution=_resolution(getattr(candidate, "resolution", None)) or _resolution(traits.resolution),
        codec=codec,
        hdr=hdr,
        languages=tuple(sorted(languages)) if languages is not None else None,
        seasons=tuple(sorted(traits.seasons)),
        episodes=tuple(sorted(traits.episodes)),
        repack=traits.repack,
        group=traits.group,
        same_bytes=same_bytes,
        size_delta=delta,
        seeders=getattr(candidate, "seeders", None),
    )


def _coverage(source: SourceSummary, release: TrackerRelease) -> tuple[str, str | None]:
    """(equal | different | unknown, motivo): la release sul tracker copre lo
    stesso contenuto (stagione/episodio)?"""
    if source.kind == "movie":
        return "equal", None
    seasons, episodes = set(release.seasons), set(release.episodes)
    if not seasons:
        return "unknown", None
    if not seasons & source.seasons:
        return "different", "season"
    if source.kind == "episode":
        if episodes:
            return ("equal", None) if source.episode in episodes else ("different", "episode")
        # Un season pack della stessa stagione contiene l'episodio: dupe anche
        # così (decisione dell'utente, 2026-11-07), e a un'altra risoluzione è
        # comunque meglio la stagione intera.
        return "equal", "covered_by_pack"
    # Un pack contro un singolo episodio: non sono lo stesso posto.
    if episodes:
        return "different", "single_episode"
    if source.kind == "complete_pack" and seasons != source.seasons:
        return "different", "season"
    return "equal", None


def _compare(mine, theirs) -> str:
    if mine is None or theirs is None:
        return "unknown"
    return "equal" if mine == theirs else "different"


def judge(release: TrackerRelease, source: SourceSummary, mine: SourceTraits,
          threshold: int = DEFAULT_THRESHOLD) -> dict:
    """{"verdict", "score", "reasons", "unknown"} di un torrent del tracker."""
    if release.same_bytes:
        return {"verdict": "identical", "score": None, "reasons": [], "unknown": []}
    states: dict[str, str] = {}
    coverage, note = _coverage(source, release)
    states["coverage"] = coverage
    states["resolution"] = _compare(mine.resolution, release.resolution)
    states["type"] = ("unknown" if not mine.type or not release.types
                      else "equal" if mine.type in release.types else "different")
    states["codec"] = _compare(mine.codec, release.codec)
    states["hdr"] = _compare(mine.hdr, frozenset(release.hdr) if release.hdr is not None else None)
    states["languages"] = _compare(mine.languages,
                                   frozenset(release.languages) if release.languages is not None else None)
    unknown = [trait for trait in TRAITS if states[trait] == "unknown"]
    reasons = [note if trait == "coverage" else trait for trait in TRAITS if states[trait] == "different"]
    if reasons:
        if note == "covered_by_pack":
            reasons.append(note)
        return {"verdict": "different", "score": None, "reasons": reasons, "unknown": unknown}
    if note:
        reasons.append(note)
    # Un repack dello stesso gruppo sostituisce la release, non la duplica.
    if mine.repack and not release.repack and mine.group and mine.group == release.group:
        return {"verdict": "different", "score": None, "reasons": [*reasons, "repack_of_same_group"],
                "unknown": unknown}
    counted = {trait: weight for trait, weight in WEIGHTS.items()
               if not (trait == "languages" and states[trait] == "unknown")}
    score = round(100 * sum(w for trait, w in counted.items() if states[trait] == "equal") / sum(counted.values()))
    delta = release.size_delta
    if delta is not None and abs(delta) <= SIZE_CLOSE:
        score = min(100, score + SIZE_CLOSE_BONUS)
        reasons.append("size_close")
    gap = mine.type in ENCODE_TYPES and delta is not None and abs(delta) > ENCODE_GAP
    if gap:
        reasons.append("size_gap")
    if score < threshold:
        verdict = "possible"
    else:
        verdict = "trump" if gap else "same_slot"
    return {"verdict": verdict, "score": score, "reasons": reasons, "unknown": unknown}


def classify(
    candidate: TorrentCandidate,
    source: SourceSummary,
    mine: SourceTraits | None = None,
    keys: dict[int, tuple[str, ...]] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict:
    """Il risultato di un torrent del tracker, come si salva in dupes_json.
    mine: i tratti della sorgente, se già calcolati (check() li calcola una
    volta per tutti i candidati); keys: type_keys() del profilo del tracker."""
    mine = mine or source_traits(source)
    release = release_of(candidate, source, keys)
    return {
        "torrent_id_remote": candidate.torrent_id_remote,
        "name": candidate.name,
        "size_bytes": candidate.size_bytes,
        "seeders": release.seeders,
        "size_delta": release.size_delta,
        **judge(release, source, mine, threshold),
        # Come lo dà il tracker: chi lo salva (analysis.py) toglie la chiave
        # personale, e il full hash check o il reseed la rimettono
        # (verify.dupe_torrent, audit 2026-10-18).
        "download_link": candidate.download_link,
        "verification": None,
        "release": release.to_dict(),
    }


def blocks_upload(result: dict) -> bool:
    """Un identico, o un dupe vivo: un dupe senza seeder (morto) non ferma
    l'upload, il nuovo torrent lo sostituisce (decisione dell'utente,
    2026-11-07); nemmeno un trump o un possibile dupe."""
    return result["verdict"] == "identical" or (result["verdict"] == "same_slot" and result.get("seeders") != 0)


def suggest(results: list[dict]) -> str:
    """L'azione suggerita per un tracker: reseed se c'è un identico, skip se
    c'è un dupe vivo, se no upload."""
    if any(r["verdict"] == "identical" for r in results):
        return "reseed"
    if any(blocks_upload(r) for r in results):
        return "skip"
    return "upload"


def _ordered(results: list[dict]) -> list[dict]:
    return sorted(results, key=lambda r: (_VERDICT_ORDER.get(r["verdict"], len(VERDICTS)), -(r.get("score") or 0)))


def check(
    candidates: list[TorrentCandidate],
    source: SourceSummary,
    keys: dict[int, tuple[str, ...]] | None = None,
    threshold: int = DEFAULT_THRESHOLD,
) -> tuple[list[dict], str]:
    """(risultati classificati, azione suggerita per questo tracker)."""
    mine = source_traits(source)
    results = _ordered([classify(c, source, mine, keys, threshold) for c in candidates])
    return results, suggest(results)


def rejudge(results: list[dict], source: SourceSummary, threshold: int = DEFAULT_THRESHOLD) -> tuple[list[dict], str]:
    """I risultati salvati con un nuovo verdetto, per i tratti della sorgente
    di adesso (gli override cambiati). Quelli salvati prima di questo dupe
    check (senza "release") restano come sono; la verifica resta."""
    mine = source_traits(source)
    out = []
    for entry in results:
        raw = entry.get("release")
        if isinstance(raw, dict):
            entry = {**entry, **judge(TrackerRelease.from_dict(raw), source, mine, threshold)}
        out.append(entry)
    out = _ordered(out)
    return out, suggest(out)
