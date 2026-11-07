import json

import pytest

from nazgarr.adapters.tracker.base import TorrentCandidate, Unit3dTrackerAdapter
from nazgarr.library.mediainfo_text import language_code, summarize
from nazgarr.torrents.catalog_categories import other_content_type
from nazgarr.upload.dupes import (
    SourceSummary,
    blocks_upload,
    check,
    classify,
    codec_family,
    rejudge,
    suggest,
    type_keys,
)

GB = 1024**3

# La tabella dei tipi del profilo ITT (nazgarr/tracker_profiles/itt.yaml).
ITT_TYPES = type_keys({"DISC": 1, "REMUX": 2, "ENCODE": 3, "WEBDL": 4, "WEBRIP": 5, "HDTV": 6, "DVDRIP": 24,
                       "BRRIP": 25, "WEBMUX": 26, "DLMUX": 27})


def _mi(video_format="AVC", library="", hdr="", languages=("Italian", "English"), titles=()):
    """Un MediaInfo in testo come lo dà UNIT3D, ridotto."""
    lines = ["General", "Complete name : Movie.mkv", "Format : Matroska", "", "Video", f"Format : {video_format}"]
    if library:
        lines.append(f"Writing library : {library}")
    if hdr:
        lines += [f"HDR format : {hdr}", "Transfer characteristics : PQ"]
    for i, language in enumerate(languages, start=1):
        lines += ["", f"Audio #{i}", "Format : AC-3"]
        if language:
            lines.append(f"Language : {language}")
        if i <= len(titles):
            lines.append(f"Title : {titles[i - 1]}")
    return summarize("\n".join(lines))


ENC_SIZE = int(10.6 * GB)  # entro il 10% dell'encode dei test, ma non entro il 2%


def _candidate(name, size=5 * GB, file_sizes=None, tid="1", type_id=None, resolution=None, seeders=None,
               mi=None, category_id=None):
    return TorrentCandidate(
        torrent_id_remote=tid, info_hash=None, name=name, size_bytes=size, file_list=None,
        mediainfo_unique_id=None, download_link=f"https://t/{tid}", file_sizes=file_sizes,
        category_id=category_id, type_id=type_id, resolution=resolution, seeders=seeders, media_info_summary=mi,
    )


MOVIE = SourceSummary(
    name="Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP", total_size_bytes=60 * GB,
    video_sizes=(60 * GB - 1000,), kind="movie", seasons=frozenset(), episode=None,
)

# Un encode 1080p x264 con audio italiano e inglese, come lo legge l'analisi (MediaInfo e nome).
ENCODE = SourceSummary(
    name="Il 13o Guerriero 1999 1080p BluRay x264 ITA ENG-GRP", total_size_bytes=10 * GB, video_sizes=(10 * GB,),
    kind="movie", seasons=frozenset(), episode=None, resolution="1080p", release_type="ENCODE", codec="AVC",
    hdr=frozenset(), languages=frozenset({"it", "en"}),
)


def test_identical_by_total_size():
    result = classify(_candidate("Dune Part Two 2024 2160p REMUX", size=60 * GB), MOVIE)
    assert result["verdict"] == "identical"


def test_identical_by_video_sizes_even_with_different_extras():
    result = classify(
        _candidate("Dune.Part.Two.2024.2160p.BluRay.REMUX-GRP", size=61 * GB,
                   file_sizes={"Dune.mkv": 60 * GB - 1000, "Dune.nfo": 2000}),
        MOVIE,
    )
    assert result["verdict"] == "identical"


@pytest.mark.parametrize(("name", "verdict", "reasons"), [
    ("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-OTHER", "same_slot", []),
    ("Dune.Part.Two.2024.1080p.BluRay.REMUX.AVC-OTHER", "different", ["resolution", "codec"]),
    ("Dune.Part.Two.2024.2160p.UHD.BluRay.x265.DV.HDR-OTHER", "different", ["type"]),
    ("Dune.Part.Two.2024.2160p.WEB-DL.DV.HDR.H265-OTHER", "different", ["type"]),
])
def test_movie_slots_from_the_names(name, verdict, reasons):
    result = classify(_candidate(name), MOVIE)
    assert (result["verdict"], result["reasons"]) == (verdict, reasons)


def test_a_name_without_source_is_read_from_the_tracker_fields_and_mediainfo():
    """Il falso dupe di prima: un nome senza sorgente né tipo saltava quei
    confronti e finiva nello stesso slot. Ora tipo e risoluzione vengono dal
    tracker, il codec dal suo MediaInfo: un x265 non è un dupe di un x264."""
    mask = _candidate("The Mask - Da zero a mito 1994 DD 5.1 ITA ENG 1080p H.265 sub ita eng Sp33dy94-MIRCrew",
                      size=ENC_SIZE, type_id=3, resolution="1080p", seeders=8, mi=_mi("HEVC", "x265 3.2.1"))
    result = classify(mask, ENCODE, keys=ITT_TYPES)
    assert (result["verdict"], result["reasons"]) == ("different", ["codec"])
    assert result["release"]["types"] == ("ENCODE",) and result["release"]["codec"] == "HEVC"


def test_codec_families_and_av1():
    assert codec_family("x264") == codec_family("H.264") == codec_family("AVC") == "AVC"
    assert codec_family("x265") == codec_family("H265") == codec_family("HEVC") == "HEVC"
    assert codec_family("AV1") == "AV1" and codec_family(None) is None
    same = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p", mi=_mi("AVC", "x264 core 157"))
    assert classify(same, ENCODE, keys=ITT_TYPES)["verdict"] == "same_slot"
    av1 = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p", mi=_mi("AV1"))
    assert classify(av1, ENCODE, keys=ITT_TYPES)["reasons"] == ["codec"]


def test_audio_languages_must_be_the_same_set():
    """ITA contro ITA ENG non è un dupe (decisione dell'utente, 2026-11-07);
    un commento non conta."""
    ita = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p", mi=_mi(languages=("Italian",)))
    assert classify(ita, ENCODE, keys=ITT_TYPES)["reasons"] == ["languages"]
    with_commentary = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p",
                                 mi=_mi(languages=("Italian", "English", "English"),
                                        titles=("", "", "Commentary with the director")))
    assert classify(with_commentary, ENCODE, keys=ITT_TYPES)["verdict"] == "same_slot"


def test_unknown_languages_leave_the_total():
    """Lingue ignote (una traccia senza lingua): fuori dal punteggio, non uno zero."""
    untagged = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p",
                          mi=_mi(languages=("Italian", "")))
    result = classify(untagged, ENCODE, keys=ITT_TYPES)
    assert (result["verdict"], result["score"], result["unknown"]) == ("same_slot", 100, ["languages"])


def test_a_dupe_with_too_little_known_is_only_possible():
    """Senza tipo, codec, HDR e lingue (un nome che dice solo la
    risoluzione, nessun MediaInfo) non si arriva alla soglia: upload, con
    un avviso. Video entro il 2% aggiungono 15 punti."""
    bare = _candidate("Il 13o Guerriero 1999 1080p", size=12 * GB)
    result = classify(bare, ENCODE)
    assert (result["verdict"], result["score"]) == ("possible", 44)
    assert result["unknown"] == ["type", "codec", "hdr", "languages"]
    close = classify(_candidate("Il 13o Guerriero 1999 1080p BluRay", size=10 * GB + 100 * 1024**2), ENCODE)
    assert (close["verdict"], close["score"], close["reasons"]) == ("same_slot", 84, ["size_close"])


@pytest.mark.parametrize("size", [12 * GB, 8 * GB])
def test_an_encode_with_a_different_size_is_a_possible_trump(size):
    """Oltre il 10% in più o in meno (decisione dell'utente, 2026-11-07): un
    altro encode, che può sostituirlo."""
    other = _candidate("Guerriero 1080p", size=size, type_id=3, resolution="1080p", mi=_mi("AVC", "x264"))
    result = classify(other, ENCODE, keys=ITT_TYPES)
    assert (result["verdict"], result["reasons"]) == ("trump", ["size_gap"])
    assert round(result["size_delta"], 2) == round((10 * GB - size) / size, 2)
    assert check([other], ENCODE, ITT_TYPES)[1] == "upload"


def test_a_remux_with_a_different_size_stays_a_dupe():
    remux = SourceSummary(
        name="Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-GRP", total_size_bytes=60 * GB,
        video_sizes=(60 * GB,), kind="movie", seasons=frozenset(), episode=None, release_type="REMUX",
    )
    result = classify(_candidate("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-OTHER", size=70 * GB), remux)
    assert result["verdict"] == "same_slot"


def test_a_dead_dupe_does_not_stop_the_upload():
    dead = _candidate("Guerriero 1080p", size=ENC_SIZE, type_id=3, resolution="1080p", seeders=0,
                      mi=_mi("AVC", "x264"))
    results, suggested = check([dead], ENCODE, ITT_TYPES)
    assert results[0]["verdict"] == "same_slot" and results[0]["seeders"] == 0
    assert suggested == "upload" and not blocks_upload(results[0])
    alive = _candidate("Guerriero 1080p", size=ENC_SIZE, tid="2", type_id=3, resolution="1080p", seeders=3,
                       mi=_mi("AVC", "x264"))
    assert check([dead, alive], ENCODE, ITT_TYPES)[1] == "skip"
    # Un identico morto: il reseed lo resuscita.
    assert suggest([{"verdict": "identical", "seeders": 0}]) == "reseed"


def test_the_tracker_type_wins_over_the_name():
    """BDRip ha un suo type_id su ITT: non è lo stesso slot di un encode,
    anche se il nome dice BluRay x264."""
    bdrip = _candidate("Guerriero 1999 1080p BluRay x264", size=ENC_SIZE, type_id=25, resolution="1080p")
    assert classify(bdrip, ENCODE, keys=ITT_TYPES)["reasons"] == ["type"]
    # Due tipi con lo stesso id in un altro profilo sono lo stesso slot lì.
    merged = type_keys({"ENCODE": 3, "BRRIP": 3})
    assert merged == {3: ("BRRIP", "ENCODE")}
    assert classify(bdrip, ENCODE, keys=merged)["reasons"] == []


EPISODE = SourceSummary(
    name="Severance.S02E03.1080p.ATVP.WEB-DL.DDP5.1.H.264-GRP.mkv", total_size_bytes=3 * GB,
    video_sizes=(3 * GB,), kind="episode", seasons=frozenset({2}), episode=3,
)


def test_episode_is_covered_by_a_season_pack_of_the_same_slot():
    result = classify(_candidate("Severance.S02.1080p.ATVP.WEB-DL.DDP5.1.H.264-NTb", size=30 * GB), EPISODE)
    assert (result["verdict"], result["reasons"]) == ("same_slot", ["covered_by_pack"])
    # A un'altra risoluzione non è un dupe, ma il pack della stagione resta da sapere.
    other = classify(_candidate("Severance.S02.2160p.ATVP.WEB-DL.DDP5.1.H.265-NTb", size=60 * GB), EPISODE)
    assert (other["verdict"], other["reasons"]) == ("different", ["resolution", "codec", "covered_by_pack"])


def test_other_episode_or_season_is_not_a_dupe():
    assert classify(_candidate("Severance.S02E04.1080p.WEB-DL-GRP"), EPISODE)["reasons"] == ["episode"]
    assert classify(_candidate("Severance.S01.1080p.WEB-DL-GRP"), EPISODE)["reasons"] == ["season"]


def test_season_pack_against_a_single_episode():
    pack = SourceSummary(
        name="Severance.S02.1080p.ATVP.WEB-DL.H.264-GRP", total_size_bytes=30 * GB, video_sizes=(3 * GB,) * 10,
        kind="season_pack", seasons=frozenset({2}), episode=None,
    )
    assert classify(_candidate("Severance.S02E03.1080p.WEB-DL.H.264-X"), pack)["reasons"] == ["single_episode"]
    assert classify(_candidate("Severance.S02.1080p.WEB-DL.H.264-X"), pack)["verdict"] == "same_slot"


def test_repack_of_the_same_group_replaces_the_release():
    repack = SourceSummary(
        name="Severance.S02E03.REPACK.1080p.WEB-DL-GRP.mkv", total_size_bytes=3 * GB, video_sizes=(3 * GB,),
        kind="episode", seasons=frozenset({2}), episode=3,
    )
    result = classify(_candidate("Severance.S02E03.1080p.WEB-DL-GRP", size=3 * GB - 5), repack)
    assert (result["verdict"], result["reasons"]) == ("different", ["repack_of_same_group"])


def test_check_orders_results_and_suggests_an_action():
    results, suggested = check([
        _candidate("Dune.Part.Two.2024.1080p.BluRay.REMUX-X", tid="a"),
        _candidate("Dune Part Two 2024 2160p", tid="c"),
        _candidate("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-X", tid="b"),
    ], MOVIE)
    assert [(r["torrent_id_remote"], r["verdict"]) for r in results] == [
        ("b", "same_slot"), ("c", "possible"), ("a", "different")]
    assert suggested == "skip"

    assert check([_candidate("x", size=60 * GB)], MOVIE)[1] == "reseed"
    assert check([], MOVIE) == ([], "upload")


def test_a_library_name_without_resolution_uses_the_mediainfo_one():
    """Un nome rinominato da Plex/Radarr non dice la risoluzione: senza
    quella di MediaInfo un 1080p sul tracker sembrava lo stesso posto."""
    plex = SourceSummary(name="Dune Part Two (2024)", total_size_bytes=60 * GB, video_sizes=(60 * GB - 1000,),
                         kind="movie", seasons=frozenset(), episode=None, resolution="2160p")
    assert classify(_candidate("Dune.Part.Two.2024.1080p.WEB-DL.H264-OTHER"), plex)["reasons"] == ["resolution"]
    unknown = SourceSummary(name="Dune Part Two (2024)", total_size_bytes=60 * GB, video_sizes=(60 * GB - 1000,),
                            kind="movie", seasons=frozenset(), episode=None)
    # Prima "same_slot": niente di confrontabile contava come uguale.
    assert classify(_candidate("Dune.Part.Two.2024.1080p.WEB-DL.H264-OTHER"), unknown)["verdict"] == "possible"


def test_rejudge_follows_the_overrides_and_keeps_the_verification():
    stored, _suggested = check([
        _candidate("Guerriero 1080p", size=ENC_SIZE, tid="a", type_id=5, resolution="1080p", seeders=2,
                   mi=_mi("AVC", "x264")),
        _candidate("Guerriero", tid="b", size=10 * GB),
    ], ENCODE, ITT_TYPES)
    stored = json.loads(json.dumps(stored))  # come da dupes_json
    stored[0]["verification"] = {"status": "passed", "pieces": 9}
    legacy = {"torrent_id_remote": "z", "name": "old", "verdict": "same_slot", "reasons": []}
    assert [r["verdict"] for r in stored] == ["identical", "different"]  # un WEBRip non è un encode

    webrip = SourceSummary(**{**ENCODE.__dict__, "release_type": "WEBRIP"})
    results, suggested = rejudge([*stored, legacy], webrip)
    by_id = {r["torrent_id_remote"]: r for r in results}
    assert by_id["a"]["verdict"] == "same_slot" and by_id["a"]["score"] == 100
    assert by_id["b"]["verification"] == {"status": "passed", "pieces": 9}
    assert by_id["z"] == legacy
    assert suggested == "reseed"


def test_other_content_type():
    categories = {"movie": 1, "tv": 2}
    assert other_content_type(2, "movie", categories) and other_content_type(1, "tv", categories)
    assert not other_content_type(1, "movie", categories)
    assert not other_content_type(7, "movie", categories)  # una categoria che il profilo non conosce
    assert not other_content_type(None, "movie", categories)
    assert not other_content_type(2, "movie", {})


def test_mediainfo_text_reads_the_first_file_of_a_pack():
    text = "\n".join([
        "General", "Complete name : S01E01.mkv", "", "Video", "Format : HEVC",
        "Writing library : x265 3.5", "Encoding settings : crf=18",
        "HDR format : Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
        "", "Video #2", "Format : AVC", "", "Audio #1", "Format : E-AC-3", "Language : English (US)",
        "Title : Main", "", "Audio #2", "Format : AAC", "Language : Klingon", "", "Text #1", "Language : Italian",
        "", "General", "Complete name : S01E02.mkv", "", "Audio", "Language : French",
    ])
    summary = summarize(text)
    assert summary["video"]["format"] == "HEVC" and summary["video"]["encoding_settings"] is True
    assert "Dolby Vision" in summary["video"]["hdr_format"]
    assert [t["language"] for t in summary["audio"]] == ["en", None]
    assert summarize("") is None and summarize("General\nFormat : Matroska") is None
    assert [language_code(v) for v in ("it", "en-US", "ita", "Italian", "Norwegian Bokmal", "nb", "")] == [
        "it", "en", "it", "it", "no", "no", None]


def test_the_unit3d_adapter_fills_the_dupe_fields():
    adapter = Unit3dTrackerAdapter("https://tracker.example", "token")
    item = {"id": 8887, "attributes": {
        "name": "The Mask 1994 1080p H.265", "size": 2_630_000_000, "files": [], "category_id": 1,
        "type_id": 3, "resolution": "1080p", "seeders": 8, "leechers": 0,
        "media_info": "General\nFormat : Matroska\n\nVideo\nFormat : HEVC\nWriting library : x265\n\n"
                      "Audio\nLanguage : Italian",
    }}
    candidate = adapter._to_candidate(item)
    assert (candidate.category_id, candidate.type_id, candidate.resolution, candidate.seeders) == (1, 3, "1080p", 8)
    assert candidate.media_info_summary["audio"] == [{"language": "it"}]
    odd = adapter._to_candidate({"id": 1, "attributes": {"name": "x", "size": 1, "category_id": "1",
                                                         "seeders": -3, "resolution": 1080, "media_info": None}})
    assert (odd.category_id, odd.seeders, odd.resolution, odd.media_info_summary) == (None, None, None, None)


def test_summary_of_reads_the_release_values_and_the_mediainfo(tmp_path):
    from types import SimpleNamespace

    from nazgarr.upload.analysis import summary_of

    job = SimpleNamespace(kind="movie", seasons_json="[]", episode=None, source_path=str(tmp_path / "Dune (2024)"),
                          is_dir=True, pack_json=None, overrides_json="{}")
    analysis = {"name_source": {"name": "Dune.Part.Two.2024.2160p.BluRay.REMUX-GRP", "origin": "torrent"},
                "mediainfo": {"video": {"width": 3840, "height": 1608, "format": "HEVC",
                                        "hdr_format": "Dolby Vision / SMPTE ST 2086",
                                        "hdr_format_compatibility": "HDR10"},
                              "audio": [{"language": "it"}, {"language": "en", "title": "Commentary"}]}}
    summary = summary_of(job, [(str(tmp_path / "Dune (2024)" / "Dune.mkv"), 10)], analysis)
    assert summary.name == "Dune.Part.Two.2024.2160p.BluRay.REMUX-GRP" and summary.resolution == "2160p"
    assert (summary.codec, summary.hdr, summary.languages) == ("HEVC", frozenset({"DV", "HDR"}), frozenset({"it"}))
    values = {"type": "REMUX", "resolution": "2160p", "video_codec": "x265"}
    job.overrides_json = json.dumps({"hdr": "HDR10"})
    corrected = summary_of(job, [], analysis, values)
    assert (corrected.release_type, corrected.hdr) == ("REMUX", frozenset({"HDR"}))
