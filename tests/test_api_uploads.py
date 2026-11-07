import json
import os

import pytest

from nazgarr.adapters.media_resolver.base import ResolvedMedia
from nazgarr.api import metadata as metadata_api
from nazgarr.core import settings_repo
from nazgarr.core.models import UploadJob
from nazgarr.integrations import adapter_factory
from nazgarr.library.poster_cache import poster_file
from nazgarr.upload import analysis as upload_analysis
from nazgarr.upload import identify as upload_identify
from nazgarr.upload.worker import UploadWorker
from tests.upload_helpers import (
    FakeTMDB,
    InlineExecutor,
    make_client,
    make_disk,
    make_tracker,
    tmdb_result,
    write_video,
)


class _FakeResolver:
    SOURCE = "filename_parser"

    def __init__(self, resolved=None):
        self.resolved = resolved
        self.calls = []

    def resolve(self, file_path):
        self.calls.append(file_path)
        return self.resolved


class _NoDupes:
    def search_by_tmdb(self, tmdb_id):
        return []


@pytest.fixture
def setup(client, tmp_path, monkeypatch):
    """Disco, un tracker con profilo e un worker che gira nel thread del test."""
    session = client.app.state.session_factory()
    disk = make_disk(session, tmp_path)
    tracker = make_tracker(session)
    # Il match automatico spento: questi test provano il punto di match.
    settings_repo.set_setting(session, upload_identify.AUTO_MATCH_SETTING, "0")
    ids = disk.id, tracker.id
    session.close()
    client.app.state.upload_worker = UploadWorker(
        client.app.state.session_factory, str(tmp_path / "data"),
        light_executor=InlineExecutor(), heavy_executor=InlineExecutor(),
    )
    resolver = _FakeResolver()
    monkeypatch.setattr(adapter_factory, "build_media_resolver", lambda session, arr_index=None: resolver)
    # Mai una chiamata vera verso un tracker o mediainfo sui file finti.
    monkeypatch.setattr(upload_analysis.adapter_factory, "build_tracker_adapter", lambda t: _NoDupes())
    monkeypatch.setattr(upload_analysis.mediainfo_util, "extract_full_text", lambda path: "MI")
    return {"disk_id": ids[0], "tracker_id": ids[1], "resolver": resolver}


def test_create_movie_upload_identifies_and_stops_at_the_match_gate(client, tmp_path, setup):
    video = write_video(tmp_path / "media" / "Movie.Name.2024.1080p.WEB.mkv")
    setup["resolver"].resolved = ResolvedMedia(
        tmdb_id=603, content_type="movie", title="Movie Name", year=2024, poster_path="/p.jpg"
    )

    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "media/" + video.name})

    assert resp.status_code == 201, resp.text
    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert detail["status"] == "awaiting_match"
    assert detail["kind"] == "movie"
    assert [(c["tmdb_id"], c["title"], c["source"]) for c in detail["candidates"]] == [
        (603, "Movie Name", "filename_parser")
    ]
    assert setup["resolver"].calls == [str(video)]
    assert [t["tracker_id"] for t in detail["targets"]] == [setup["tracker_id"]]
    assert [e["code"] for e in detail["events"]] == ["job_created", "identify_started", "identify_done"]


def test_create_season_pack_folder(client, tmp_path, setup):
    for ep in (1, 2):
        write_video(tmp_path / "Show.S01.1080p-GRP" / f"Show.S01E0{ep}.1080p-GRP.mkv")

    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Show.S01.1080p-GRP"})

    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert detail["status"] == "awaiting_match"
    assert detail["is_dir"] is True
    assert detail["kind"] == "season_pack"
    assert detail["seasons"] == [1]
    assert detail["layout"]["episodes_by_season"] == {"1": [1, 2]}
    assert detail["candidates"] == []


def test_episodes_picked_by_hand_become_a_season_pack(client, tmp_path, setup):
    files = []
    for ep in (1, 2):
        name = f"Show.S01E0{ep}.1080p-GRP"
        write_video(tmp_path / "torrents" / name / f"{name}.mkv")
        files.append(f"torrents/{name}/{name}.mkv")

    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "files": files})

    assert resp.status_code == 201, resp.text
    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert (detail["origin"], detail["pack_name"], detail["pack_files"]) == ("pack", "Show.S01.1080p-GRP", files)
    assert (detail["status"], detail["kind"], detail["seasons"]) == ("awaiting_match", "season_pack", [1])
    assert detail["layout"]["episodes_by_season"] == {"1": [1, 2]}

    one = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "files": files[:1]})
    assert one.status_code == 400 and one.json()["detail"]["code"] == "upload_pack_too_few_files"


def test_folder_without_videos_fails_with_a_coded_error(client, tmp_path, setup):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("x")

    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "docs"})

    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert detail["status"] == "failed"
    assert detail["error_message"] == "no_video_files"


def test_create_rejects_path_outside_disk(client, setup):
    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "../../etc/passwd"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "path_outside_scope"


def test_create_rejects_missing_source_and_unknown_disk(client, setup):
    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "nope.mkv"})
    assert resp.json()["detail"]["code"] == "upload_source_not_found"

    resp = client.post("/api/uploads", json={"disk_id": 999, "relative_path": "x"})
    assert resp.status_code == 404


@pytest.mark.parametrize(("overrides", "code"), [
    ({"content_name": "."}, "upload_invalid_override"),
    ({"screenshot_count": 999}, "upload_invalid_override"),
    ({"file_naming": "whatever"}, "upload_invalid_override"),
    ({"no_such_override": 1}, "upload_unknown_override"),
])
def test_overrides_given_at_creation_follow_the_decision_rules(client, tmp_path, setup, overrides, code):
    write_video(tmp_path / "Movie.2024.mkv")
    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv",
                                             "overrides": overrides})
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == code
    assert client.get("/api/uploads").json() == []

    ok = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv",
                                           "overrides": {"group": " NZG ", "notes": ""}})
    assert ok.status_code == 201 and ok.json()["overrides"] == {"group": "NZG"}


def test_cancel_list_and_delete(client, tmp_path, setup):
    write_video(tmp_path / "Movie.2024.mkv")
    job_id = client.post(
        "/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv"}
    ).json()["id"]

    listed = client.get("/api/uploads").json()
    assert [j["id"] for j in listed] == [job_id]
    assert listed[0]["targets"][0]["tracker_label"] == "t"

    assert client.post(f"/api/uploads/{job_id}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/uploads/{job_id}/cancel").status_code == 400
    # Annullato mentre un tracker è ancora in invio: si aspetta che finisca.
    session = client.app.state.session_factory()
    target = session.get(UploadJob, job_id).targets[0]
    target.action, target.status = "upload", "uploading"
    session.commit()
    busy = client.delete(f"/api/uploads/{job_id}")
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "upload_job_busy"
    assert client.post(f"/api/uploads/{job_id}/resume").status_code == 409
    target.status = "failed"
    session.commit()
    session.close()
    assert client.delete(f"/api/uploads/{job_id}").status_code == 204
    assert client.get(f"/api/uploads/{job_id}").status_code == 404


class _RecordingWorker:
    def __init__(self):
        self.kicked = []

    def kick(self, job_id, status):
        self.kicked.append((job_id, status))

    def is_active(self, job_id):
        return False

    def shutdown(self):
        pass


def test_a_failed_upload_is_retried_through_the_api(client, tmp_path, setup):
    write_video(tmp_path / "Movie.2024.mkv")
    job_id = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv"}
                         ).json()["id"]
    worker = client.app.state.upload_worker = _RecordingWorker()
    session = client.app.state.session_factory()
    job = session.get(UploadJob, job_id)
    job.status = "failed"
    target = job.targets[0]
    target.action, target.status, target.error_message = "upload", "failed", "interrupted"
    session.commit()
    session.close()

    unsure = client.post(f"/api/uploads/{job_id}/retry")
    assert unsure.status_code == 409
    assert unsure.json()["detail"] == {"code": "upload_retry_needs_confirm", "params": {"trackers": "t"}}

    resp = client.post(f"/api/uploads/{job_id}/retry", json={"confirm_interrupted": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued" and body["targets"][0]["status"] == "approved"
    assert body["events"][-1]["code"] == "job_retried"
    assert worker.kicked == [(job_id, "queued")]
    assert client.post(f"/api/uploads/{job_id}/retry").status_code == 400  # in coda: niente da riprovare


def test_resolver_error_does_not_block_identification(client, tmp_path, setup):
    write_video(tmp_path / "Movie.2024.mkv")

    def boom(file_path):
        raise RuntimeError("tmdb down")

    setup["resolver"].resolve = boom
    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv"})

    assert resp.status_code == 201
    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert detail["status"] == "awaiting_match"
    assert detail["candidates"] == []



def test_a_resolver_result_of_an_unknown_type_is_not_a_candidate(client, tmp_path, setup):
    """Un resolver di un plugin che risponde con un tipo né film né serie: il
    candidato non si può confermare, e il dettaglio del job non deve fallire."""
    write_video(tmp_path / "Movie.2024.mkv")
    setup["resolver"].resolved = ResolvedMedia(tmdb_id=603, content_type="anime", title="Movie", year=2024)
    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "Movie.2024.mkv"})

    detail = client.get(f"/api/uploads/{resp.json()['id']}")
    assert detail.status_code == 200 and detail.json()["candidates"] == []

def _awaiting_match(client, tmp_path, setup, relative_path, files):
    for f in files:
        write_video(tmp_path / f)
    job_id = client.post(
        "/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": relative_path,
                              "forced_ids": {"imdb": "tt0944947", "mal": 5}},
    ).json()["id"]
    assert client.get(f"/api/uploads/{job_id}").json()["status"] == "awaiting_match"
    return job_id


def test_confirm_match_stores_ids_and_moves_on(client, tmp_path, setup, monkeypatch):
    files = ["Show.S02/Show.S02E01.mkv", "Show.S02/Show.S02E02.mkv"]
    job_id = _awaiting_match(client, tmp_path, setup, "Show.S02", files)
    fake = FakeTMDB(details={("tv", 1399): {
        **tmdb_result(1399, "Game of Thrones", 2011, "tv", "/got.jpg"), "imdb_id": "tt-from-tmdb", "tvdb_id": 121361,
    }})
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: fake)

    resp = client.post(f"/api/uploads/{job_id}/match", json={
        "content_type": "tv", "tmdb_id": 1399, "kind": "season_pack", "seasons": [2],
    })

    assert resp.status_code == 200, resp.text
    session = client.app.state.session_factory()
    try:
        job = session.get(UploadJob, job_id)
        assert (job.tmdb_id, job.imdb_id, job.tvdb_id, job.mal_id) == (1399, "tt0944947", 121361, 5)
        assert (job.title, job.year, job.poster_path, job.seasons_json) == ("Game of Thrones", 2011, "/got.jpg", "[2]")
        assert "match_confirmed" in [e.code for e in job.events]
        assert job.status == "awaiting_decision"
        assert [(t.status, t.suggested_action) for t in job.targets] == [("awaiting_decision", "upload")]
        assert job.mediainfo_text == "MI"
    finally:
        session.close()


@pytest.mark.parametrize(("body", "code"), [
    ({"content_type": "movie", "tmdb_id": 1, "kind": "season_pack", "seasons": [1]}, "upload_kind_mismatch"),
    ({"content_type": "tv", "tmdb_id": 1, "kind": "season_pack", "seasons": []}, "upload_season_required"),
    ({"content_type": "tv", "tmdb_id": 1, "kind": "season_pack", "seasons": [1, 2]}, "upload_single_season_required"),
    ({"content_type": "tv", "tmdb_id": 1, "kind": "episode", "seasons": [1]}, "upload_episode_required"),
])
def test_confirm_match_validates_kind_and_seasons(client, tmp_path, setup, body, code):
    files = ["Show.S01/Show.S01E01.mkv", "Show.S01/Show.S01E02.mkv"]
    job_id = _awaiting_match(client, tmp_path, setup, "Show.S01", files)

    resp = client.post(f"/api/uploads/{job_id}/match", json=body)

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == code


def test_packs_need_a_folder(client, tmp_path, setup):
    job_id = _awaiting_match(client, tmp_path, setup, "Show.S01E01.mkv", ["Show.S01E01.mkv"])

    resp = client.post(f"/api/uploads/{job_id}/match", json={
        "content_type": "tv", "tmdb_id": 1, "kind": "season_pack", "seasons": [1],
    })

    assert resp.json()["detail"]["code"] == "upload_pack_requires_folder"


def test_reidentify_with_forced_ids(client, tmp_path, setup, monkeypatch):
    job_id = _awaiting_match(client, tmp_path, setup, "Movie.2024.mkv", ["Movie.2024.mkv"])
    fake = FakeTMDB(details={("movie", 603): tmdb_result(603, "The Matrix", 1999)})
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: fake)

    resp = client.post(f"/api/uploads/{job_id}/reidentify", json={"forced_ids": {"tmdb": "movie/603"}})

    assert resp.status_code == 200
    detail = client.get(f"/api/uploads/{job_id}").json()
    assert detail["status"] == "awaiting_match"
    assert detail["forced_ids"] == {"tmdb": "movie/603"}
    assert [c["tmdb_id"] for c in detail["candidates"]] == [603]


def test_metadata_search_details_and_poster(client, tmp_path, monkeypatch):
    fake = FakeTMDB(
        search={("movie", "Matrix", None): [tmdb_result(603, "The Matrix", 1999, poster_path="/m.jpg")]},
        details={("movie", 603): {**tmdb_result(603, "The Matrix", 1999, poster_path="/m.jpg"), "genres": ["SF"]}},
    )
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: fake)
    downloads = []

    def fake_download(posters_dir, content_type, tmdb_id, poster_path):
        downloads.append(poster_path)
        path = poster_file(posters_dir, content_type, tmdb_id)
        os.makedirs(posters_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"jpg")
        return path

    monkeypatch.setattr(metadata_api, "download_poster", fake_download)

    assert client.get("/api/metadata/search?content_type=movie&query=Matrix").json() == [
        tmdb_result(603, "The Matrix", 1999, poster_path="/m.jpg")]
    details = client.get("/api/metadata/movie/603").json()
    # Tipizzati (audit 2026-10-18): quello che TMDB non ha dato c'è lo stesso, vuoto.
    assert (details["genres"], details["cast"], details["seasons"], details["runtime"]) == (["SF"], [], [], None)
    assert client.get("/api/metadata/search?content_type=book&query=x").status_code == 400

    # Con il path dal candidato non serve chiedere i dettagli; un path che
    # non è un nome file TMDB viene ignorato.
    assert client.get("/api/metadata/posters/movie/603.jpg?path=/m.jpg").content == b"jpg"
    assert client.get("/api/metadata/posters/movie/603.jpg").status_code == 200  # dalla cache
    fake.details[("tv", 603)] = tmdb_result(603, "Other", 2000, "tv", poster_path=None)
    resp = client.get("/api/metadata/posters/tv/603.jpg?path=http://evil/x.jpg")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "poster_not_available"
    assert ("details", "tv", 603) in fake.calls
    assert downloads == ["/m.jpg"]


def test_metadata_without_tmdb_key(client):
    resp = client.get("/api/metadata/search?content_type=movie&query=x")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "tmdb_api_key_missing"


def test_upload_trackers_lists_only_trackers_with_a_profile_and_their_client(client):
    session = client.app.state.session_factory()
    try:
        qbit_id = make_client(session, "qbit").id
        tracker_id = make_tracker(session, "with").id
        make_tracker(session, "without", with_profile=False)
    finally:
        session.close()

    assert client.get("/api/uploads/trackers").json() == [
        {"id": tracker_id, "label": "with", "torrent_client_id": qbit_id, "torrent_client_label": "qbit",
         "freeleech_options": [], "default_freeleech": None}
    ]


def test_verify_endpoint_rejects_unknown_target_or_dupe(client, tmp_path, setup):
    job_id = _awaiting_match(client, tmp_path, setup, "Movie.2024.mkv", ["Movie.2024.mkv"])
    client.post(f"/api/uploads/{job_id}/match", json={"content_type": "movie", "tmdb_id": 1, "kind": "movie"})
    target_id = client.get(f"/api/uploads/{job_id}").json()["targets"][0]["id"]

    resp = client.post(f"/api/uploads/{job_id}/targets/999/verify", json={"torrent_id_remote": "1"})
    assert resp.status_code == 404
    resp = client.post(f"/api/uploads/{job_id}/targets/{target_id}/verify", json={"torrent_id_remote": "1"})
    assert resp.json()["detail"]["code"] == "upload_dupe_not_found"


def test_overrides_and_approve_through_the_api(client, tmp_path, setup):
    job_id = _awaiting_match(client, tmp_path, setup, "Movie.Name.2024.1080p.WEB-DL.H.264-GRP.mkv",
                             ["Movie.Name.2024.1080p.WEB-DL.H.264-GRP.mkv"])
    client.post(f"/api/uploads/{job_id}/match", json={"content_type": "movie", "tmdb_id": 1, "kind": "movie"})

    detail = client.put(f"/api/uploads/{job_id}/overrides", json={"overrides": {"group": "ME"}}).json()
    target = detail["targets"][0]
    assert target["proposed_name"].endswith("-ME")
    assert target["type_id_map"] == {}

    resp = client.post(f"/api/uploads/{job_id}/approve", json={"targets": [
        {"target_id": target["id"], "action": "upload", "name": "Movie Name (2024)", "category_id": 1, "type_id": 4,
         "resolution_id": 3, "flags": {"anonymous": False}},
    ]})
    assert resp.status_code == 200, resp.text
    session = client.app.state.session_factory()
    try:
        job = session.get(UploadJob, job_id)
        assert "target_approved" in [e.code for e in job.events]
        assert job.targets[0].approved_name == "Movie Name (2024)"
    finally:
        session.close()


def test_targets_expose_the_freeleech_options_of_their_profile(client, tmp_path, setup):
    client.patch(f"/api/trackers/{setup['tracker_id']}/upload-profile", json={"freeleech_options": [50, 25]})
    job_id = _awaiting_match(client, tmp_path, setup, "Movie.2024.mkv", ["Movie.2024.mkv"])
    client.post(f"/api/uploads/{job_id}/match", json={"content_type": "movie", "tmdb_id": 1, "kind": "movie"})

    target = client.get(f"/api/uploads/{job_id}").json()["targets"][0]

    assert target["freeleech_options"] == [25, 50]
    assert target["flags"]["freeleech"] == 0


def test_the_file_naming_pattern_is_editable_and_previewed(client):
    current = client.get("/api/uploads/file-naming").json()
    assert current["rules"]["separator"] == "." and current["rules"] == current["default"]

    preview = client.post("/api/uploads/file-naming/preview", json={"rules": current["rules"]}).json()
    names = {e["key"]: e["name"] for e in preview["examples"]}
    assert names["uhd_remux"].startswith("Dune.Part.Two.2024.2160p.BluRay.REMUX.")
    assert names["uhd_remux"].endswith("-FraMeSToR.mkv") and ":" not in names["uhd_remux"]

    # La forma tipizzata (audit 2026-10-18, finding 61): i valori del nome
    # come testo, anche l'anno (un numero nei valori di release_values).
    assert preview["sample"]["kind"] == "example" and preview["variables"]["year"] == "2024"
    assert all(isinstance(v, str) or v is None for v in preview["variables"].values())

    mine = {**current["rules"], "templates": {"default": "{title} {year} {group}"}}
    assert client.put("/api/uploads/file-naming", json={"rules": mine}).json() == {
        "rules": mine, "default": current["default"]}


@pytest.mark.parametrize("bad", [{"templates": 5}, {"separator": 3}, {"audio_languages": "all"},
                                 {"audio_codecs": "x"}, {"audio_languages": {"multi_from": "4"}}])
def test_file_naming_rules_without_the_expected_form_are_refused(client, bad):
    """Audit 2026-10-18 (finding 50): si salvavano senza controllo, e poi
    ogni analisi di un upload falliva con un TypeError."""
    rules = {**client.get("/api/uploads/file-naming").json()["rules"], **bad}
    for response in (client.put("/api/uploads/file-naming", json={"rules": rules}),
                     client.post("/api/uploads/file-naming/preview", json={"rules": rules})):
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "upload_invalid_naming_rules"
    assert client.get("/api/uploads/file-naming").json()["rules"]["separator"] == "."


def test_stored_naming_rules_that_no_longer_fit_fall_back_to_the_defaults(client, caplog):
    """Regole storte già salvate (prima del controllo, o a mano nel DB): le
    regole di default con un avviso, mai un errore per ogni upload."""
    from nazgarr.core.models import Tracker, TrackerUploadProfile
    from nazgarr.upload import decision as upload_decision
    from nazgarr.upload import file_names as upload_file_names

    session = client.app.state.session_factory()
    settings_repo.set_setting(session, upload_file_names.SETTING, json.dumps({"templates": 5}))
    session.commit()
    assert upload_file_names.rules(session) == upload_file_names.DEFAULT_RULES
    assert "non valide" in caplog.text
    tracker = Tracker(label="t", adapter_type="unit3d", base_url="https://t.example", api_token="x")
    session.add(tracker)
    session.commit()
    profile = TrackerUploadProfile(tracker_id=tracker.id, naming_convention="scene",
                                   naming_rules_json=json.dumps({"separator": 3}))
    assert upload_decision.profile_rules(profile) == upload_decision.rules_from_convention("scene")
    session.close()
    assert client.get("/api/uploads/file-naming").json()["rules"] == upload_file_names.DEFAULT_RULES



def test_a_sure_match_confirms_itself_in_the_classic_flow_too(client, tmp_path, setup):
    session = client.app.state.session_factory()
    settings_repo.set_setting(session, upload_identify.AUTO_MATCH_SETTING, "0.9")
    session.close()
    video = write_video(tmp_path / "media" / "Movie.Name.2024.1080p.WEB.mkv")
    setup["resolver"].resolved = ResolvedMedia(tmdb_id=603, content_type="movie", title="Movie Name", year=2024)

    resp = client.post("/api/uploads", json={"disk_id": setup["disk_id"], "relative_path": "media/" + video.name})

    detail = client.get(f"/api/uploads/{resp.json()['id']}").json()
    assert detail["tmdb_id"] == 603 and detail["status"] != "awaiting_match"
    assert "auto_matched" in [e["code"] for e in detail["events"]]


def test_notices_tell_about_watched_releases_only_from_now_on(client, tmp_path, setup):
    from nazgarr.core.models import Disk
    from nazgarr.upload import jobs as upload_jobs

    session = client.app.state.session_factory()
    try:
        disk = session.get(Disk, setup["disk_id"])
        write_video(tmp_path / "media" / "Old.mkv")
        write_video(tmp_path / "releases" / "My.Movie.2024.mkv")
        upload_jobs.create_job(session, disk, "media/Old.mkv")  # a mano: nessun avviso
        start = client.get("/api/uploads/notices").json()
        assert start["notices"] == []  # un browser nuovo non riceve arretrati
        job = upload_jobs.create_job(session, disk, "releases/My.Movie.2024.mkv", origin="watch")
        upload_jobs.log_event(session, job, "analysis_done")
        session.commit()
        job_id = job.id
    finally:
        session.close()

    body = client.get("/api/uploads/notices", params={"after": start["latest_id"]}).json()

    assert [(n["upload_id"], n["kind"]) for n in body["notices"]] == [(job_id, "detected"), (job_id, "ready")]
    assert client.get("/api/uploads/notices", params={"after": body["latest_id"]}).json()["notices"] == []


def test_metadata_follows_the_interface_language_and_falls_back_to_english(client, monkeypatch):
    from nazgarr.library import tmdb_client as tmdb_module

    calls = []

    class _Response:
        def __init__(self, body):
            self.body, self.is_success = body, True

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class _Http:
        def get(self, path, params):
            calls.append(params.get("language"))
            # In italiano TMDB ha il titolo ma non la trama: la trama viene dall'inglese.
            if params.get("language") == "it-IT":
                return _Response({"id": 603, "title": "Matrix", "overview": "", "release_date": "1999-03-31"})
            english = {"id": 603, "title": "The Matrix", "overview": "A hacker learns...", "release_date": "1999"}
            return _Response(english)

    real = tmdb_module.TMDBClient(api_key="k", client=_Http())
    monkeypatch.setattr(metadata_api, "_client", lambda session: real)

    body = client.get("/api/metadata/movie/603", params={"language": "it-IT"}).json()

    assert (body["title"], body["overview"]) == ("Matrix", "A hacker learns...")
    assert calls == ["it-IT", "en-US"]
    assert client.get("/api/metadata/movie/603", params={"language": "../x"}).status_code == 422


def test_the_episode_ordering_is_offered_and_kept_with_the_match(client, tmp_path, setup, monkeypatch):
    from nazgarr.core.models import EpisodeOrderPreference
    from nazgarr.library import episode_orders
    from tests.test_episode_orders import FakeSonarr, FakeTmdb

    files = [f"Lupin.S02/Lupin.S02E0{e}.mkv" for e in range(1, 6)]  # le parti di Netflix: S02 da 5
    job_id = _awaiting_match(client, tmp_path, setup, "Lupin.S02", files)
    fake = FakeTMDB(details={("tv", 96677): {**tmdb_result(96677, "Lupin", 2021, "tv"), "tvdb_id": 375921}})
    monkeypatch.setattr(upload_identify, "tmdb_client", lambda session: fake)
    monkeypatch.setattr(episode_orders, "TmdbApi", lambda key: FakeTmdb())
    monkeypatch.setattr("nazgarr.integrations.arr.ArrApi", FakeSonarr)
    episode_orders._cache.clear()
    session = client.app.state.session_factory()
    try:
        from nazgarr.core.models import SonarrInstance

        settings_repo.set_setting(session, "tmdb_api_key", "k")
        session.add(SonarrInstance(label="sonarr", base_url="http://s", api_key="k"))
        session.commit()
    finally:
        session.close()

    orders = client.get(f"/api/uploads/{job_id}/episode-orders", params={"tmdb_id": 96677}).json()
    assert orders["recommended"] == "tmdb:group:g1"
    assert orders["warning"] == {"code": "files_not_tvdb_aired", "order": "tmdb:group:g1", "tvdb": "sonarr:aired"}
    assert orders["found"]["sonarr:aired"] == {"1": [6, 7, 8, 9, 10]}

    bad = client.post(f"/api/uploads/{job_id}/match", json={
        "content_type": "tv", "tmdb_id": 96677, "kind": "season_pack", "seasons": [2], "episode_order": "nope"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "upload_episode_order_unknown"
    resp = client.post(f"/api/uploads/{job_id}/match", json={
        "content_type": "tv", "tmdb_id": 96677, "kind": "season_pack", "seasons": [2],
        "episode_order": "tmdb:group:g1"})
    assert resp.status_code == 200, resp.text
    session = client.app.state.session_factory()
    try:
        job = session.get(UploadJob, job_id)
        assert job.episode_order == "tmdb:group:g1" and job.seasons_json == "[2]"
        assert session.get(EpisodeOrderPreference, 96677).order_key == "tmdb:group:g1"
        assert episode_orders.translate_files(job, 2, 3) == [(2, 3)]  # i file seguono già quello scelto
    finally:
        session.close()


def _jobs(session, statuses, trackers):
    from nazgarr.core.models import UploadTarget

    ids = []
    for status in statuses:
        job = UploadJob(relative_path=f"{status}.mkv", source_path=f"/x/{status}.mkv", status=status,
                        mediainfo_text="M" * 1000, analysis_json='{"x": 1}')
        session.add(job)
        session.flush()
        for tracker in trackers:
            session.add(UploadTarget(job_id=job.id, tracker_id=tracker.id, status="done" if status == "done" else
                                     "pending"))
        ids.append(job.id)
    session.commit()
    return ids


def test_the_list_costs_the_same_few_queries_whatever_the_history(client):
    """Audit 2026-10-18: una query per job e una per target (il profilo del
    tracker), a ogni poll di 2 s."""
    from sqlalchemy import event

    session = client.app.state.session_factory()
    try:
        trackers = [make_tracker(session, label=f"t{i}") for i in range(3)]
        _jobs(session, ["done"] * 30, trackers)
    finally:
        session.close()
    engine = client.app.state.session_factory.kw["bind"]
    statements = []

    def count(*_args):
        statements.append(1)

    event.listen(engine, "before_cursor_execute", count)
    try:
        listed = client.get("/api/uploads").json()
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(listed) == 30 and all(len(j["targets"]) == 3 for j in listed)
    assert listed[0]["targets"][0]["category_id_map"] is not None  # le mappe del profilo ci sono ancora
    assert len(statements) < 15


def test_active_jobs_and_the_history_in_pages(client):
    session = client.app.state.session_factory()
    try:
        tracker = make_tracker(session)
        done = _jobs(session, ["done", "failed", "cancelled", "partial", "done"], [tracker])
        active = _jobs(session, ["queued", "awaiting_decision"], [tracker])
    finally:
        session.close()

    assert [j["id"] for j in client.get("/api/uploads", params={"scope": "active"}).json()] == active[::-1]
    assert len(client.get("/api/uploads").json()) == 7  # senza scope tutti, come prima
    first = client.get("/api/uploads/history", params={"limit": 2}).json()
    second = client.get("/api/uploads/history", params={"limit": 2, "before_id": first["next_before_id"]}).json()
    last = client.get("/api/uploads/history", params={"limit": 2, "before_id": second["next_before_id"]}).json()

    assert first["total"] == 5 and [j["id"] for j in first["jobs"]] == done[::-1][:2]
    assert [j["id"] for j in second["jobs"]] == done[::-1][2:4]
    assert [j["id"] for j in last["jobs"]] == done[:1] and last["next_before_id"] is None
    # Il riordino della coda risponde con la stessa lista della coda.
    reordered = client.put("/api/uploads/queue", json={"job_ids": [active[0]]}).json()
    assert [j["id"] for j in reordered] == active[::-1]


def test_the_fixed_shapes_of_an_upload_are_typed_and_tolerate_older_rows():
    """Audit 2026-10-18 (finding 61): dupe, layout e analisi erano dict
    liberi, e il frontend ne riscriveva la forma a mano. Ora modelli, che
    accettano le righe salvate prima (chiavi aggiunte dopo, un id numerico)
    e lasciano passare le chiavi dell'analisi che non descrivono."""
    from nazgarr.api.uploads import UploadAnalysisResponse, UploadDupeResponse, UploadLayoutResponse

    dupe = UploadDupeResponse.model_validate({
        "torrent_id_remote": 77, "verdict": "identical", "download_link": "https://t/torrent/download/77.k",
        "verification": {"status": "passed", "info_hash": "ab", "pieces": 3, "ok": 3},
    })
    assert dupe.model_dump() == {
        "torrent_id_remote": "77", "name": "", "size_bytes": None, "verdict": "identical", "score": None,
        "reasons": [], "unknown": [], "seeders": None, "size_delta": None,
        "verification": {"status": "passed", "reason": None, "pieces": 3, "ok": 3, "mismatched": None,
                         "unreadable": None},
    }  # mai il link di download, né i tratti salvati per il nuovo verdetto (release)
    layout = UploadLayoutResponse.model_validate({
        "kind": "season_pack", "content_type": "tv", "videos": [{"relative_path": "a.mkv", "size_bytes": 1,
                                                                   "season": 1, "episodes": [1]}],
        "episodes_by_season": {"1": [1]}, "seasons": [1], "main_video": "/d/a.mkv", "title": "Show", "year": None,
        "other_files": 0, "total_size_bytes": 1,
    })
    assert layout.episodes_by_season == {1: [1]} and layout.release_group is None
    analysis = UploadAnalysisResponse.model_validate({
        "total_size_bytes": 1, "file_count": 1, "client_matches": [{
            "client": "qbit", "name": "x", "info_hash": "aa", "tracker_host": None, "state": "uploading",
            "match": "hardlink", "videos_matched": 1, "videos_total": 1}],
        "pack_mixed": {"resolution": ["1080p", "720p"]}, "file_names": {"available": []},
    }).model_dump()
    assert analysis["pack_mixed"] == {"resolution": ["1080p", "720p"]}
    assert analysis["client_matches"][0]["videos_hardlinked"] is None and analysis["seeding_here"] == {}


def test_the_tmdb_candidates_are_typed_and_an_older_row_keeps_its_missing_keys():
    """Anche i candidati TMDB del dettaglio (finding 61, dopo la review): una
    chiave che un job di prima non ha resta assente, mai null (la schermata
    di match legge confidence?: number), e i dettagli di un id forzato
    restano nel job."""
    from pydantic import TypeAdapter

    from nazgarr.api.uploads import UploadCandidateResponse

    candidates = TypeAdapter(list[UploadCandidateResponse])
    forced = {"tmdb_id": 603, "content_type": "movie", "title": "The Matrix", "original_title": "The Matrix",
              "year": 1999, "poster_path": "/p.jpg", "overview": "x", "source": "forced_tmdb", "confidence": 1.0,
              "confidence_parts": {"basis": "forced"}, "genres": ["Action"], "cast": ["Keanu"], "titles": ["Matrix"]}
    older = {"tmdb_id": "1399", "content_type": "tv", "title": None, "year": None, "poster_path": None}
    out = candidates.dump_python(candidates.validate_python([forced, older]), mode="json")
    assert out[0] == {key: value for key, value in forced.items() if key not in ("genres", "cast", "titles")}
    assert out[1] == {"tmdb_id": 1399, "content_type": "tv", "title": None, "year": None, "poster_path": None}
