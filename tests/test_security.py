import io
import json
import re
import zipfile

import pytest

import src.opdes_web_app as webapp


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    metadata_dir = tmp_path / "metadata"
    output_dir = tmp_path / "output"
    season_dir = metadata_dir / "Season 1"
    season_dir.mkdir(parents=True)
    (season_dir / "season.nfo").write_text("<season><title>Season 1</title></season>", encoding="utf-8")
    output_dir.mkdir()

    monkeypatch.setattr(webapp, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(webapp, "CONFIG_PATH", config_dir / "config.json")
    webapp.guardar_config(
        {
            **webapp.DEFAULT_CONFIG,
            "output_dir": str(output_dir),
            "metadata_dir": str(metadata_dir),
            "jellyfin_url": "http://192.168.1.204:8096",
            "jellyfin_token": "secret-token",
        }
    )
    webapp.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return webapp.app


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_setup_never_renders_existing_jellyfin_token(isolated_app):
    with isolated_app.test_client() as client:
        response = client.get("/setup")
        html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "secret-token" not in html
    assert "Token configurado" in html


def test_empty_jellyfin_token_preserves_existing_token(isolated_app):
    with isolated_app.test_client() as client:
        setup_response = client.get("/setup")
        token = csrf_from(setup_response.get_data(as_text=True))
        response = client.post(
            "/setup",
            data={
                "csrf_token": token,
                "url": webapp.DEFAULT_CONFIG["url"],
                "output_dir": json.loads(webapp.CONFIG_PATH.read_text())["output_dir"],
                "metadata_dir": json.loads(webapp.CONFIG_PATH.read_text())["metadata_dir"],
                "quality": "max",
                "jellyfin_url": "http://192.168.1.204:8096",
                "jellyfin_token": "",
                "jellyfin_user": "",
                "jellyfin_series": "One Piece",
            },
        )

    assert response.status_code == 302
    assert json.loads(webapp.CONFIG_PATH.read_text())["jellyfin_token"] == "secret-token"


def test_post_requires_csrf(isolated_app):
    with isolated_app.test_client() as client:
        response = client.post("/api/jobs/clear")

    assert response.status_code == 400


def test_sync_metadata_is_not_get(isolated_app):
    with isolated_app.test_client() as client:
        response = client.get("/sync-metadata")

    assert response.status_code == 405


def test_safe_extract_zip_rejects_path_traversal(tmp_path):
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w") as zf:
        zf.writestr("../escape.txt", "owned")
    zip_bytes.seek(0)

    with zipfile.ZipFile(zip_bytes) as zf:
        with pytest.raises(RuntimeError):
            webapp.safe_extract_zip(zf, tmp_path / "extract")


def test_pixeldrain_video_files_ignores_non_video_entries(monkeypatch):
    monkeypatch.setattr(
        webapp,
        "obtener_archivos_lista_pixeldrain",
        lambda url: [
            {"name": "episode.mp4", "id": "video"},
            {"name": "wip.md", "id": "note"},
            {"name": "poster.jpg", "id": "image"},
        ],
    )

    files = webapp.pixeldrain_video_files("https://pixeldrain.net/l/example")

    assert [f["id"] for f in files] == ["video"]


def test_episode_availability_uses_pixeldrain_manifest(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    metadata_dir = tmp_path / "metadata"
    season_dir = metadata_dir / "Season 1"
    season_dir.mkdir(parents=True)
    output_dir.mkdir()
    (season_dir / "season.nfo").write_text("<season><title>Season 1</title></season>", encoding="utf-8")
    for episode in [1, 2]:
        (season_dir / f"S01E{episode:02d}.nfo").write_text(
            f"<episodedetails><title>Episode {episode}</title></episodedetails>",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        webapp,
        "cargar_arc_por_numero",
        lambda season, config: {
            "id": "arc",
            "season_number": season,
            "season_title": "Arc",
            "elegida": {"url": "https://pixeldrain.net/l/example", "quality": "1080p"},
        },
    )
    monkeypatch.setattr(webapp, "pixeldrain_available_episodes", lambda url: {1})

    detail = webapp.cargar_detalle_temporada(
        1,
        {
            **webapp.DEFAULT_CONFIG,
            "output_dir": str(output_dir),
            "metadata_dir": str(metadata_dir),
        },
    )

    assert [episode["available"] for episode in detail["episodes"]] == [True, False]


def test_job_cancel_and_retry_for_queued_job(isolated_app):
    webapp.job_create("test-job", "Test job", lambda job_id: None, ())

    with isolated_app.test_client() as client:
        setup_response = client.get("/setup")
        token = csrf_from(setup_response.get_data(as_text=True))
        cancel = client.post("/api/jobs/test-job/cancel", headers={"X-CSRF-Token": token})
        retry = client.post("/api/jobs/test-job/retry", headers={"X-CSRF-Token": token})
        jobs = client.get("/api/jobs").get_json()

    assert cancel.status_code == 200
    assert retry.status_code == 200
    assert jobs["test-job"]["status"] in {"queued", "running", "done"}
    assert "_fn" not in jobs["test-job"]


def test_bulk_episode_download_endpoint_starts_selected_jobs(isolated_app, monkeypatch):
    monkeypatch.setattr(
        webapp,
        "cargar_detalle_temporada",
        lambda season, config: {
            "arc": {"id": "arc", "season_number": season, "season_title": "Arc", "opciones": []},
            "season_meta": {},
            "episodes": [
                {"number": 1, "available": True, "downloaded": False},
                {"number": 2, "available": False, "downloaded": False},
                {"number": 3, "available": True, "downloaded": True},
            ],
        },
    )

    with isolated_app.test_client() as client:
        setup_response = client.get("/setup")
        token = csrf_from(setup_response.get_data(as_text=True))
        response = client.post(
            "/api/download/season/1/episodes",
            json={"episodes": [1, 2, 3]},
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 200
    assert response.get_json()["job_ids"] == ["ep-1-1"]
