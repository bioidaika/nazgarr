"""Film o serie: la categoria di un torrent del catalogo di un tracker.

L'id TMDB di un film e quello di una serie sono due numerazioni diverse, e
la ricerca del catalogo per tmdbId restituisce i torrent di tutti e due con
lo stesso numero (movie/1911 è un film, tv/1911 una serie). La tabella delle
categorie del profilo del tracker (tracker_upload_profile.category_id_map_json,
movie/tv -> id) dice quali torrent sono dell'altro tipo; senza la tabella, o
senza la categoria del torrent, non si toglie niente (come prima)."""

import json

from sqlalchemy.orm import Session

from nazgarr.core.models import TrackerUploadProfile


def category_map(session: Session, tracker_id: int) -> dict[str, int]:
    """movie/tv -> id della categoria sul tracker, dal suo profilo ({} se non ce l'ha)."""
    profile = session.get(TrackerUploadProfile, tracker_id)
    try:
        raw = json.loads(profile.category_id_map_json or "{}") if profile is not None else {}
    except ValueError:
        return {}
    out: dict[str, int] = {}
    for key, value in (raw.items() if isinstance(raw, dict) else ()):
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def other_content_type(category_id: int | None, content_type: str | None, categories: dict[str, int]) -> bool:
    """Il torrent è nella categoria dell'altro tipo di contenuto (un film
    trovato cercando una serie, o il contrario)."""
    if category_id is None or not content_type or content_type not in categories:
        return False
    if categories[content_type] == category_id:
        return False
    return any(value == category_id for key, value in categories.items() if key != content_type)
