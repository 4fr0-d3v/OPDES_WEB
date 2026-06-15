import io
import json
import re
import zipfile
from pathlib import Path

import pytest
import requests

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


def test_limpiar_temporales_no_borra_descargas_concurrentes(tmp_path):
    output_dir = tmp_path / "out"
    activo = output_dir / "_tmp" / "arc-en-curso"
    activo.mkdir(parents=True)
    activo_file = activo / "ep01.mkv.part"
    activo_file.write_bytes(b"partial")

    terminado = output_dir / "_tmp" / "arc-terminado"
    terminado.mkdir(parents=True)
    (terminado / "ep01.mkv.part").write_bytes(b"viejo")

    webapp.limpiar_temporales_si_ok(output_dir, slug="arc-terminado")

    assert not terminado.exists()
    assert activo_file.exists(), "no debe borrar tmp de jobs concurrentes"


def test_limpiar_temporales_no_escanea_arbol_completo(tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    (output_dir / "Season 1").mkdir(parents=True)
    (output_dir / "Season 1" / "S01E01.mkv").write_bytes(b"video")

    calls = []
    real_rglob = Path.rglob

    def spy(self, pattern):
        calls.append((str(self), pattern))
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", spy)

    webapp.limpiar_temporales_si_ok(output_dir, slug="x")

    assert not any(str(output_dir) in c[0] for c in calls), \
        f"limpiar_temporales no debe recorrer output_dir entero: {calls}"


def test_job_cancel_force_libera_jobs_colgados(isolated_app):
    webapp.job_create("zombie-1", "Zombie", lambda jid: None, ())
    webapp._jobs["zombie-1"]["status"] = "running"
    webapp._jobs["zombie-1"]["started_at"] = 0

    with isolated_app.test_client() as client:
        setup_response = client.get("/setup")
        token = csrf_from(setup_response.get_data(as_text=True))
        resp = client.post(
            "/api/jobs/zombie-1/cancel?force=1",
            headers={"X-CSRF-Token": token},
        )

    assert resp.status_code == 200
    assert webapp._jobs["zombie-1"]["status"] == "cancelled"


def test_setup_page_title_es_dinamico(isolated_app):
    with isolated_app.test_client() as client:
        html = client.get("/setup").get_data(as_text=True)
    assert "<title>Configuración" in html or "<title>Configuracion" in html


def test_setup_indica_path_inaccesible(isolated_app):
    webapp.guardar_config({
        **webapp.DEFAULT_CONFIG,
        "output_dir": "/no/existe/output",
        "metadata_dir": "/no/existe/meta",
    })
    with isolated_app.test_client() as client:
        html = client.get("/setup").get_data(as_text=True)
    assert "no es accesible" in html.lower()


def test_toggle_watched_falla_si_jellyfin_devuelve_error(isolated_app, monkeypatch):
    monkeypatch.setattr(webapp, "jellyfin_get_user_id", lambda cfg: "u1")
    monkeypatch.setattr(
        webapp,
        "jellyfin_season_data",
        lambda s, cfg: {1: {"id": "i1", "played": False}},
    )

    class FakeResp:
        status_code = 500

        def raise_for_status(self):
            raise requests.HTTPError("boom")

    monkeypatch.setattr(webapp.requests, "post", lambda *a, **k: FakeResp())

    with isolated_app.test_client() as client:
        token = csrf_from(client.get("/setup").get_data(as_text=True))
        resp = client.post(
            "/api/jellyfin/watched/1/1",
            headers={"X-CSRF-Token": token},
        )

    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False


def test_jellyfin_get_user_id_reintenta_en_fallo_transitorio(monkeypatch):
    calls = {"n": 0}

    class GoodResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [{
                "Id": "u1",
                "Name": "kilian",
                "Policy": {"IsAdministrator": True},
            }]

    def flaky(method, url, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.ConnectionError("blip")
        return GoodResp()

    monkeypatch.setattr(webapp.requests, "request", flaky)
    webapp._jf_user_cache["id"] = None
    webapp._jf_user_cache["ts"] = 0.0
    uid = webapp.jellyfin_get_user_id({
        "jellyfin_url": "http://x",
        "jellyfin_token": "t",
        "jellyfin_user": "kilian",
    })
    assert uid == "u1"
    assert calls["n"] == 2


def test_api_cache_clear_resetea_caches(isolated_app):
    webapp._catalog_cache = {"data": ["x"], "ts": 9999999999.0}
    webapp._pixeldrain_episode_cache["http://x"] = {"episodes": [1], "ts": 9999999999.0}
    webapp._jf_user_cache["id"] = "cache"
    webapp._jf_user_cache["ts"] = 9999999999.0

    with isolated_app.test_client() as client:
        token = csrf_from(client.get("/setup").get_data(as_text=True))
        resp = client.post("/api/cache/clear", headers={"X-CSRF-Token": token})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert webapp._catalog_cache["data"] is None
    assert webapp._pixeldrain_episode_cache == {}
    assert webapp._jf_user_cache["id"] is None


def test_descargar_episodio_bg_single_file_rechaza_episodio_distinto(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "extraer_tipo_e_id", lambda u: ("file", "abc"))
    monkeypatch.setattr(
        webapp,
        "pedir_json_resistente",
        lambda path, url: {"name": "[One Pace][1080p] Arc 05 [crc][quality][12345678].mkv"},
    )
    monkeypatch.setattr(webapp, "archivo_ya_existe_en_destino_final", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "construir_indice_metadatos", lambda md: {})

    webapp.job_create("ep-1-3", "ep", lambda jid: None, ())
    webapp.descargar_episodio_bg(
        "ep-1-3",
        {
            "id": "arc",
            "season_number": 1,
            "opciones": [{"url": "https://pixeldrain.net/u/abc", "quality": "1080p"}],
        },
        3,
        {
            "output_dir": str(tmp_path / "out"),
            "metadata_dir": str(tmp_path / "meta"),
            "quality": "max",
        },
    )
    assert webapp._jobs["ep-1-3"]["status"] == "error"
    assert "no coincide" in webapp._jobs["ep-1-3"]["msg"].lower()


def test_extraer_calidad_detecta_2160p_y_4k():
    assert webapp.extraer_calidad_desde_texto("One Pace 2160p HEVC") == "2160p"
    assert webapp.extraer_calidad_desde_texto("Arco 4K") == "2160p"
    assert webapp.ordenar_calidades("2160p") > webapp.ordenar_calidades("1080p")
