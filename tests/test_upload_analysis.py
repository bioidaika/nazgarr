import json
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from nazgarr.adapters.tracker.base import TorrentCandidate, Unit3dTrackerAdapter
from nazgarr.core.models import ClientTorrent, ClientTorrentFile, RunLog, SeedFile, UploadJob
from nazgarr.integrations.arr import ArrGrab
from nazgarr.torrents import create as torrent_create
from nazgarr.upload import analysis as upload_analysis
from nazgarr.upload import jobs as upload_jobs
from nazgarr.upload import verify as upload_verify
from nazgarr.upload.worker import UploadWorker
from tests.upload_helpers import InlineExecutor, make_client, make_disk, make_tracker, write_video

MB = 1024 * 1024


class _FakeTracker:
    def __init__(self, candidates=None, error=None, torrent_bytes=None):
        self.candidates = candidates or []
        self.error = error
        self.torrent_bytes = torrent_bytes
        self.downloads = []

    def search_by_tmdb(self, tmdb_id):
        if self.error:
            raise self.error
        return self.candidates

    def download_torrent(self, url):
        self.downloads.append(url)
        return self.torrent_bytes


def _candidate(name, size, tid="1"):
    return TorrentCandidate(
        torrent_id_remote=tid, info_hash=None, name=name, size_bytes=size, file_list=None,
        mediainfo_unique_id=None, download_link=f"https://t/download/{tid}.key",
    )


@pytest.fixture
def analyzing_job(db_session, tmp_path, monkeypatch):
    """Un film da 60 MB a match confermato, verso due tracker."""
    video = write_video(tmp_path / "Movie.Name.2024.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv", 60 * MB)
    disk = make_disk(db_session, tmp_path)
    make_tracker(db_session, "a")
    make_tracker(db_session, "b")
    job = upload_jobs.create_job(db_session, disk, video.name)
    upload_jobs.transition(db_session, job, "identifying", "awaiting_match", kind="movie", content_type="movie")
    upload_jobs.confirm_match(
        db_session, job, content_type="movie", tmdb_id=603, kind="movie", seasons=[], episode=None,
        details=None, forced={},
    )
    monkeypatch.setattr(upload_analysis.mediainfo_util, "extract_full_text", lambda path: f"MEDIAINFO {path}")
    return job


def _run(db_session, tmp_path, job):
    factory = lambda: db_session.__class__(bind=db_session.get_bind())  # noqa: E731
    worker = UploadWorker(
        factory, str(tmp_path), light_executor=InlineExecutor(), heavy_executor=InlineExecutor(),
        verify_executor=InlineExecutor(),
    )
    worker.kick(job.id, job.status)
    db_session.refresh(job)
    for target in job.targets:
        db_session.refresh(target)
    return worker


def test_analysis_checks_every_tracker_and_stops_at_the_decision(db_session, tmp_path, monkeypatch, analyzing_job):
    trackers = {
        "a": _FakeTracker([_candidate("Movie.Name.2024.1080p.WEB-DL.DDP5.1.H.264-OTHER", 59 * MB)]),
        "b": _FakeTracker(error=RuntimeError("503")),
    }
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: trackers[t.label])

    _run(db_session, tmp_path, analyzing_job)

    job = analyzing_job
    assert job.status == "awaiting_decision"
    assert job.mediainfo_text.startswith("MEDIAINFO ")
    a, b = job.targets
    assert (a.status, a.suggested_action) == ("awaiting_decision", "skip")
    assert json.loads(a.dupes_json)[0]["verdict"] == "same_slot"
    assert (b.status, b.suggested_action, b.error_message) == ("awaiting_decision", None, "dupe_check_failed")
    codes = [e.code for e in job.events]
    assert "dupe_check_failed" in codes and codes[-1] == "analysis_done"


def test_the_dupe_check_drops_the_other_content_type_and_follows_the_overrides(
        db_session, tmp_path, monkeypatch, analyzing_job):
    """movie/603 e tv/603 sono due cose diverse: la serie con lo stesso id
    TMDB non entra nel dupe check di un film (le categorie del profilo
    ITT). Il verdetto segue gli override: un tipo corretto a mano cambia
    lo slot, senza chiedere di nuovo al tracker."""
    from nazgarr.core.models import TrackerUploadProfile
    from nazgarr.upload import decision as upload_decision
    from nazgarr.upload import profiles as upload_profiles

    for profile in db_session.query(TrackerUploadProfile):
        upload_profiles.restore_upload_profile(db_session, profile, "itt")
    series = TorrentCandidate(
        torrent_id_remote="9", info_hash=None, name="Some Series S01 1080p WEB-DL H.264-X", size_bytes=59 * MB,
        file_list=None, mediainfo_unique_id=None, category_id=2, type_id=4, resolution="1080p", seeders=4)
    webrip = TorrentCandidate(
        torrent_id_remote="7", info_hash=None, name="Movie Name 2024 1080p ITA-X", size_bytes=55 * MB,
        file_list=None, mediainfo_unique_id=None, category_id=1, type_id=5, resolution="1080p", seeders=1)
    trackers = {"a": _FakeTracker([series, webrip]), "b": _FakeTracker([])}
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: trackers[t.label])

    _run(db_session, tmp_path, analyzing_job)

    a = analyzing_job.targets[0]
    dupes = json.loads(a.dupes_json)
    assert [(d["torrent_id_remote"], d["verdict"], d["reasons"]) for d in dupes] == [("7", "different", ["type"])]
    assert a.suggested_action == "upload"

    upload_decision.update_overrides(db_session, analyzing_job, {"type": "WEBRIP"})
    db_session.refresh(a)
    (dupe,) = json.loads(a.dupes_json)
    # Ora lo stesso tipo: niente di diverso, ma codec, HDR e lingue non si sanno (nessun MediaInfo).
    assert (dupe["verdict"], dupe["score"], dupe["unknown"]) == ("possible", 69, ["codec", "hdr", "languages"])


def test_analysis_finds_the_source_already_seeding(db_session, tmp_path, monkeypatch, analyzing_job):
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    video = analyzing_job.source_path
    st = os.stat(video)
    run = RunLog(run_type="manual", started_at=datetime.now(UTC))
    client = make_client(db_session, "qbit")
    db_session.add(run)
    db_session.commit()
    sf = SeedFile(disk_id=analyzing_job.disk_id, relative_path="torrents/x.mkv", size_bytes=st.st_size,
                  st_dev=st.st_dev, inode=st.st_ino, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    seeding = ClientTorrent(torrent_client_id=client.id, info_hash="aa", name="Seeding", save_path="/t",
                            state="uploading", tracker_url="https://tracker.one/announce/k",
                            last_polled_at=datetime.now(UTC))
    lookalike = ClientTorrent(torrent_client_id=client.id, info_hash="bb", name="Lookalike", save_path="/t",
                              state="uploading", last_polled_at=datetime.now(UTC))
    db_session.add_all([sf, seeding, lookalike])
    db_session.commit()
    db_session.add_all([
        ClientTorrentFile(client_torrent_id=seeding.id, path_in_torrent="x.mkv", size_bytes=st.st_size,
                          seed_file_id=sf.id, last_scan_id=run.id),
        ClientTorrentFile(client_torrent_id=lookalike.id, path_in_torrent="y.mkv", size_bytes=st.st_size,
                          last_scan_id=run.id),
    ])
    db_session.commit()
    grab = ArrGrab(tracker_host="tracker.one", torrent_id_remote="77", download_url="u", info_hash="aa",
                   indexer="One")

    class _Index:
        def grab_for(self, path, size):
            return grab

    monkeypatch.setattr(upload_analysis, "arr_index_if_configured", lambda session: _Index())

    _run(db_session, tmp_path, analyzing_job)

    analysis = json.loads(analyzing_job.analysis_json)
    assert [(m["name"], m["match"], m["tracker_host"]) for m in analysis["client_matches"]] == [
        ("Seeding", "hardlink", "tracker.one"), ("Lookalike", "same_size", None),
    ]
    assert analysis["arr_grabs"] == [
        {"tracker_host": "tracker.one", "torrent_id_remote": "77", "info_hash": "aa", "indexer": "One"}
    ]
    levels = {e.code: e.level for e in analyzing_job.events}
    assert levels["already_on_client"] == "warning" and levels["grabbed_from_tracker"] == "warning"


def test_full_hash_check_turns_an_identical_release_into_a_reseed(db_session, tmp_path, monkeypatch, analyzing_job):
    source = analyzing_job.source_path
    torrent_path, _ = torrent_create.create_torrent(source, "https://a.example/announce", str(tmp_path / "t.torrent"))
    with open(torrent_path, "rb") as f:
        torrent_bytes = f.read()
    same = _FakeTracker([_candidate("Movie.Name.2024.1080p.WEB-DL-GRP", os.path.getsize(source), "42")],
                        torrent_bytes=torrent_bytes)
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: same)
    monkeypatch.setattr(upload_verify.adapter_factory, "build_tracker_adapter", lambda t: same)
    worker = _run(db_session, tmp_path, analyzing_job)
    target = analyzing_job.targets[0]
    assert target.suggested_action == "reseed"  # stessi byte: suggerito, ma non ancora verificato

    upload_verify.start(db_session, analyzing_job, target, "42", worker)

    db_session.refresh(target)
    verification = json.loads(target.dupes_json)[0]["verification"]
    assert verification["status"] == "passed"
    assert verification["ok"] == verification["pieces"] > 0
    # Cosa ha letto (2026-10-21): il reseed non rilegge questi file per la
    # tolleranza degli extra mancanti finché sono gli stessi.
    st = os.stat(source)
    assert verification["files"] == {os.path.basename(source): [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]}
    assert (target.status, target.reseed_torrent_id) == ("awaiting_decision", "42")
    assert same.downloads == ["https://t/download/42.key"]


def test_full_hash_check_fails_on_different_bytes(db_session, tmp_path, monkeypatch, analyzing_job):
    other = write_video(tmp_path / "other" / "Movie.Name.2024.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv", 60 * MB)
    with open(other, "r+b") as f:
        f.write(b"y" * 1000)
    torrent_path, _ = torrent_create.create_torrent(
        str(other), "https://a.example/announce", str(tmp_path / "t.torrent")
    )
    with open(torrent_path, "rb") as f:
        tracker = _FakeTracker([_candidate("x", 60 * MB, "9")], torrent_bytes=f.read())
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: tracker)
    monkeypatch.setattr(upload_verify.adapter_factory, "build_tracker_adapter", lambda t: tracker)
    worker = _run(db_session, tmp_path, analyzing_job)
    target = analyzing_job.targets[0]

    upload_verify.start(db_session, analyzing_job, target, "9", worker)

    db_session.refresh(target)
    verification = json.loads(target.dupes_json)[0]["verification"]
    assert verification["status"] == "failed" and verification["mismatched"] >= 1
    assert "files" not in verification  # un controllo fallito non verifica niente
    assert target.reseed_torrent_id is None
    assert analyzing_job.events[-1].code == "verify_failed"


def test_verify_requires_a_known_dupe(db_session, tmp_path, monkeypatch, analyzing_job):
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    worker = _run(db_session, tmp_path, analyzing_job)

    with pytest.raises(upload_jobs.UploadJobError) as exc:
        upload_verify.start(db_session, analyzing_job, analyzing_job.targets[0], "nope", worker)
    assert exc.value.code == "upload_dupe_not_found"


def test_interrupted_verification_goes_back_to_the_decision(db_session, tmp_path, monkeypatch, analyzing_job):
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    _run(db_session, tmp_path, analyzing_job)
    analyzing_job.targets[0].status = "verifying"
    db_session.commit()

    upload_jobs.reset_interrupted(db_session)

    db_session.refresh(analyzing_job.targets[0])
    assert analyzing_job.targets[0].status == "awaiting_decision"
    assert db_session.get(UploadJob, analyzing_job.id).events[-1].code == "verify_interrupted"


def _seed_hardlink(db_session, job, tracker_url, client):
    st = os.stat(job.source_path)
    run = RunLog(run_type="manual", started_at=datetime.now(UTC))
    db_session.add(run)
    db_session.commit()
    sf = SeedFile(disk_id=job.disk_id, relative_path="torrents/x.mkv", size_bytes=st.st_size,
                  st_dev=st.st_dev, inode=st.st_ino, last_scan_id=run.id, last_seen_at=datetime.now(UTC))
    torrent = ClientTorrent(torrent_client_id=client.id, info_hash="aa",
                            name="Movie.Name.2024.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-ORIG.mkv", save_path="/t",
                            state="uploading", tracker_url=tracker_url, last_polled_at=datetime.now(UTC))
    db_session.add_all([sf, torrent])
    db_session.commit()
    db_session.add(ClientTorrentFile(client_torrent_id=torrent.id, path_in_torrent="x.mkv", size_bytes=st.st_size,
                                     seed_file_id=sf.id, last_scan_id=run.id))
    db_session.commit()


def test_already_seeding_on_the_trackers_client_suggests_skip_and_names_come_from_it(
    db_session, tmp_path, monkeypatch, analyzing_job
):
    size = os.path.getsize(analyzing_job.source_path)
    same = _FakeTracker([_candidate("Movie.Name.2024.1080p.BluRay.REMUX-ORIG", size)])
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: same)
    client = make_client(db_session, "qbit")
    for target in analyzing_job.targets:
        target.torrent_client_id = client.id
    db_session.commit()
    _seed_hardlink(db_session, analyzing_job, "https://tracker.example/announce/key", client)

    _run(db_session, tmp_path, analyzing_job)

    a, b = analyzing_job.targets
    # Identico sul tracker, ma già in seed lì: né upload né reseed.
    assert (a.suggested_action, b.suggested_action) == ("skip", "skip")
    assert "already_seeding_here" in [e.code for e in analyzing_job.events]
    analysis = json.loads(analyzing_job.analysis_json)
    assert analysis["name_source"] == {"name": "Movie.Name.2024.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-ORIG",
                                       "origin": "hardlink"}
    assert analysis["detected"]["type"] == "REMUX" and analysis["detected"]["group"] == "ORIG"


def test_names_come_from_the_original_name_radarr_recorded(db_session, tmp_path, monkeypatch, analyzing_job):
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    from nazgarr.integrations.arr import ArrIdentity

    class _Index:
        def grab_for(self, path, size):
            return None

        def identity_for(self, path, size):
            return ArrIdentity(source="radarr", content_type="movie", tmdb_id=603,
                               scene_name="Movie.Name.2024.2160p.WEB-DL.DV.HDR.H.265-SCENE")

    monkeypatch.setattr(upload_analysis, "arr_index_if_configured", lambda session: _Index())

    _run(db_session, tmp_path, analyzing_job)

    analysis = json.loads(analyzing_job.analysis_json)
    assert analysis["name_source"]["origin"] == "radarr"
    assert (analysis["detected"]["resolution"], analysis["detected"]["group"]) == ("2160p", "SCENE")


def test_a_cancel_during_the_dupe_checks_stops_the_analysis(db_session, tmp_path, monkeypatch, analyzing_job):
    """Audit 2026-10-18: annullato durante un dupe check, l'analisi andava
    avanti sugli altri tracker e riscriveva target e proposte; ripreso
    subito, due analisi dello stesso job giravano insieme."""
    asked = []

    class Cancelling(_FakeTracker):
        def search_by_tmdb(self, tmdb_id):
            asked.append(tmdb_id)
            other = db_session.__class__(bind=db_session.get_bind())
            upload_jobs.cancel_job(other, other.get(UploadJob, analyzing_job.id))
            other.close()
            return []

    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: Cancelling())
    proposed = []
    monkeypatch.setattr(upload_analysis.upload_decision, "propose", lambda session, job: proposed.append(job.id))

    upload_analysis.handle(db_session, analyzing_job, None)

    db_session.refresh(analyzing_job)
    assert analyzing_job.status == "cancelled" and len(asked) == 1 and proposed == []
    # Nemmeno il tracker che si stava controllando si scrive: torna com'era.
    assert [t.status for t in analyzing_job.targets] == ["pending", "pending"]
    assert [t.dupes_json for t in analyzing_job.targets] == [None, None]
    assert "dupe_check_done" not in [e.code for e in analyzing_job.events]


def test_an_analysis_started_after_this_one_wins(db_session, tmp_path, monkeypatch, analyzing_job):
    asked = []

    class Superseded(_FakeTracker):
        def search_by_tmdb(self, tmdb_id):
            asked.append(tmdb_id)
            # Un'altra analisi dello stesso job parte adesso (annullato e ripreso).
            upload_jobs.log_event(db_session, analyzing_job, "analysis_started")
            db_session.commit()
            return []

    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: Superseded())

    upload_analysis.handle(db_session, analyzing_job, None)

    assert len(asked) == 1 and analyzing_job.status == "analyzing"
    codes = [e.code for e in analyzing_job.events]
    assert "analysis_done" not in codes and "dupe_check_done" not in codes


def test_a_truncated_catalogue_is_said_at_the_decision(db_session, tmp_path, monkeypatch, analyzing_job):
    """Ondata 1 (finding 51): un tracker con più risultati di quelli letti
    lo dice con CatalogResults.truncated; il dupe check non deve sembrare
    completo."""
    from nazgarr.adapters.tracker.base import CatalogResults

    many = CatalogResults([_candidate("Other.Movie.2019.720p", 1 * MB, "9")])
    many.truncated = True
    trackers = {"a": _FakeTracker(), "b": _FakeTracker()}
    trackers["b"].candidates = many
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: trackers[t.label])

    _run(db_session, tmp_path, analyzing_job)

    _a, b = analyzing_job.targets
    assert json.loads(analyzing_job.analysis_json)["dupes_truncated"] == {str(b.id): 1}
    event = next(e for e in analyzing_job.events if e.code == "dupe_check_truncated")
    assert (event.target_id, event.level, json.loads(event.params_json)) == (b.id, "warning", {"results": 1})


class _Unit3dLike(_FakeTracker):
    """Come UNIT3D: la chiave personale nei link di download, e
    rewrite_download_link che rimette quella attuale."""

    base_url = "https://a.example"
    _DOWNLOAD_KEY_RE = Unit3dTrackerAdapter._DOWNLOAD_KEY_RE

    def __init__(self, *args, rss_key=None, learns=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rss_key, self.learns = rss_key, learns

    def search_by_tmdb(self, tmdb_id):
        self.rss_key = self.learns or self.rss_key  # dalle risposte dell'API, come _learn_rss_key
        return super().search_by_tmdb(tmdb_id)

    def rewrite_download_link(self, url):
        return Unit3dTrackerAdapter.rewrite_download_link(self, url)


def test_the_download_key_never_reaches_the_db_and_comes_back_for_the_full_hash_check(
    db_session, tmp_path, monkeypatch, analyzing_job,
):
    """Audit 2026-10-18 (finding 55): il link di download di UNIT3D ha la
    chiave personale, e finiva in chiaro nel dupes_json di ogni target (e
    nella risposta dell'API). Si salva senza; la chiave imparata resta sul
    tracker, cifrata, e torna solo quando il full hash check scarica il
    .torrent."""
    from nazgarr.api.uploads import UploadTargetResponse

    source = analyzing_job.source_path
    torrent_path, _ = torrent_create.create_torrent(source, "https://a.example/announce", str(tmp_path / "t.torrent"))
    with open(torrent_path, "rb") as f:
        torrent_bytes = f.read()
    link = "https://a.example/torrent/download/77.SeCrEtKey123"
    candidate = _candidate("Movie.Name.2024.1080p.WEB-DL-GRP", os.path.getsize(source), "77")
    candidate = type(candidate)(**{**candidate.__dict__, "download_link": link})
    fake = _Unit3dLike([candidate], torrent_bytes=torrent_bytes, learns="SeCrEtKey123")

    def build(tracker):
        fake.rss_key = tracker.rss_key  # come plugins/builtin.py: la chiave salvata sul tracker
        return fake

    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", build)
    worker = _run(db_session, tmp_path, analyzing_job)
    fake.learns = None  # il full hash check non cerca: la chiave è solo quella del tracker

    target = analyzing_job.targets[0]
    assert "SeCrEtKey123" not in target.dupes_json
    assert json.loads(target.dupes_json)[0]["download_link"] == "https://a.example/torrent/download/77.rsskey"
    assert "SeCrEtKey123" not in UploadTargetResponse.from_model(target).model_dump_json()
    db_session.refresh(target.tracker)
    assert target.tracker.rss_key == "SeCrEtKey123"
    raw = db_session.execute(text("SELECT rss_key FROM tracker WHERE id = :id"), {"id": target.tracker_id}).scalar()
    assert raw and "SeCrEtKey123" not in raw  # cifrata nel DB

    upload_verify.start(db_session, analyzing_job, target, "77", worker)

    db_session.refresh(target)
    assert json.loads(target.dupes_json)[0]["verification"]["status"] == "passed"
    assert fake.downloads == [link]  # la chiave rimessa solo per scaricare


def test_a_dupe_torrent_comes_from_its_link_as_stored_or_from_the_torrent_detail():
    """verify.dupe_torrent: un link salvato prima, con la chiave dentro, o di
    un tracker che non la usa, si scarica com'è; senza una chiave nota, il
    link del dettaglio del torrent sul tracker."""
    tracker = _Unit3dLike(torrent_bytes=b"t")
    keyed = "https://a.example/torrent/download/5.OldKey"
    assert upload_verify.dupe_torrent(tracker, {"torrent_id_remote": "5", "download_link": keyed}) == b"t"
    assert upload_verify.dupe_torrent(tracker, {"download_link": "https://t/dl?id=1"}) == b"t"
    assert tracker.downloads == [keyed, "https://t/dl?id=1"]

    detail = "https://a.example/torrent/download/5.FromApi"
    tracker.get_torrent_detail = lambda torrent_id: TorrentCandidate(
        torrent_id_remote=torrent_id, info_hash=None, name="x", size_bytes=1, file_list=None,
        mediainfo_unique_id=None, download_link=detail)
    upload_verify.dupe_torrent(tracker, {"torrent_id_remote": "5",
                                         "download_link": "https://a.example/torrent/download/5.rsskey"})
    assert tracker.downloads[-1] == detail
    with pytest.raises(upload_jobs.UploadJobError):
        upload_verify.dupe_torrent(tracker, {"torrent_id_remote": "5", "download_link": None})


def test_another_live_upload_of_the_same_files_is_said_at_the_decision(db_session, tmp_path, monkeypatch,
                                                                       analyzing_job):
    """Audit 2026-10-18 (finding 163): due upload vivi sugli stessi file (una
    stagione di una cartella divisa confermata come la cartella intera, un
    upload a mano di una release della cartella osservata) arrivavano alla
    decisione senza che niente lo dicesse. Anche da un altro percorso, con
    un hardlink: gli stessi byte."""
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    disk = analyzing_job.disk
    elsewhere = tmp_path / "releases" / "Movie.Name.2024"
    elsewhere.mkdir(parents=True)
    os.link(analyzing_job.source_path, elsewhere / "movie.mkv")
    same_path = upload_jobs.create_job(db_session, disk, analyzing_job.relative_path)  # ancora da identificare
    hardlinked = upload_jobs.create_job(db_session, disk, "releases/Movie.Name.2024")
    finished = upload_jobs.create_job(db_session, disk, analyzing_job.relative_path)
    upload_jobs.cancel_job(db_session, finished)
    write_video(tmp_path / "Other.2023.mkv", 60 * MB)
    upload_jobs.create_job(db_session, disk, "Other.2023.mkv")  # altri file: niente

    _run(db_session, tmp_path, analyzing_job)

    assert analyzing_job.status == "awaiting_decision"
    others = json.loads(analyzing_job.analysis_json)["other_jobs"]
    assert [(o["id"], o["status"], o["videos"]) for o in others] == [
        (same_path.id, "identifying", 1), (hardlinked.id, "identifying", 1)]
    event = next(e for e in analyzing_job.events if e.code == "other_upload_same_files")
    assert (event.level, json.loads(event.params_json)) == (
        "warning", {"jobs": f"#{same_path.id}, #{hardlinked.id}"})


def test_the_pack_a_split_came_from_never_counts_as_another_upload(db_session, tmp_path, analyzing_job):
    child = upload_jobs.create_job(db_session, analyzing_job.disk, analyzing_job.relative_path)
    child.split_from_id = analyzing_job.id
    db_session.commit()
    files = upload_analysis.source_files(analyzing_job)

    assert upload_analysis.other_jobs(db_session, analyzing_job, files) == []
    assert upload_analysis.other_jobs(db_session, child, files) == []


def _live_pack(db_session, disk, folder):
    """Un pack di due episodi a mano, ancora da identificare."""
    from nazgarr.upload import pack as upload_pack

    root = os.path.realpath(disk.root_path)
    episodes = [write_video(folder / f"Show.S01E0{n}.1080p.WEB-DL-GRP.mkv", 60 * MB) for n in (1, 2)]
    job = upload_pack.create_job(db_session, disk, [os.path.relpath(e, root) for e in episodes])
    return job, episodes


def test_another_upload_whose_files_are_gone_never_fails_this_analysis(db_session, tmp_path, monkeypatch,
                                                                         analyzing_job):
    """Revisione wave 7 (finding 163): un pack vivo con un episodio sparito
    (o un symlink al suo posto) alzava un UploadJobError, non un ValueError,
    e l'analisi di un altro upload falliva con l'errore di quel job."""
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    pack, episodes = _live_pack(db_session, analyzing_job.disk, tmp_path / "Show")
    os.unlink(episodes[1])

    _run(db_session, tmp_path, analyzing_job)

    assert analyzing_job.status == "awaiting_decision"
    assert json.loads(analyzing_job.analysis_json)["other_jobs"] == []
    db_session.refresh(pack)
    assert pack.status == "identifying"


def test_another_upload_whose_disk_was_removed_never_fails_this_analysis(db_session, tmp_path, monkeypatch,
                                                                         analyzing_job):
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _FakeTracker())
    other_disk = make_disk(db_session, tmp_path / "other")
    pack, _episodes = _live_pack(db_session, other_disk, tmp_path / "other" / "Show")
    db_session.delete(other_disk)
    db_session.commit()
    db_session.refresh(pack)
    assert pack.disk is None

    _run(db_session, tmp_path, analyzing_job)

    assert analyzing_job.status == "awaiting_decision"
    assert json.loads(analyzing_job.analysis_json)["other_jobs"] == []
