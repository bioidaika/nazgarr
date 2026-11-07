"""Quello che il matching salva a ogni ricerca (audit 2026-10-18): i torrent
che non riguardano il file cercato non diventano righe di audit, e una nuova
ricerca aggiorna le righe di prima invece di aggiungerne altre. Filesystem
vero in tmp_path, nessuna chiamata di rete."""

import hashlib
from datetime import UTC, datetime

from nazgarr.adapters.tracker.base import TorrentCandidate
from nazgarr.core.models import Candidate, MatchReview, SeedFile
from nazgarr.reseed import matching, pipeline
from tests.test_season_pack import (
    E01,
    E02,
    FOLDER,
    PACK_FILES,
    PIECE,
    PackTracker,
    _bencode,
    _library,
    _no_mediainfo,
    _pack_candidate,
    _pack_torrent,
)


def _single(remote: str, name: str, size: int) -> TorrentCandidate:
    return TorrentCandidate(
        torrent_id_remote=remote, info_hash=None, name=name, size_bytes=size, file_list=[name],
        mediainfo_unique_id=None, folder=None, download_link=None, file_sizes={name: size},
    )


def test_singles_of_other_episodes_are_not_stored(db_session, tmp_path, monkeypatch):
    # La ricerca è per tmdb_id della serie: ogni singolo di un altro episodio
    # finiva abbinato all'episodio cercato, a confidence 0, per ogni episodio.
    _no_mediainfo(monkeypatch)
    _disk, tracker, _files = _library(db_session, tmp_path)
    others = [_single(str(100 + n), f"Show.S01E{n:02d}.1080p-GRP.mkv", 1000 + n) for n in range(3, 9)]
    other_release = _single("200", "Show.S01E01.720p-OTHER.mkv", 999)

    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([*others, other_release]))

    # Solo l'altra release dello stesso episodio resta, per E01: per E02 è un altro episodio.
    rows = db_session.query(Candidate).all()
    assert [(c.torrent_id_remote, c.ambiguity_reason) for c in rows] == [("200", "size_mismatch")]


def test_a_double_episode_file_with_the_episode_is_still_evaluated(db_session, tmp_path, monkeypatch):
    _no_mediainfo(monkeypatch)
    _disk, tracker, (e01, e02) = _library(db_session, tmp_path)

    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("300", "Show.S01E01E02.1080p-GRP.mkv", 5000)]))

    assert {c.media_item_id for c in db_session.query(Candidate)} == {e01.media_item_id, e02.media_item_id}


def test_a_single_with_the_same_size_is_left_to_the_pieces(db_session, tmp_path, monkeypatch):
    # A size uguale non si scarta mai sul nome: lo decidono i piece hash.
    _no_mediainfo(monkeypatch)
    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)

    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("400", "Show.S01E05.1080p-GRP.mkv", len(E01))]))

    assert db_session.query(Candidate).filter_by(media_item_id=e01.media_item_id).count() == 1


def test_a_torrent_with_unsafe_names_in_the_catalog_is_ignored(db_session, tmp_path, monkeypatch):
    _no_mediainfo(monkeypatch)
    _disk, tracker, _files = _library(db_session, tmp_path)
    hostile = TorrentCandidate(
        torrent_id_remote="666", info_hash=None, name="x", size_bytes=len(E01),
        file_list=["../../Show.S01E01.1080p-GRP.mkv"], mediainfo_unique_id=None, folder=None, download_link=None,
        file_sizes={"../../Show.S01E01.1080p-GRP.mkv": len(E01)},
    )

    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([hostile]))

    assert db_session.query(Candidate).count() == 0


def test_a_movie_with_the_same_tmdb_id_as_the_series_is_ignored(db_session, tmp_path, monkeypatch):
    """movie/X e tv/X sono numerazioni diverse: la ricerca per tmdbId le
    mescola, le categorie del profilo del tracker le separano."""
    from nazgarr.upload import profiles as upload_profiles

    _no_mediainfo(monkeypatch)
    _disk, tracker, _files = _library(db_session, tmp_path)
    upload_profiles.restore_upload_profile(db_session, upload_profiles.create_upload_profile(db_session, tracker, None),
                                           "itt")
    film = TorrentCandidate(
        torrent_id_remote="500", info_hash=None, name="Show.S01E01.720p-OTHER.mkv", size_bytes=999,
        file_list=["Show.S01E01.720p-OTHER.mkv"], mediainfo_unique_id=None, folder=None, download_link=None,
        file_sizes={"Show.S01E01.720p-OTHER.mkv": 999}, category_id=1,
    )

    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([film]))

    assert db_session.query(Candidate).count() == 0


OTHER_FOLDER = "Show.S01.1080p-OTHER"
OTHER_FILES = {name.replace("GRP", "OTHER"): content for name, content in PACK_FILES.items()}


def _other_pack_torrent() -> bytes:
    stream = b"".join(OTHER_FILES.values())
    pieces = b"".join(hashlib.sha1(stream[i : i + PIECE]).digest() for i in range(0, len(stream), PIECE))
    return _bencode({"info": {
        "name": OTHER_FOLDER, "piece length": PIECE, "pieces": pieces,
        "files": [{"length": len(c), "path": [n]} for n, c in OTHER_FILES.items()],
    }})


def test_a_pack_without_the_seed_file_does_not_hide_it_from_its_own_files(db_session, tmp_path, monkeypatch):
    # torrent -> client: due cartelle di pack su disco, nessuna seguita da un
    # client. Il pack B valutato dal primo file di A finiva in evaluated_packs
    # ("parziale"): i file di B lo saltavano e non tornavano mai in seed.
    _no_mediainfo(monkeypatch)
    disk, tracker, (e01, e02) = _library(db_session, tmp_path, with_srt=False)
    run = pipeline.start_run(db_session, "manual")
    inode = 100
    for folder, files in ((FOLDER, PACK_FILES), (OTHER_FOLDER, OTHER_FILES)):
        pack_dir = tmp_path / "torrents" / "tv" / folder
        pack_dir.mkdir(parents=True)
        for name, content in files.items():
            (pack_dir / name).write_bytes(content)
            linked = e01.id if content is E01 else e02.id if content is E02 else None
            inode += 1
            db_session.add(SeedFile(
                disk_id=disk.id, relative_path=f"torrents/tv/{folder}/{name}", size_bytes=len(content), st_dev=1,
                inode=inode, media_file_id=linked, last_scan_id=run.id, last_seen_at=datetime.now(UTC),
            ))
    db_session.commit()
    other = TorrentCandidate(
        torrent_id_remote="901", info_hash=None, name="Show S01 1080p OTHER",
        size_bytes=sum(map(len, OTHER_FILES.values())), file_list=list(OTHER_FILES), mediainfo_unique_id=None,
        folder=OTHER_FOLDER, download_link="https://t.example/torrent/download/901.pk",
        file_sizes={n: len(c) for n, c in OTHER_FILES.items()},
    )

    class TwoPacks(PackTracker):
        def download_torrent(self, url):
            self.downloads.append(url)
            return _other_pack_torrent() if "901" in url else _pack_torrent()

    matching.run_torrent_to_client_matching(db_session, tracker, TwoPacks([_pack_candidate(), other]))

    reviewed = {r.candidate.torrent_id_remote for r in db_session.query(MatchReview)}
    assert reviewed == {"900", "901"}
    # Nessuna riga per il pack valutato da un file che non ne fa parte.
    assert db_session.query(Candidate).count() == 2


def test_a_new_search_drops_what_no_longer_concerns_the_file(db_session, tmp_path, monkeypatch):
    # Righe salvate da una ricerca di prima (i singoli degli altri episodi,
    # un torrent tolto dal tracker): la ricerca dopo le toglie, ma non quelle
    # che una review usa.
    from nazgarr.core import settings_repo

    _no_mediainfo(monkeypatch)
    settings_repo.set_setting(db_session, "rematch_interval_days", "0")
    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    others = [_single(str(100 + n), f"Show.S01E{n:02d}.1080p-GRP.mkv", 1000 + n) for n in range(3, 6)]
    gone = _single("500", "Show.S01E01.WEB-GONE.mkv", 998)
    with monkeypatch.context() as old:
        old.setattr(matching, "_unrelated", lambda *a: False)  # come prima del fix
        matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([*others, gone]))
    assert db_session.query(Candidate).filter_by(media_item_id=e01.media_item_id).count() == 4

    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([]))

    assert db_session.query(Candidate).count() == 0


def test_an_unexpected_torrent_error_keeps_the_other_candidates_and_the_attempt(db_session, tmp_path, monkeypatch):
    # Un errore di un tipo inatteso al download del .torrent usciva dal file:
    # tutti i suoi candidati annullati, la ricerca non registrata, lo stesso
    # link riscaricato per ogni file (audit 2026-10-18, finding 19).
    from nazgarr.core.models import MatchAttempt

    _no_mediainfo(monkeypatch)
    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    broken = _single("700", "Show.S01E01.1080p-BROKEN.mkv", len(E01))
    broken.download_link = "https://t.example/torrent/download/700.k"
    other = _single("701", "Show.S01E01.720p-OTHER.mkv", 999)

    class Broken(PackTracker):
        def download_torrent(self, url):
            self.downloads.append(url)
            raise AttributeError("plugin bug")

    adapter = Broken([broken, other])
    totals = matching.run_media_to_torrent_matching(db_session, tracker, adapter)

    assert totals["failed"] == 0
    assert {c.torrent_id_remote for c in db_session.query(Candidate).filter_by(media_item_id=e01.media_item_id)} \
        == {"700", "701"}
    assert db_session.query(MatchAttempt).filter_by(media_file_id=e01.id).count() == 1
    assert adapter.downloads == [broken.download_link]  # una volta sola nella run


def test_a_targeted_search_reads_only_its_files_and_their_disk(db_session, tmp_path, monkeypatch):
    # "Cerca ora" su un contenuto: prima orfani e indici dei file locali di
    # tutta la libreria, per ogni tracker (audit 2026-10-18, finding 34).
    from nazgarr.torrents.layout import LocalFiles

    _no_mediainfo(monkeypatch)
    disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    loads = []
    original = LocalFiles.load.__func__
    monkeypatch.setattr(LocalFiles, "load", classmethod(lambda cls, s, disk_ids=None: (
        loads.append(disk_ids), original(cls, s, disk_ids))[1]))

    assert [mf.id for mf in matching.orphan_media_files(db_session, tracker=tracker, media_file_ids={e01.id})] \
        == [e01.id]
    totals = matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_pack_candidate()]), only_media_file_ids={e01.id}, force=True)

    assert totals["files"] == 1
    assert loads == [{disk.id}]


def test_a_run_builds_the_local_file_indexes_once_for_every_tracker(db_session, tmp_path, monkeypatch):
    from nazgarr.core.models import Tracker
    from nazgarr.integrations import adapter_factory
    from nazgarr.torrents.layout import LocalFiles

    _no_mediainfo(monkeypatch)
    _library(db_session, tmp_path)
    db_session.add(Tracker(label="t2", adapter_type="unit3d", base_url="https://t2.example", api_token="x"))
    db_session.commit()
    loads = []
    original = LocalFiles.load.__func__
    monkeypatch.setattr(LocalFiles, "load", classmethod(lambda cls, s, disk_ids=None: (
        loads.append(disk_ids), original(cls, s, disk_ids))[1]))
    monkeypatch.setattr(adapter_factory, "build_tracker_adapter", lambda row: PackTracker([_pack_candidate()]))

    run = pipeline.start_run(db_session, "manual")
    pipeline.run_bulk_import(db_session, run, str(tmp_path / "data"))

    assert loads == [None]


def test_orphans_are_searched_by_series_season_and_episode():
    from types import SimpleNamespace as NS

    def orphan(oid, tmdb, season, episode):
        return NS(id=oid, media_item=NS(tmdb_id=tmdb, season_number=season, episode_number=episode))

    orphans = [orphan(1, 20, 1, 3), orphan(2, 10, 2, 1), orphan(3, 20, 1, 1), orphan(4, 10, 1, 9),
               orphan(5, 30, None, None)]
    ordered = sorted(orphans, key=lambda o: matching._series_order(o, "media"))
    assert [o.id for o in ordered] == [4, 2, 3, 1, 5]


def test_the_same_torrent_on_two_trackers_reads_the_pieces_once_per_run(db_session, tmp_path, monkeypatch):
    # I piece verificati si tenevano per contesto, con id() dell'oggetto
    # .torrent come chiave: si rileggevano per ogni tracker (audit
    # 2026-10-18, finding 104 e 130).
    from nazgarr.core.models import Tracker
    from nazgarr.torrents import layout as torrent_layout

    _no_mediainfo(monkeypatch)
    _disk, first, _files = _library(db_session, tmp_path)
    second = Tracker(label="t2", adapter_type="unit3d", base_url="https://t2.example", api_token="x")
    db_session.add(second)
    db_session.commit()
    reads = []
    original = torrent_layout.verify_file_pieces
    monkeypatch.setattr(torrent_layout, "verify_file_pieces", lambda *a, **k: reads.append(a[0]) or original(*a, **k))
    shared = matching.SharedLocalFiles(db_session)

    for tracker in (first, second):
        matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([_pack_candidate()]), local=shared)

    assert db_session.query(Candidate).count() == 2  # una valutazione per tracker
    assert len(reads) == len(set(reads)) == 2  # ogni episodio del pack letto una volta


def test_the_parsed_torrents_kept_in_a_pass_are_bounded(db_session, monkeypatch):
    from nazgarr.core.models import Tracker

    monkeypatch.setattr(matching, "MAX_PARSED_TORRENTS", 2)

    class Many:
        def __init__(self):
            self.downloads = []

        def download_torrent(self, url):
            self.downloads.append(url)
            if url.endswith("bad"):
                raise AttributeError("plugin bug")
            return _pack_torrent()

    adapter = Many()
    ctx = matching.MatchContext(db_session, Tracker(id=1, label="t"), adapter, "media_to_torrent")
    for url in ("a", "b", "c", "a", "bad", "bad"):
        ctx.fetch_torrent(url)

    assert list(ctx._torrents) == ["c", "a"]
    assert adapter.downloads == ["a", "b", "c", "a", "bad"]  # un link che non va si prova una volta sola


def test_a_torrent_with_a_huge_piece_length_is_never_read_against_the_library(db_session, tmp_path, monkeypatch):
    # Un .torrent con un piece grande quanto il file faceva leggere in memoria
    # tutto il video: il parser lo rifiuta, e il matching non legge niente
    # (audit 2026-10-18, finding 3, lato layout._check_pieces).
    from nazgarr.torrents import layout as torrent_layout

    _no_mediainfo(monkeypatch)
    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    hostile = _bencode({"info": {"name": "Show.S01E01.1080p-GRP.mkv", "piece length": 2 ** 40,
                                 "length": len(E01), "pieces": b"\x00" * 20}})
    single = _single("800", "Show.S01E01.1080p-GRP.mkv", len(E01))
    single.download_link = "https://t.example/torrent/download/800.k"

    class Hostile(PackTracker):
        def download_torrent(self, url):
            self.downloads.append(url)
            return hostile

    reads = []
    monkeypatch.setattr(torrent_layout, "verify_file_pieces", lambda *a, **k: reads.append(a) or None)
    matching.run_media_to_torrent_matching(db_session, tracker, Hostile([single]))

    assert reads == []
    candidate = db_session.query(Candidate).filter_by(media_item_id=e01.media_item_id).one()
    assert candidate.piece_length is None and candidate.info_hash is None  # resta la forma del catalogo


def test_a_review_on_a_torrent_the_tracker_no_longer_returns_leaves_the_queue(db_session, tmp_path, monkeypatch):
    # La nuova ricerca restituisce solo singoli di altri episodi, scartati
    # prima di valutarli: senza candidati, la review sul torrent sparito
    # restava in coda per sempre (ed eseguita, con l'esecuzione automatica).
    from nazgarr.core import settings_repo

    _no_mediainfo(monkeypatch)
    settings_repo.set_setting(db_session, "rematch_interval_days", "0")
    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("500", "Show.S01E01.WEB-GONE.mkv", len(E01))]))
    (queued,) = db_session.query(MatchReview).filter_by(media_file_id=e01.id).all()
    assert queued.status in ("pending", "auto_approved")

    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("103", "Show.S01E03.1080p-GRP.mkv", 1003)]))

    db_session.refresh(queued)
    assert (queued.status, queued.decided_by) == ("rejected", "system")


def test_a_review_on_a_pack_another_episode_evaluated_stays(db_session, tmp_path, monkeypatch):
    # Senza candidati perché il pack l'ha già valutato un altro episodio in
    # questa run: la review su quel pack resta (il tracker lo restituisce).
    from nazgarr.reseed import review

    _disk, tracker, (e01, _e02) = _library(db_session, tmp_path)
    pack = Candidate(media_item_id=e01.media_item_id, tracker_id=tracker.id, torrent_id_remote="900", name="p",
                     size_bytes=1, source="catalog_search", direction="media_to_torrent", confidence=0.9)
    db_session.add(pack)
    db_session.commit()
    queued = MatchReview(candidate_id=pack.id, media_file_id=e01.id, status="pending")
    db_session.add(queued)
    db_session.commit()

    review.create_review_for_media_file(db_session, e01, [], searched=review.CatalogSearch(tracker.id,
                                                                                         frozenset({"900"})))
    assert queued.status == "pending"
    review.create_review_for_media_file(db_session, e01, [], searched=None)  # solo la history
    assert queued.status == "pending"

    review.create_review_for_media_file(db_session, e01, [], searched=review.CatalogSearch(tracker.id,
                                                                                         frozenset({"103"})))
    assert (queued.status, queued.decided_by) == ("rejected", "system")


def test_a_queued_review_follows_a_pack_whose_mapping_changed(db_session, tmp_path, monkeypatch):
    # Stesso pack, stesso esito, ma ora il sottotitolo c'è anche in libreria:
    # la review in coda puntava ancora alla valutazione vecchia, e il
    # controllo completo e l'esecuzione usavano quella (audit 2026-10-18).
    from nazgarr.core import settings_repo
    from nazgarr.core.models import MediaFile
    from tests.test_season_pack import SRT

    _no_mediainfo(monkeypatch)
    settings_repo.set_setting(db_session, "rematch_interval_days", "0")
    disk, tracker, (e01, _e02) = _library(db_session, tmp_path, with_srt=False)
    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([_pack_candidate()]))
    (first,) = db_session.query(MatchReview).filter_by(media_file_id=e01.id).all()
    status = first.status
    assert all(f.media_file_id is None for f in first.candidate.files if f.torrent_path.endswith(".srt"))

    srt_name = "Show - S01E01 - Pilot.en.srt"
    (tmp_path / "media" / "tv" / "Show" / "Season 01" / srt_name).write_bytes(SRT)
    db_session.add(MediaFile(disk_id=disk.id, relative_path=f"media/tv/Show/Season 01/{srt_name}",
                             size_bytes=len(SRT), st_dev=1, inode=50, last_scan_id=e01.last_scan_id,
                             last_seen_at=datetime.now(UTC)))
    db_session.commit()
    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([_pack_candidate()]))

    db_session.refresh(first)
    (current,) = db_session.query(MatchReview).filter(MatchReview.media_file_id == e01.id,
                                                     MatchReview.status != "rejected").all()
    assert (first.status, first.decided_by) == ("rejected", "system")
    assert current.status == status and current.candidate_id != first.candidate_id
    assert any(f.media_file_id is not None for f in current.candidate.files if f.torrent_path.endswith(".srt"))


def test_dropped_rows_leave_the_session(db_session, tmp_path, monkeypatch):
    # La DELETE delle righe vecchie lasciava i loro oggetti nella sessione:
    # SQLite riusa l'id più alto, e la riga nuova dello stesso passaggio
    # trovava l'id già occupato (SAWarning a ogni run).
    import warnings

    from sqlalchemy.exc import SAWarning

    from nazgarr.core import settings_repo

    _no_mediainfo(monkeypatch)
    settings_repo.set_setting(db_session, "rematch_interval_days", "0")
    _disk, tracker, (e01, e02) = _library(db_session, tmp_path)
    matching.run_media_to_torrent_matching(db_session, tracker, PackTracker([_single("500", "Show.S01E01.WEB.mkv",
                                                                                     998)]),
                                           only_media_file_ids={e01.id})
    (stale,) = db_session.query(Candidate).all()
    stale_id = stale.id
    del stale

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        matching.run_media_to_torrent_matching(
            db_session, tracker, PackTracker([_single("600", "Show.S01E02.720p-OTHER.mkv", 997)]))

    (row,) = db_session.query(Candidate).all()
    assert (row.torrent_id_remote, row.media_item_id) == ("600", e02.media_item_id)
    assert row.id == stale_id  # l'id riusato, senza oggetti vecchi in mezzo


def test_a_single_of_an_episode_in_the_library_asks_no_episode_orders(db_session, tmp_path, monkeypatch):
    # Per un singolo di un altro episodio che la libreria ha, i numeri bastano:
    # prima ogni serie con un orfano costava le chiamate a TMDB e Sonarr per
    # gli ordinamenti, a ogni run, solo per non salvare una riga a confidence 0.
    from nazgarr.library import episode_orders

    _no_mediainfo(monkeypatch)
    _disk, tracker, _files = _library(db_session, tmp_path)
    asked = []
    monkeypatch.setattr(episode_orders.Translator, "orders",
                        lambda self, tmdb_id, library=None: asked.append(tmdb_id) or [])

    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("601", "Show.S01E02.720p-OTHER.mkv", 997)]))
    assert asked == []
    assert db_session.query(Candidate).count() == 1  # solo per E02, l'episodio del singolo

    # Un episodio che la libreria non ha può essere numerato con un altro ordinamento.
    matching.run_media_to_torrent_matching(
        db_session, tracker, PackTracker([_single("609", "Show.S01E09.720p-OTHER.mkv", 997)]), force=True)
    assert asked
