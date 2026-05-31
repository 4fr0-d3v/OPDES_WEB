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
