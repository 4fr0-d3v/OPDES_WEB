from __future__ import annotations

import json
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, send_file, session, url_for
from markupsafe import escape
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError, RequestException, Timeout
from tqdm import tqdm
from urllib3.util.retry import Retry
from werkzeug.security import check_password_hash

# ── Constants ──────────────────────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".opdes"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_CONFIG = {
    "url": "https://onepace.net/es/watch",
    "output_dir": "",
    "metadata_dir": "",
    "quality": "max",
    "log_level": "error",
    "jellyfin_url": "",
    "jellyfin_token": "",
    "jellyfin_user": "",
    "jellyfin_series": "One Piece",
}
HEADERS = {"User-Agent": "Mozilla/5.0"}
API_HOSTS = ["pixeldrain.net", "pixeldrain.com"]
CHUNK_SIZE = 1024 * 1024
MAX_REINTENTOS_DESCARGA = 8
MAX_REINTENTOS_JSON = 8
METADATA_REPO_ZIP_URL = "https://github.com/4fr0-d3v/OPDES/archive/refs/heads/main.zip"
METADATA_SOURCE_SUFFIX = "one-pace-jellyfin-master/One Pace"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov"}

app = Flask(__name__)


def _load_secret_key() -> str:
    secret_key = os.environ.get("OPDES_SECRET_KEY", "").strip()
    if secret_key:
        return secret_key
    if os.environ.get("OPDES_ENV", "development").lower() == "production":
        raise RuntimeError("OPDES_SECRET_KEY es obligatorio en OPDES_ENV=production.")
    return secrets.token_hex(32)


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("OPDES_SESSION_SECURE", "").lower() in {"1", "true", "yes"},
)

# ── Job tracking ───────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_cancel_flags: dict[str, threading.Event] = {}
_job_queue: list[str] = []
_job_queue_cv = threading.Condition(_jobs_lock)
_workers_started = False


def max_concurrent_downloads() -> int:
    raw = os.environ.get("OPDES_MAX_CONCURRENT_DOWNLOADS", "2").strip()
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return 2


def _job_terminal(status: str) -> bool:
    return status in {"done", "error", "cancelled"}


def job_create(job_id: str, label: str, fn=None, args: tuple = ()) -> None:
    now = time.time()
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued", "label": label,
            "progress": 0, "total": 0, "msg": "",
            "file_progress": 0, "file_total": 0,
            "files": [], "bytes_progress": 0, "bytes_total": 0,
            "bytes_per_sec": 0, "eta_seconds": None,
            "created_at": now, "queued_at": now, "started_at": None,
            "finished_at": None, "updated_at": now, "last_error": "",
            "_fn": fn, "_args": args,
        }
    _cancel_flags[job_id] = threading.Event()


def job_cancel_event(job_id: str) -> threading.Event | None:
    return _cancel_flags.get(job_id)


def job_update(job_id: str, **kw) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            job = _jobs[job_id]
            prev_bytes = int(job.get("bytes_progress") or job.get("file_progress") or 0)
            prev_ts = float(job.get("updated_at") or time.time())
            now = time.time()
            job.update(kw)
            current_bytes = int(job.get("bytes_progress") or job.get("file_progress") or 0)
            elapsed = max(now - prev_ts, 0.001)
            delta = current_bytes - prev_bytes
            if delta > 0:
                speed = delta / elapsed
                job["bytes_per_sec"] = speed
                total = int(job.get("bytes_total") or job.get("file_total") or 0)
                if total > current_bytes and speed > 0:
                    job["eta_seconds"] = int((total - current_bytes) / speed)
            if "status" in kw and kw["status"] == "running" and job.get("started_at") is None:
                job["started_at"] = now
            if "status" in kw and _job_terminal(kw["status"]):
                job["finished_at"] = now
                if kw["status"] == "error":
                    job["last_error"] = str(job.get("msg") or "")
            job["updated_at"] = now


def jobs_snapshot() -> dict:
    with _jobs_lock:
        snapshot = {}
        for k, v in _jobs.items():
            public = {pk: pv for pk, pv in v.items() if not pk.startswith("_")}
            snapshot[k] = public
        return snapshot


def jobs_clear_done() -> None:
    with _jobs_lock:
        done = [k for k, v in _jobs.items() if _job_terminal(v["status"])]
        for k in done:
            del _jobs[k]
            _cancel_flags.pop(k, None)


def _job_worker() -> None:
    while True:
        with _job_queue_cv:
            while True:
                while not _job_queue:
                    _job_queue_cv.wait()
                job_id = _job_queue.pop(0)
                job = _jobs.get(job_id)
                if job and job.get("status") == "queued":
                    break
            fn = job.get("_fn")
            args = job.get("_args", ())
            job["status"] = "running"
            job["started_at"] = time.time()
            job["updated_at"] = job["started_at"]
        if fn is None:
            job_update(job_id, status="error", msg="Job sin función asociada.")
            continue
        try:
            fn(job_id, *args)
        except Exception as e:
            job_update(job_id, status="error", msg=str(e))


def ensure_job_workers() -> None:
    global _workers_started
    with _jobs_lock:
        if _workers_started:
            return
        _workers_started = True
        worker_count = max_concurrent_downloads()
    for i in range(worker_count):
        t = threading.Thread(target=_job_worker, name=f"opdes-job-worker-{i + 1}", daemon=True)
        t.start()


def enqueue_job(job_id: str) -> None:
    ensure_job_workers()
    with _job_queue_cv:
        if job_id not in _job_queue and _jobs.get(job_id, {}).get("status") == "queued":
            _job_queue.append(job_id)
            _job_queue_cv.notify()


def job_cancel(job_id: str) -> bool:
    with _job_queue_cv:
        job = _jobs.get(job_id)
        if not job:
            return False
        if job["status"] == "queued":
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            job["updated_at"] = job["finished_at"]
            if job_id in _job_queue:
                _job_queue.remove(job_id)
            return True
        if job["status"] == "running":
            ev = _cancel_flags.get(job_id)
            if ev:
                ev.set()
            return True
        return False


def job_retry(job_id: str) -> bool:
    with _job_queue_cv:
        job = _jobs.get(job_id)
        if not job or job.get("_fn") is None or not _job_terminal(job["status"]):
            return False
        now = time.time()
        job.update({
            "status": "queued", "progress": 0, "total": 0, "msg": "",
            "file_progress": 0, "file_total": 0, "files": [],
            "bytes_progress": 0, "bytes_total": 0,
            "bytes_per_sec": 0, "eta_seconds": None,
            "queued_at": now, "started_at": None, "finished_at": None,
            "updated_at": now,
        })
        _cancel_flags[job_id] = threading.Event()
        if job_id not in _job_queue:
            _job_queue.append(job_id)
        _job_queue_cv.notify()
        return True


# ── Catalog cache ──────────────────────────────────────────────────────────────
_catalog_cache: dict = {"data": None, "ts": 0.0}
_pixeldrain_episode_cache: dict[str, dict] = {}
CATALOG_TTL = 3600.0

# ── Config ─────────────────────────────────────────────────────────────────────

def cargar_config() -> dict:
    if not CONFIG_PATH.exists():
        guardar_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        guardar_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    config = DEFAULT_CONFIG.copy()
    config.update(data)
    return config


def guardar_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def config_completa(config: dict) -> bool:
    return bool(
        str(config.get("output_dir", "")).strip()
        and str(config.get("metadata_dir", "")).strip()
    )


def metadatos_ok(config: dict) -> bool:
    md = str(config.get("metadata_dir", "")).strip()
    if not md:
        return False
    p = Path(md).expanduser()
    return p.exists() and bool(list(p.glob("Season *")))


# ── Security helpers ───────────────────────────────────────────────────────────

PUBLIC_ENDPOINTS = {"login", "login_submit", "favicon", "static"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def env_list(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def admin_auth_configured() -> bool:
    return bool(
        os.environ.get("OPDES_ADMIN_TOKEN", "").strip()
        or (
            os.environ.get("OPDES_ADMIN_USER", "").strip()
            and os.environ.get("OPDES_ADMIN_PASSWORD_HASH", "").strip()
        )
    )


def auth_required() -> bool:
    return admin_auth_configured() or os.environ.get("OPDES_ENV", "development").lower() == "production"


def is_authenticated() -> bool:
    return bool(session.get("opdes_admin_authenticated"))


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_input() -> str:
    return f'<input type="hidden" name="csrf_token" value="{escape(csrf_token())}">'


def validate_csrf() -> None:
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, "CSRF token inválido.")


def wants_json_response() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def validate_login(username: str, password: str) -> bool:
    admin_token = os.environ.get("OPDES_ADMIN_TOKEN", "").strip()
    if admin_token and hmac.compare_digest(password, admin_token):
        return True
    admin_user = os.environ.get("OPDES_ADMIN_USER", "").strip()
    password_hash = os.environ.get("OPDES_ADMIN_PASSWORD_HASH", "").strip()
    if admin_user and password_hash and hmac.compare_digest(username, admin_user):
        return check_password_hash(password_hash, password)
    return False


def ensure_auth_is_configured() -> None:
    if os.environ.get("OPDES_ENV", "development").lower() == "production" and not admin_auth_configured():
        raise RuntimeError("Configura OPDES_ADMIN_TOKEN o OPDES_ADMIN_USER/OPDES_ADMIN_PASSWORD_HASH en producción.")


def is_private_or_loopback_host(host: str) -> bool:
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except socket.gaierror:
        return False
    return False


def validate_remote_url(raw_url: str, *, allow_private: bool = False) -> str:
    value = raw_url.strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("La URL debe usar http:// o https:// y tener host.")
    allowed_hosts = set(env_list("OPDES_ALLOWED_REMOTE_HOSTS"))
    host = (parsed.hostname or "").lower()
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError(f"Host remoto no permitido: {host}")
    if not allow_private and not allowed_hosts and is_private_or_loopback_host(host):
        raise ValueError("Host privado bloqueado. Añádelo a OPDES_ALLOWED_REMOTE_HOSTS si es esperado.")
    return value


def allowed_path_roots() -> list[Path]:
    roots = env_list("OPDES_ALLOWED_PATHS")
    if not roots:
        return []
    return [Path(p).expanduser().resolve() for p in roots]


def validate_config_path(raw_path: str, field_name: str) -> str:
    value = raw_path.strip()
    if not value:
        return ""
    resolved = Path(value).expanduser().resolve()
    dangerous = {Path("/"), Path.home().resolve()}
    if resolved in dangerous:
        raise ValueError(f"{field_name} no puede apuntar a {resolved}.")
    roots = allowed_path_roots()
    if roots and not any(resolved == root or root in resolved.parents for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ValueError(f"{field_name} debe estar dentro de: {allowed}")
    return str(resolved)


def safe_extract_zip(zf: zipfile.ZipFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in zf.infolist():
        member_path = destination / member.filename
        resolved = member_path.resolve()
        if resolved != destination and destination not in resolved.parents:
            raise RuntimeError(f"ZIP inseguro: {member.filename}")
    zf.extractall(destination)


# ── Helpers ────────────────────────────────────────────────────────────────────

def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\-\.]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "sin_nombre"


def copiar_contenido_directorio(origen: Path, destino: Path) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    for item in origen.iterdir():
        dest_item = destino / item.name
        if item.is_dir():
            shutil.copytree(item, dest_item, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest_item)


# ── Network ────────────────────────────────────────────────────────────────────

def obtener_html(url: str) -> str:
    url = validate_remote_url(url)
    ultimo_error = None
    for intento in range(1, 6):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.text
        except RequestException as e:
            ultimo_error = e
            if intento == 5:
                break
            time.sleep(min(2 ** intento, 10))
    raise RuntimeError(f"No se pudo obtener el HTML de {url}: {ultimo_error}")


def extraer_temporadas_y_pixeldrain(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    temporadas = []
    for li in soup.find_all("li", id=True):
        season_id = li["id"]
        pixeldrain_links = []
        for a in li.find_all("a", href=True):
            href = a["href"].strip()
            if "pixeldrain.net" in href or "pixeldrain.com" in href:
                pixeldrain_links.append({"texto": a.get_text(separator=" ", strip=True), "url": href})
        temporadas.append({"id": season_id, "pixeldrain": pixeldrain_links})
    return temporadas


def extraer_calidad_desde_texto(texto: str) -> str | None:
    m = re.search(r"(480p|720p|1080p)", texto, re.IGNORECASE)
    return m.group(1).lower() if m else None


def ordenar_calidades(calidad: str) -> int:
    return {"480p": 480, "720p": 720, "1080p": 1080}.get(calidad.lower(), 0)


def agrupar_por_temporada(items: list[dict]) -> list[dict]:
    agrupado: dict[str, dict] = {}
    for item in items:
        arc_id = item.get("id", "sin_id")
        bucket = agrupado.setdefault(arc_id, {"id": arc_id, "opciones": []})
        for enlace in item.get("pixeldrain", []):
            calidad = extraer_calidad_desde_texto(enlace.get("texto", "")) or "desconocida"
            bucket["opciones"].append({"texto": enlace.get("texto", ""), "url": enlace.get("url", ""), "quality": calidad})
    resultado = list(agrupado.values())
    for idx, arc in enumerate(resultado, start=1):
        arc["season_number"] = idx
    return resultado


def elegir_opcion_por_calidad(opciones: list[dict], quality_config: str) -> dict | None:
    if not opciones:
        return None
    validas = [x for x in opciones if x.get("quality") in {"480p", "720p", "1080p"}]
    if not validas:
        return opciones[0]
    ordenadas = sorted(validas, key=lambda x: ordenar_calidades(x["quality"]))
    if quality_config == "max":
        return ordenadas[-1]
    for op in ordenadas:
        if op["quality"] == quality_config:
            return op
    return ordenadas[-1]


def extraer_tipo_e_id(url: str):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in API_HOSTS:
        raise ValueError(f"URL de Pixeldrain no soportada: {url}")
    path = parsed.path.strip("/")
    partes = path.split("/")
    if len(partes) >= 2:
        tipo, item_id = partes[0], partes[1]
        if tipo == "l":
            return "list", item_id
        if tipo == "u":
            return "file", item_id
    raise ValueError(f"URL de Pixeldrain no soportada: {url}")


def hosts_preferidos_desde_url(url: str):
    host = (urlparse(url).hostname or "").lower()
    if host == "pixeldrain.net":
        return ["pixeldrain.net", "pixeldrain.com"]
    if host == "pixeldrain.com":
        return ["pixeldrain.com", "pixeldrain.net"]
    return API_HOSTS[:]


def crear_sesion():
    session = requests.Session()
    retry = Retry(total=0, connect=0, read=0, redirect=3)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    return session


def pedir_json_resistente(path: str, url_original: str, max_intentos: int = MAX_REINTENTOS_JSON):
    ultimo_error = None
    hosts = hosts_preferidos_desde_url(url_original)
    for host in hosts:
        url = f"https://{host}/api{path}"
        for intento in range(1, max_intentos + 1):
            try:
                with requests.get(
                    url, timeout=(10, 30),
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Connection": "close"},
                ) as resp:
                    if resp.status_code == 404:
                        raise RuntimeError(f"No encontrado: {url}")
                    if resp.status_code == 403:
                        try:
                            det = resp.json()
                        except Exception:
                            det = {"message": "403 Forbidden"}
                        raise RuntimeError(f"Acceso denegado: {det.get('message', '403')}")
                    resp.raise_for_status()
                    return resp.json()
            except (ConnectionError, Timeout, ChunkedEncodingError, OSError) as e:
                ultimo_error = e
                if intento == max_intentos:
                    break
                time.sleep(min(2 ** intento, 20))
            except HTTPError as e:
                ultimo_error = e
                break
    raise RuntimeError(f"Falló la petición JSON: {ultimo_error}")


def obtener_archivos_lista_pixeldrain(url: str) -> list[dict]:
    tipo, item_id = extraer_tipo_e_id(url)
    if tipo == "file":
        info = pedir_json_resistente(f"/file/{item_id}/info", url)
        return [info]
    data = pedir_json_resistente(f"/list/{item_id}", url)
    return data.get("files", [])


def pixeldrain_video_files(url: str) -> list[dict]:
    files = obtener_archivos_lista_pixeldrain(url)
    return [
        f for f in files
        if Path(f.get("name") or "").suffix.lower() in VIDEO_EXTS
    ]


def pixeldrain_available_episodes(url: str) -> set[int]:
    now = time.time()
    cached = _pixeldrain_episode_cache.get(url)
    if cached and now - cached["ts"] < CATALOG_TTL:
        return set(cached["episodes"])
    episodes: set[int] = set()
    for file_info in pixeldrain_video_files(url):
        parsed = parsear_nombre_descargado(file_info.get("name") or "")
        if parsed:
            episodes.add(parsed["episode_in_arc"])
    _pixeldrain_episode_cache[url] = {"episodes": sorted(episodes), "ts": now}
    return episodes


# ── Metadata ───────────────────────────────────────────────────────────────────

def sync_metadata(config: dict) -> None:
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    metadata_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        zip_path = tmpdir_path / "repo.zip"
        extract_dir = tmpdir_path / "repo"
        with requests.get(METADATA_REPO_ZIP_URL, stream=True, timeout=120, headers=HEADERS) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
        with zipfile.ZipFile(zip_path, "r") as zf:
            safe_extract_zip(zf, extract_dir)
        source_dir = None
        for candidate in extract_dir.rglob("One Pace"):
            if candidate.as_posix().endswith(METADATA_SOURCE_SUFFIX):
                source_dir = candidate
                break
        if source_dir is None or not source_dir.exists():
            raise RuntimeError("No se encontró la carpeta de metadatos en el repositorio descargado.")
        copiar_contenido_directorio(source_dir, metadata_dir)


def leer_titulo_season_nfo(season_nfo: Path) -> str | None:
    try:
        root = ET.parse(season_nfo).getroot()
        title = root.findtext("title")
        return title.strip() if title else None
    except Exception:
        return None


def leer_episodio_nfo(ep_nfo: Path) -> dict:
    result = {"title": ep_nfo.stem, "plot": "", "aired": ""}
    try:
        root = ET.parse(ep_nfo).getroot()
        title = root.findtext("title")
        if title:
            result["title"] = title.strip()
        plot = root.findtext("plot")
        if plot:
            result["plot"] = plot.strip()
        aired = root.findtext("aired") or root.findtext("premiered")
        if aired:
            result["aired"] = aired.strip()
    except Exception:
        pass
    return result


def extraer_season_episode_de_nfo_name(nfo_name: str):
    m = re.search(r"S(\d+)E(\d+)", nfo_name, re.IGNORECASE)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def construir_indice_metadatos(metadata_root: Path) -> dict:
    indice = {}
    if not metadata_root.exists():
        return indice
    for season_dir in sorted(
        metadata_root.glob("Season *"),
        key=lambda p: int(re.search(r"\d+", p.name).group()),
    ):
        season_nfo = season_dir / "season.nfo"
        if not season_nfo.exists():
            continue
        num_match = re.search(r"Season\s+(\d+)", season_dir.name, re.IGNORECASE)
        if not num_match:
            continue
        season_number = int(num_match.group(1))
        season_title = leer_titulo_season_nfo(season_nfo) or season_dir.name
        episodes = {}
        for ep_nfo in sorted(season_dir.glob("*.nfo")):
            if ep_nfo.name.lower() == "season.nfo":
                continue
            _, ep_num = extraer_season_episode_de_nfo_name(ep_nfo.name)
            if ep_num is not None:
                episodes[ep_num] = ep_nfo
        indice[season_number] = {
            "season_number": season_number,
            "season_title": season_title,
            "season_dir": season_dir,
            "season_nfo": season_nfo,
            "episodes": episodes,
        }
    return indice


def parsear_nombre_descargado(nombre_archivo: str):
    patron = re.compile(
        r"^\[One Pace\]\[[^\]]+\]\s+(.+?)\s+(\d+)\s+(?:[A-Za-z][^\[]+)?\[[^\]]+\]\[[^\]]+\]\[[0-9A-Fa-f]{8}\](\.[^.]+)$"
    )
    m = patron.match(nombre_archivo)
    if not m:
        return None
    return {"arc_name": m.group(1).strip(), "episode_in_arc": int(m.group(2)), "ext": m.group(3)}


def obtener_ruta_final_esperada(
    nombre_archivo: str, destino_base: Path, indice_metadatos: dict | None, season_number: int | None
) -> Path | None:
    if not indice_metadatos or season_number is None:
        return None
    info = parsear_nombre_descargado(nombre_archivo)
    if not info:
        return None
    season_meta = indice_metadatos.get(season_number)
    if not season_meta:
        return None
    ep_nfo_src = season_meta["episodes"].get(info["episode_in_arc"])
    if not ep_nfo_src:
        return None
    carpeta_temporada = destino_base / f"Season {season_meta['season_number']}"
    return carpeta_temporada / f"{ep_nfo_src.stem}{Path(nombre_archivo).suffix}"


def archivo_ya_existe_en_destino_final(
    nombre_archivo: str, destino_base: Path, indice_metadatos: dict | None, season_number: int | None
) -> Path | None:
    ruta_final = obtener_ruta_final_esperada(nombre_archivo, destino_base, indice_metadatos, season_number)
    if ruta_final and ruta_final.exists():
        return ruta_final
    return None


def asegurar_estructura_temporada(destino_base: Path, season_meta: dict) -> Path:
    carpeta_temporada = destino_base / f"Season {season_meta['season_number']}"
    carpeta_temporada.mkdir(parents=True, exist_ok=True)
    season_nfo_dest = carpeta_temporada / "season.nfo"
    if not season_nfo_dest.exists():
        shutil.copy2(season_meta["season_nfo"], season_nfo_dest)
    for poster_name in ["poster.png", "folder.jpg", "folder.png", "season.jpg", "season.png"]:
        poster_src = season_meta["season_dir"] / poster_name
        if poster_src.exists():
            poster_dest = carpeta_temporada / poster_src.name
            if not poster_dest.exists():
                shutil.copy2(poster_src, poster_dest)
            break
    return carpeta_temporada


def renombrar_y_copiar_nfo_segun_metadata(
    video_path: Path, destino_base: Path, indice_metadatos: dict, season_number: int
) -> Path:
    info = parsear_nombre_descargado(video_path.name)
    if not info:
        return video_path
    season_meta = indice_metadatos.get(season_number)
    if not season_meta:
        return video_path
    ep_nfo_src = season_meta["episodes"].get(info["episode_in_arc"])
    if not ep_nfo_src:
        return video_path
    carpeta_temporada = asegurar_estructura_temporada(destino_base, season_meta)
    nuevo_stem = ep_nfo_src.stem
    nuevo_video = carpeta_temporada / f"{nuevo_stem}{video_path.suffix}"
    nuevo_nfo = carpeta_temporada / ep_nfo_src.name
    if video_path.resolve() != nuevo_video.resolve() and not nuevo_video.exists():
        shutil.move(str(video_path), str(nuevo_video))
    if not nuevo_nfo.exists():
        shutil.copy2(ep_nfo_src, nuevo_nfo)
    return nuevo_video


# ── Local filesystem helpers ───────────────────────────────────────────────────

def contar_locales(output_dir: Path, season_number: int) -> int:
    carpeta = output_dir / f"Season {season_number}"
    if not carpeta.exists():
        return 0
    return sum(1 for f in carpeta.iterdir() if f.suffix.lower() in VIDEO_EXTS)


def episodio_descargado(output_dir: Path, season_number: int, episode_number: int) -> bool:
    carpeta = output_dir / f"Season {season_number}"
    if not carpeta.exists():
        return False
    patron = re.compile(rf"S{season_number:02d}E{episode_number:02d}", re.IGNORECASE)
    return any(
        patron.search(f.name) and f.suffix.lower() in VIDEO_EXTS
        for f in carpeta.iterdir()
    )


def encontrar_archivo_local(output_dir: Path, season_number: int, episode_number: int) -> Path | None:
    carpeta = output_dir / f"Season {season_number}"
    if not carpeta.exists():
        return None
    patron = re.compile(rf"S{season_number:02d}E{episode_number:02d}", re.IGNORECASE)
    for f in carpeta.iterdir():
        if patron.search(f.name) and f.suffix.lower() in VIDEO_EXTS:
            return f
    return None


def limpiar_temporales_si_ok(base_dir: Path) -> None:
    tmp_dir = base_dir / "_tmp"
    if tmp_dir.exists() and tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    for ds_store in base_dir.rglob(".DS_Store"):
        try:
            ds_store.unlink()
        except Exception:
            pass


# ── Download ───────────────────────────────────────────────────────────────────

def descargar_archivo_reanudable(
    file_id: str,
    nombre_archivo: str,
    carpeta_destino: Path,
    session: requests.Session,
    url_original: str,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
):
    carpeta_destino.mkdir(parents=True, exist_ok=True)
    destino = carpeta_destino / nombre_archivo
    temp = destino.with_suffix(destino.suffix + ".part")
    if destino.exists():
        return destino
    ultimo_error = None
    hosts = hosts_preferidos_desde_url(url_original)
    for host in hosts:
        url_descarga = f"https://{host}/api/file/{file_id}?download"
        for intento in range(1, MAX_REINTENTOS_DESCARGA + 1):
            descargado = temp.stat().st_size if temp.exists() else 0
            headers = {"Connection": "close", "User-Agent": "Mozilla/5.0"}
            if descargado > 0:
                headers["Range"] = f"bytes={descargado}-"
            try:
                with session.get(url_descarga, headers=headers, stream=True, timeout=(10, 120)) as resp:
                    if resp.status_code == 403:
                        raise RuntimeError(f"403 Forbidden: {file_id}")
                    if resp.status_code == 404:
                        raise RuntimeError(f"Archivo no encontrado: {file_id}")
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    modo = "ab" if resp.status_code == 206 and descargado > 0 else "wb"
                    if modo == "wb" and temp.exists():
                        temp.unlink()
                        descargado = 0
                    total = None
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        total_resp = int(cl)
                        total = descargado + total_resp if resp.status_code == 206 else total_resp
                    bytes_escritos = descargado
                    with open(temp, modo) as f, tqdm(
                        total=total,
                        initial=descargado,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=nombre_archivo,
                        ascii=False,
                        dynamic_ncols=True,
                    ) as pbar:
                        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                            if cancel_event and cancel_event.is_set():
                                raise InterruptedError("Cancelado")
                            if chunk:
                                f.write(chunk)
                                bytes_escritos += len(chunk)
                                pbar.update(len(chunk))
                                if progress_cb and total:
                                    progress_cb(bytes_escritos, total)
                temp.rename(destino)
                return destino
            except InterruptedError:
                raise
            except (ConnectionError, Timeout, ChunkedEncodingError, OSError) as e:
                ultimo_error = e
                if intento == MAX_REINTENTOS_DESCARGA:
                    break
                time.sleep(min(2 ** intento, 30))
            except RuntimeError:
                raise
    raise RuntimeError(f"Fallo descargando {file_id}: {ultimo_error}")


def descargar_temporada_bg(job_id: str, arc: dict, config: dict) -> None:
    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()
    opcion = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
    if not opcion:
        job_update(job_id, status="error", msg="No hay enlaces disponibles.")
        return
    session = crear_sesion()
    tmp_dir = output_dir / "_tmp" / slugify(arc["id"])
    try:
        archivos_pd = pixeldrain_video_files(opcion["url"])
        files_manifest = [
            {"name": a.get("name") or f"{a.get('id','?')}.bin",
             "size": a.get("size", 0), "progress": 0, "done": False}
            for a in archivos_pd
        ]
        bytes_total = sum(f["size"] for f in files_manifest)
        job_update(job_id, total=len(archivos_pd), files=files_manifest,
                   bytes_total=bytes_total, bytes_progress=0)
    except Exception as e:
        job_update(job_id, status="error", msg=str(e))
        return
    done = [0]

    def _mark_done(idx: int) -> None:
        done[0] += 1
        with _jobs_lock:
            if job_id not in _jobs:
                return
            j = _jobs[job_id]
            j["progress"] = done[0]
            files = j.get("files", [])
            if idx < len(files):
                files[idx]["done"] = True
                files[idx]["progress"] = files[idx]["size"]
            j["bytes_progress"] = sum(f["progress"] for f in files)

    def _make_prog(idx: int):
        def _cb(dl: int, tot: int) -> None:
            with _jobs_lock:
                if job_id not in _jobs:
                    return
                j = _jobs[job_id]
                files = j.get("files", [])
                if idx < len(files):
                    files[idx]["progress"] = dl
                    if tot:
                        files[idx]["size"] = tot
                j["bytes_progress"] = sum(f["progress"] for f in files)
        return _cb

    cancel_ev = job_cancel_event(job_id)
    try:
        rutas = []
        for i, archivo in enumerate(archivos_pd):
            if cancel_ev and cancel_ev.is_set():
                raise InterruptedError("Cancelado")
            file_id = archivo.get("id")
            if not file_id:
                continue
            nombre = archivo.get("name") or f"{file_id}.bin"
            existente = archivo_ya_existe_en_destino_final(nombre, output_dir, indice_metadatos, arc["season_number"])
            if existente:
                rutas.append(existente)
                _mark_done(i)
                continue
            ruta = descargar_archivo_reanudable(file_id, nombre, tmp_dir, session, opcion["url"], _make_prog(i), cancel_ev)
            rutas.append(ruta)
            _mark_done(i)
        rutas_finales = []
        for ruta in rutas:
            ruta_path = Path(ruta)
            try:
                ruta_path.relative_to(output_dir)
                ya_final = ruta_path.parent.name.startswith("Season ")
            except ValueError:
                ya_final = False
            if ya_final:
                rutas_finales.append(ruta_path)
            else:
                rutas_finales.append(
                    renombrar_y_copiar_nfo_segun_metadata(ruta_path, output_dir, indice_metadatos, arc["season_number"])
                )
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="done", msg=f"Descargados {len(rutas_finales)} episodio(s).")
    except InterruptedError:
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="cancelled")
    except Exception as e:
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="error", msg=str(e))


def descargar_episodio_bg(job_id: str, arc: dict, episode_number: int, config: dict) -> None:
    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()
    opcion = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
    if not opcion:
        job_update(job_id, status="error", msg="No hay enlaces disponibles.")
        return
    url = opcion["url"]
    tipo, item_id = extraer_tipo_e_id(url)
    try:
        if tipo == "file":
            info = pedir_json_resistente(f"/file/{item_id}/info", url)
            file_id = item_id
            pd_nombre = info.get("name") or f"{item_id}.bin"
        else:
            archivos = pixeldrain_video_files(url)
            archivo_target = None
            for archivo in archivos:
                nombre = archivo.get("name") or ""
                info = parsear_nombre_descargado(nombre)
                if info and info["episode_in_arc"] == episode_number:
                    archivo_target = archivo
                    break
            if not archivo_target:
                job_update(job_id, status="error", msg=f"Episodio {episode_number} no encontrado en Pixeldrain.")
                return
            file_id = archivo_target["id"]
            pd_nombre = archivo_target.get("name") or f"{file_id}.bin"
    except Exception as e:
        job_update(job_id, status="error", msg=str(e))
        return

    existente = archivo_ya_existe_en_destino_final(pd_nombre, output_dir, indice_metadatos, arc["season_number"])
    if existente:
        job_update(job_id, status="done", msg="Episodio ya descargado.")
        return
    session = crear_sesion()
    tmp_dir = output_dir / "_tmp" / slugify(arc["id"])

    def progress_cb(downloaded: int, total_bytes: int):
        job_update(job_id, file_progress=downloaded, file_total=total_bytes)

    cancel_ev = job_cancel_event(job_id)
    try:
        ruta = descargar_archivo_reanudable(file_id, pd_nombre, tmp_dir, session, url, progress_cb, cancel_ev)
        renombrar_y_copiar_nfo_segun_metadata(ruta, output_dir, indice_metadatos, arc["season_number"])
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="done", msg=f"Episodio {episode_number} descargado.")
    except InterruptedError:
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="cancelled")
    except Exception as e:
        limpiar_temporales_si_ok(output_dir)
        job_update(job_id, status="error", msg=str(e))


# ── Catalog ────────────────────────────────────────────────────────────────────

def cargar_catalogo(config: dict) -> list[dict]:
    global _catalog_cache
    now = time.time()
    if _catalog_cache["data"] is not None and (now - _catalog_cache["ts"]) < CATALOG_TTL:
        arcs_base = _catalog_cache["data"]
    else:
        html = obtener_html(config["url"])
        temporadas = extraer_temporadas_y_pixeldrain(html)
        arcs_base = agrupar_por_temporada(temporadas)
        _catalog_cache = {"data": arcs_base, "ts": now}

    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()

    result = []
    for arc in arcs_base:
        a = dict(arc)
        a["elegida"] = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
        a["descargados"] = contar_locales(output_dir, arc["season_number"])
        season_meta = indice_metadatos.get(arc["season_number"])
        a["total_meta"] = len(season_meta["episodes"]) if season_meta else None
        a["season_title"] = season_meta["season_title"] if season_meta else arc["id"]
        result.append(a)
    return result


def cargar_arc_por_numero(season_number: int, config: dict) -> dict | None:
    catalogo = cargar_catalogo(config)
    return next((x for x in catalogo if x["season_number"] == season_number), None)


def cargar_detalle_temporada(season_number: int, config: dict) -> dict | None:
    arc = cargar_arc_por_numero(season_number, config)
    if not arc:
        return None
    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    season_meta = indice_metadatos.get(season_number)
    available_episodes = None
    if arc.get("elegida"):
        try:
            available_episodes = pixeldrain_available_episodes(arc["elegida"]["url"])
        except Exception:
            available_episodes = None
    episodes = []
    if season_meta:
        for ep_num, ep_nfo in sorted(season_meta["episodes"].items()):
            ep_info = leer_episodio_nfo(ep_nfo)
            downloaded = episodio_descargado(output_dir, season_number, ep_num)
            episodes.append({
                "number": ep_num,
                "title": ep_info["title"],
                "plot": ep_info["plot"],
                "aired": ep_info["aired"],
                "downloaded": downloaded,
                "available": ep_num in available_episodes if available_episodes is not None else arc.get("elegida") is not None,
            })
    return {"arc": arc, "season_meta": season_meta, "episodes": episodes}


# ── CSS / JS ───────────────────────────────────────────────────────────────────

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;
  --accent:#f6a200;--accent-dim:rgba(246,162,0,.15);
  --text:#e6edf3;--muted:#8b949e;--success:#3fb950;--danger:#f85149;
  --radius:12px;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:inherit;text-decoration:none}

/* Nav */
.navbar{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:58px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;gap:16px}
.nav-brand{font-size:1.1rem;font-weight:700;color:var(--accent);display:flex;align-items:center;gap:10px;white-space:nowrap}
.nav-logo{height:30px;width:auto;border-radius:6px;object-fit:cover}
.nav-links{display:flex;gap:4px}
.nav-links a{padding:6px 14px;border-radius:8px;font-size:.875rem;color:var(--muted);transition:background .15s,color .15s}
.nav-links a:hover{background:var(--surface2);color:var(--text)}
.nav-links a.active{background:var(--accent-dim);color:var(--accent)}
.nav-form-link{padding:6px 14px;border-radius:8px;font-size:.875rem;color:var(--muted);transition:background .15s,color .15s;background:transparent;border:0;cursor:pointer;font-family:inherit}
.nav-form-link:hover{background:var(--surface2);color:var(--text)}
.nav-form-link.active{background:var(--accent-dim);color:var(--accent)}

/* Flash */
.flash-wrap{padding:12px 24px 0}
.flash{display:flex;align-items:center;gap:10px;background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:var(--radius);padding:12px 16px;font-size:.875rem;margin-bottom:8px}
.flash.err{border-left-color:var(--danger)}

/* Main */
.main{padding:32px 24px;max-width:1400px;margin:0 auto}

/* Buttons */
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 18px;border-radius:8px;border:none;cursor:pointer;font-size:.875rem;font-weight:500;transition:opacity .15s,transform .1s;white-space:nowrap}
.btn:hover{opacity:.85}
.btn:active{transform:scale(.98)}
.btn-primary{background:var(--accent);color:#000}
.btn-danger{background:var(--danger);color:#fff}
.btn-ghost{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-sm{padding:5px 12px;font-size:.8rem}
.btn:disabled{opacity:.4;cursor:not-allowed;pointer-events:none}

/* Forms */
.form-group{margin-bottom:18px}
.form-group label{display:block;margin-bottom:5px;font-size:.8rem;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
.form-group input,.form-group select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:.9rem;transition:border-color .15s}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--accent)}
.form-hint{font-size:.75rem;color:var(--muted);margin-top:4px}
.form-errors{background:rgba(248,81,73,.1);border:1px solid var(--danger);border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:.875rem;color:var(--danger)}
.form-errors li{margin-left:16px;margin-top:4px}

/* Setup wizard */
.wizard{max-width:600px;margin:60px auto}
.wizard-hero{text-align:center;margin-bottom:40px}
.wizard-hero h1{font-size:2rem;color:var(--accent);margin-bottom:8px}
.wizard-hero p{color:var(--muted);font-size:.95rem}
.wizard-logo{width:80px;height:80px;border-radius:16px;object-fit:cover;margin:0 auto 20px;display:block}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:28px}
.card+.card{margin-top:16px}
.card h2{font-size:1rem;margin-bottom:20px;display:flex;align-items:center;gap:10px}
.card h2 .step-num{width:28px;height:28px;border-radius:50%;background:var(--accent);color:#000;font-size:.8rem;font-weight:700;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.card h2 .step-num.done{background:var(--success)}
.card h2 .step-num.locked{background:var(--surface2);color:var(--muted)}
.card-footer{display:flex;gap:10px;margin-top:20px;flex-wrap:wrap}
.sync-info{background:var(--surface2);border-radius:8px;padding:12px 14px;font-size:.8rem;color:var(--muted);margin-bottom:16px;line-height:1.5}

/* Catalog */
.page-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;gap:16px;flex-wrap:wrap}
.page-header h1{font-size:1.4rem;font-weight:700}
.page-header .sub{color:var(--muted);font-size:.875rem}
.season-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:18px}
.season-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;transition:transform .2s,border-color .2s;display:block;cursor:pointer}
.season-card:hover{transform:translateY(-4px);border-color:var(--accent)}
.season-card .poster-wrap{position:relative;aspect-ratio:2/3;background:var(--surface2);overflow:hidden}
.season-card .poster-wrap img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.season-card .poster-wrap .no-poster{position:absolute;inset:0;width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:2rem;font-weight:700;color:var(--muted)}
.season-card .poster-wrap .status-dot{position:absolute;top:8px;right:8px;width:10px;height:10px;border-radius:50%;border:2px solid var(--surface)}
.status-dot.full{background:var(--success)}
.status-dot.partial{background:var(--accent)}
.status-dot.none{background:var(--surface2)}
.season-card .card-body{padding:10px 12px 12px}
.season-card .card-num{font-size:.7rem;color:var(--muted);font-weight:600;margin-bottom:2px}
.season-card .card-title{font-size:.82rem;font-weight:600;line-height:1.35;margin-bottom:6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.season-card .prog-wrap{display:flex;align-items:center;gap:6px}
.season-card .prog-bar{flex:1;height:3px;background:var(--surface2);border-radius:99px;overflow:hidden}
.season-card .prog-bar .fill{height:100%;background:var(--accent);border-radius:99px}
.season-card .prog-text{font-size:.7rem;color:var(--muted);white-space:nowrap}

/* Season detail */
.back-link{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:.875rem;margin-bottom:24px;transition:color .15s}
.back-link:hover{color:var(--text)}
.season-hero{display:flex;gap:28px;margin-bottom:32px}
.season-hero .s-poster{width:180px;flex-shrink:0;border-radius:var(--radius);overflow:hidden;aspect-ratio:2/3;background:var(--surface2)}
.season-hero .s-poster img{width:100%;height:100%;object-fit:cover;display:block}
.season-hero .s-info{flex:1;min-width:0}
.season-hero .s-num{font-size:.8rem;color:var(--muted);font-weight:600;margin-bottom:4px}
.season-hero .s-title{font-size:1.6rem;font-weight:700;line-height:1.2;margin-bottom:12px}
.season-hero .s-stats{display:flex;gap:20px;margin-bottom:20px;flex-wrap:wrap}
.stat{display:flex;flex-direction:column;gap:2px}
.stat .stat-val{font-size:1.2rem;font-weight:700}
.stat .stat-lbl{font-size:.75rem;color:var(--muted)}
.season-actions{display:flex;gap:10px;flex-wrap:wrap}

/* Episode list */
.ep-section-header{font-size:.8rem;font-weight:600;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin:28px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.ep-list{display:flex;flex-direction:column;gap:8px}
.ep-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;display:flex;align-items:center;gap:14px;padding:12px 16px;transition:border-color .15s}
.ep-card:hover{border-color:var(--border);background:var(--surface2)}
.ep-card.ep-downloaded{border-left:3px solid var(--success)}
.ep-check{width:18px;height:18px;accent-color:var(--accent);flex-shrink:0}
.ep-num{font-size:.9rem;font-weight:700;color:var(--muted);min-width:36px;text-align:center;flex-shrink:0}
.ep-body{flex:1;min-width:0}
.ep-title{font-size:.875rem;font-weight:600;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ep-plot{font-size:.75rem;color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.4}
.ep-meta{font-size:.72rem;color:var(--muted);margin-top:4px}
.ep-actions{flex-shrink:0;display:flex;align-items:center;gap:8px}
.ep-badge{font-size:.7rem;font-weight:600;padding:3px 8px;border-radius:99px;white-space:nowrap}
.ep-badge.ok{background:rgba(63,185,80,.15);color:var(--success)}
.ep-badge.pending{background:var(--surface2);color:var(--muted)}
.ep-badge.watched{background:rgba(246,162,0,.15);color:var(--accent);cursor:pointer}
.ep-badge.unwatched{background:var(--surface2);color:var(--muted);cursor:pointer;opacity:.7}
.no-av{font-size:.72rem;color:var(--muted);font-style:italic}

/* Job bar */
#job-bar{position:fixed;bottom:0;left:0;right:0;background:var(--surface);border-top:1px solid var(--border);z-index:200;display:none}
#job-bar.visible{display:block}
#job-detail{max-height:0;overflow:hidden;transition:max-height .28s ease}
#job-detail.expanded{max-height:340px;overflow-y:auto}
#job-summary{padding:8px 20px;cursor:pointer;user-select:none}
#job-summary:hover{background:rgba(255,255,255,.03)}
.job-sum-inner{display:flex;align-items:center;gap:10px}
.job-sum-label{font-size:.82rem;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job-toggle{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.75rem;padding:2px 8px;border-radius:4px;line-height:1.4}
.job-toggle:hover{background:var(--surface2);color:var(--text)}
.job-stop{background:none;border:1px solid var(--danger);color:var(--danger);cursor:pointer;font-size:.72rem;padding:2px 8px;border-radius:4px;line-height:1.4;white-space:nowrap}
.job-stop:hover{background:rgba(248,81,73,.15)}
.job-err-badge{font-size:.72rem;color:var(--danger);background:rgba(248,81,73,.1);padding:2px 8px;border-radius:99px;white-space:nowrap;flex-shrink:0}
.job-row{display:flex;align-items:center;gap:10px;padding:7px 20px;border-top:1px solid var(--border)}
.job-row-left{flex:1;min-width:0}
.job-lbl{font-size:.82rem;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job-sub-label{font-size:.7rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.job-prog{width:70px;height:3px;background:var(--surface2);border-radius:99px;overflow:hidden;flex-shrink:0}
.job-prog .fill{height:100%;background:var(--accent);border-radius:99px;transition:width .4s}
.job-st{font-size:.7rem;color:var(--muted);min-width:100px;text-align:right;flex-shrink:0;white-space:nowrap}
.job-err{font-size:.78rem;color:var(--danger);padding:7px 20px;border-top:1px solid var(--border)}

/* Episode inline download progress */
.ep-dl-wrap{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.ep-dl-progress{display:flex;align-items:center;gap:6px}
.ep-dl-bar{width:80px;height:3px;background:var(--surface2);border-radius:99px;overflow:hidden}
.ep-dl-bar .fill{height:100%;background:var(--accent);border-radius:99px;width:0%;transition:width .4s}
.ep-dl-label{font-size:.68rem;color:var(--muted);white-space:nowrap}

/* Job files list in detail panel */
.job-card{border-top:1px solid var(--border);padding:7px 20px}
.job-card-hdr{display:flex;align-items:center;gap:10px}
.job-card-hdr .job-lbl{flex:1;font-size:.82rem}
.job-count{font-size:.7rem;color:var(--muted);margin:2px 0 4px}
.job-files{display:flex;flex-direction:column;gap:3px}
.jf-row{display:flex;align-items:center;gap:6px;font-size:.7rem;white-space:nowrap;overflow:hidden}
.jf-name{flex:1;overflow:hidden;text-overflow:ellipsis;min-width:0}
.jf-size{flex-shrink:0;font-size:.65rem;opacity:.8}
.jf-done{color:var(--success)}
.jf-active{color:var(--accent)}
.jf-pending{color:var(--muted)}
.job-prog.sm{width:50px;height:2px;flex-shrink:0}

/* Season card downloading pulse */
.season-card.dl-active .prog-bar .fill{animation:dl-pulse 1.4s ease-in-out infinite alternate}
@keyframes dl-pulse{to{opacity:.35}}

/* Responsive */
@media(max-width:640px){
  .season-hero{flex-direction:column}
  .season-hero .s-poster{width:120px}
  .main{padding:20px 16px}
  .navbar{padding:0 14px}
  .ep-plot{display:none}
  .season-grid{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}
}
"""

JS = """
let _hadRunning=false,_expanded=false,_lastJobs={};
function _esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function _fb(b){if(!b||b<1)return'';if(b>1e9)return(b/1e9).toFixed(1)+' GB';if(b>1e6)return(b/1e6).toFixed(1)+' MB';return(b/1e3).toFixed(0)+' KB';}
function _pct(j){
  if(j.bytes_total>0)return Math.round(j.bytes_progress/j.bytes_total*100);
  if(j.file_total>0)return Math.round(j.file_progress/j.file_total*100);
  return 0;
}
function _stat(j){
  if(j.bytes_total>0)return _fb(j.bytes_progress)+' / '+_fb(j.bytes_total);
  if(j.file_total>0)return _fb(j.file_progress)+' / '+_fb(j.file_total);
  return '';
}
function _jobRow(id,j){
  const p=_pct(j),s=_stat(j);
  const cancel='<button class="job-toggle" onclick="_jobCancel(\''+_esc(id)+'\')">Cancelar</button>';
  if(j.files&&j.files.length>0){
    // Season job: header with overall bar, then file list below full-width
    const filesHtml=j.files.map(f=>{
      if(f.done)return '<div class="jf-row jf-done">✓ <span class="jf-name">'+_esc(f.name)+'</span><span class="jf-size">'+_fb(f.size)+'</span></div>';
      if(f.progress>0){const fp=f.size>0?Math.round(f.progress/f.size*100):0;return '<div class="jf-row jf-active">⬇ <span class="jf-name">'+_esc(f.name)+'</span><div class="job-prog sm"><div class="fill" style="width:'+fp+'%"></div></div><span class="jf-size">'+_fb(f.progress)+' / '+_fb(f.size)+'</span></div>';}
      return '<div class="jf-row jf-pending">○ <span class="jf-name">'+_esc(f.name)+'</span><span class="jf-size">'+_fb(f.size)+'</span></div>';
    }).join('');
    return '<div class="job-card"><div class="job-card-hdr"><span class="job-lbl">'+_esc(j.label)+'</span><div class="job-prog"><div class="fill" style="width:'+p+'%"></div></div><span class="job-st">'+(j.status==='queued'?'En cola':(p>0?p+'%':'…')+(s?' · '+s:''))+'</span>'+cancel+'</div><div class="job-count">'+j.progress+'/'+j.total+' archivos</div><div class="job-files">'+filesHtml+'</div></div>';
  }
  // Episode job: simple single row
  return '<div class="job-row"><div class="job-row-left"><span class="job-lbl">'+_esc(j.label)+'</span></div><div class="job-prog"><div class="fill" style="width:'+p+'%"></div></div><span class="job-st">'+(j.status==='queued'?'En cola':(p>0?p+'%':'…')+(s?' · '+s:''))+'</span>'+cancel+'</div>';
}
function _toggleJobBar(){
  _expanded=!_expanded;
  document.getElementById('job-detail').classList.toggle('expanded',_expanded);
  _render(_lastJobs);
}
function _jobCancel(id){
  fetch('/api/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST',headers:{'X-CSRF-Token':window.OPDES_CSRF_TOKEN||''}}).catch(()=>{});
}
function _jobRetry(id){
  fetch('/api/jobs/'+encodeURIComponent(id)+'/retry',{method:'POST',headers:{'X-CSRF-Token':window.OPDES_CSRF_TOKEN||''}}).catch(()=>{});
}
function _cancelAll(){
  fetch('/api/jobs/cancel',{method:'POST',headers:{'X-CSRF-Token':window.OPDES_CSRF_TOKEN||''}}).catch(()=>{});
}
function _render(jobs){
  _lastJobs=jobs;
  const bar=document.getElementById('job-bar'),sum=document.getElementById('job-summary'),det=document.getElementById('job-detail');
  if(!bar)return;
  const running=Object.entries(jobs).filter(([,v])=>v.status==='running'||v.status==='queued');
  const errors=Object.entries(jobs).filter(([,v])=>v.status==='error');
  if(!running.length&&!errors.length){
    if(_hadRunning){_hadRunning=false;}
    bar.classList.remove('visible');return;
  }
  _hadRunning=running.length>0;
  bar.classList.add('visible');
  const totalB=running.reduce((s,[,j])=>s+(j.bytes_total||j.file_total||0),0);
  const doneB=running.reduce((s,[,j])=>s+(j.bytes_progress||j.file_progress||0),0);
  const avgPct=totalB>0?Math.round(doneB/totalB*100):(running.length?Math.round(running.reduce((s,[,j])=>s+_pct(j),0)/running.length):0);
  const sizeStr=totalB>0?_fb(doneB)+' / '+_fb(totalB):'';
  const errBadge=errors.length?'<span class="job-err-badge">'+errors.length+' error'+(errors.length>1?'es':'')+'</span>':'';
  const lbl=running.length?'⬇ '+running.length+' descarga'+(running.length>1?'s':'')+' activas':(errors.length+' error'+(errors.length>1?'es':''));
  const stopBtn=running.length?'<button class="job-stop" onclick="_cancelAll()">⏹ Detener</button>':'';
  sum.innerHTML='<div class="job-sum-inner"><span class="job-sum-label">'+lbl+'</span>'+(running.length?'<div class="job-prog"><div class="fill" style="width:'+avgPct+'%"></div></div><span class="job-st">'+avgPct+'%'+(sizeStr?' · '+sizeStr:'')+'</span>':'')+errBadge+stopBtn+'<button class="job-toggle" onclick="_toggleJobBar()">'+(_expanded?'▼':'▲')+'</button></div>';
  det.innerHTML=running.map(([id,j])=>_jobRow(id,j)).join('')+errors.map(([id,j])=>'<div class="job-err">✕ '+_esc(j.label)+': '+_esc(j.msg)+' <button class="job-toggle" onclick="_jobRetry(\''+_esc(id)+'\')">Reintentar</button></div>').join('');
  document.querySelectorAll('[data-job-id]').forEach(el=>{
    const j=jobs[el.dataset.jobId];
    if(!j||j.status!=='running')return;
    const fill=el.querySelector('.ep-dl-bar .fill'),lbl=el.querySelector('.ep-dl-label');
    if(!fill||!lbl)return;
    fill.style.width=_pct(j)+'%';
    lbl.textContent=_stat(j)||'…';
  });
  document.querySelectorAll('.season-card[data-season]').forEach(card=>{
    const sj=jobs['season-'+card.dataset.season];
    card.classList.toggle('dl-active',!!(sj&&(sj.status==='running'||sj.status==='queued')));
  });
}
function _poll(){
  fetch('/api/jobs').then(r=>r.json()).then(jobs=>{
    _render(jobs);
    const active=Object.values(jobs).some(j=>j.status==='running'||j.status==='queued');
    setTimeout(_poll,active?1000:15000);
  }).catch(()=>setTimeout(_poll,15000));
}
document.addEventListener('DOMContentLoaded',_poll);
"""


def render(template: str, **ctx) -> str:
    return render_template_string(template, css=CSS, js=JS, csrf_token=csrf_token(), csrf_input=csrf_input, **ctx)


# ── Templates ──────────────────────────────────────────────────────────────────

LAYOUT = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{% block title %}ONE PACE DES{% endblock %}</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg">
  <style>{{ css|safe }}</style>
</head>
<body>
<nav class="navbar">
  <a href="{{ url_for('home') }}" class="nav-brand">
    <img class="nav-logo" src="{{ url_for('img_show_poster') }}" onerror="this.style.display='none'" alt="">
    ONE PACE DES
  </a>
  <div class="nav-links">
    <a href="{{ url_for('home') }}" {% if active=='home' %}class="active"{% endif %}>Biblioteca</a>
    <a href="{{ url_for('setup') }}" {% if active=='setup' %}class="active"{% endif %}>Configuración</a>
    <form method="post" action="{{ url_for('sync_metadata_route') }}" style="display:inline">{{ csrf_input()|safe }}<button class="nav-form-link {% if active=='sync' %}active{% endif %}" type="submit">Sincronizar</button></form>
    {% if session.get('opdes_admin_authenticated') %}<a href="{{ url_for('logout') }}">Salir</a>{% endif %}
  </div>
</nav>
{% with msgs = get_flashed_messages(with_categories=true) %}{% if msgs %}
<div class="flash-wrap">
{% for cat, msg in msgs %}<div class="flash {% if cat=='error' %}err{% endif %}">{{ msg }}</div>{% endfor %}
</div>{% endif %}{% endwith %}
<div class="main">{{ content|safe }}</div>
<div id="job-bar"><div id="job-detail"></div><div id="job-summary" onclick="_toggleJobBar()"></div></div>
<script>window.OPDES_CSRF_TOKEN={{ csrf_token|tojson }};</script>
<script>{{ js|safe }}</script>
</body>
</html>"""


def page(content: str, title: str = "ONE PACE DES", active: str = "") -> str:
    return render(LAYOUT, content=content, active=active, page_title=title)


# ── Auth / request guards ──────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    return response


@app.get("/login")
def login():
    if not auth_required() or is_authenticated():
        return redirect(url_for("home"))
    content = f"""
<div class="wizard">
  <div class="wizard-hero">
    <h1>OPDES Web</h1>
    <p>Acceso administrativo</p>
  </div>
  <div class="card">
    <form method="post" action="{url_for('login')}">
      {csrf_input()}
      <div class="form-group">
        <label for="login-username">Usuario</label>
        <input id="login-username" name="username" autocomplete="username">
      </div>
      <div class="form-group">
        <label for="login-password">Contraseña o token admin</label>
        <input id="login-password" name="password" type="password" autocomplete="current-password">
      </div>
      <div class="card-footer">
        <button class="btn btn-primary" type="submit">Entrar</button>
      </div>
    </form>
  </div>
</div>
"""
    return page(content, title="Login", active="")


@app.post("/login")
def login_submit():
    if not auth_required():
        return redirect(url_for("home"))
    validate_csrf()
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if validate_login(username, password):
        session.clear()
        session["opdes_admin_authenticated"] = True
        csrf_token()
        return redirect(url_for("home"))
    flash("Credenciales inválidas.", "error")
    return redirect(url_for("login"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Jellyfin integration ───────────────────────────────────────────────────────

_jf_user_cache: dict = {"id": None, "ts": 0.0}
_jf_series_cache: dict = {"id": None, "ts": 0.0}
_jf_seasons_cache: dict = {"data": None, "ts": 0.0}


def _jf_headers(token: str) -> dict:
    return {"X-Emby-Authorization": f'MediaBrowser Token="{token}"'}


def jellyfin_get_user_id(config: dict) -> str | None:
    now = time.time()
    if _jf_user_cache["id"] and now - _jf_user_cache["ts"] < 300:
        return _jf_user_cache["id"]
    url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    username = str(config.get("jellyfin_user", "")).strip().lower()
    if not url or not token:
        return None
    try:
        resp = requests.get(f"{url}/Users", headers=_jf_headers(token), timeout=5)
        resp.raise_for_status()
        users = resp.json()
        user_id = None
        if username:
            for u in users:
                if u.get("Name", "").lower() == username:
                    user_id = u["Id"]
                    break
        if not user_id:
            for u in users:
                if u.get("Policy", {}).get("IsAdministrator", False):
                    user_id = u["Id"]
                    break
        if not user_id and users:
            user_id = users[0]["Id"]
        _jf_user_cache["id"] = user_id
        _jf_user_cache["ts"] = now
        return user_id
    except Exception:
        return None


def jellyfin_get_series_id(config: dict) -> str | None:
    now = time.time()
    if _jf_series_cache["id"] and now - _jf_series_cache["ts"] < 3600:
        return _jf_series_cache["id"]
    url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    series_name = str(config.get("jellyfin_series", "One Piece")).strip()
    if not url or not token:
        return None
    try:
        resp = requests.get(
            f"{url}/Items",
            params={"IncludeItemTypes": "Series", "Recursive": "true", "SearchTerm": series_name, "Fields": "Id,Name", "Limit": 10},
            headers=_jf_headers(token),
            timeout=5,
        )
        resp.raise_for_status()
        items = resp.json().get("Items", [])
        series_id = None
        for item in items:
            if item.get("Name", "").lower() == series_name.lower():
                series_id = item["Id"]
                break
        if not series_id and items:
            series_id = items[0]["Id"]
        _jf_series_cache["id"] = series_id
        _jf_series_cache["ts"] = now
        return series_id
    except Exception:
        return None


def jellyfin_get_season_id(season: int, config: dict) -> str | None:
    now = time.time()
    if _jf_seasons_cache["data"] is not None and now - _jf_seasons_cache["ts"] < 3600:
        return _jf_seasons_cache["data"].get(season)
    url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    if not url or not token:
        return None
    user_id = jellyfin_get_user_id(config)
    series_id = jellyfin_get_series_id(config)
    if not user_id or not series_id:
        return None
    try:
        resp = requests.get(
            f"{url}/Shows/{series_id}/Seasons",
            params={"UserId": user_id, "Fields": "Id,IndexNumber"},
            headers=_jf_headers(token),
            timeout=5,
        )
        resp.raise_for_status()
        mapping: dict[int, str] = {}
        for item in resp.json().get("Items", []):
            idx = item.get("IndexNumber")
            if idx is not None:
                mapping[idx] = item["Id"]
        _jf_seasons_cache["data"] = mapping
        _jf_seasons_cache["ts"] = now
        return mapping.get(season)
    except Exception:
        return None


def jellyfin_season_data(season: int, config: dict) -> dict[int, dict]:
    url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    if not url or not token:
        return {}
    user_id = jellyfin_get_user_id(config)
    season_id = jellyfin_get_season_id(season, config)
    if not user_id or not season_id:
        return {}
    try:
        resp = requests.get(
            f"{url}/Items",
            params={
                "ParentId": season_id,
                "UserId": user_id,
                "Fields": "Id,IndexNumber,UserData",
                "IncludeItemTypes": "Episode",
            },
            headers=_jf_headers(token),
            timeout=5,
        )
        resp.raise_for_status()
        result = {}
        for item in resp.json().get("Items", []):
            ep_num = item.get("IndexNumber")
            if ep_num is not None:
                result[ep_num] = {
                    "id": item["Id"],
                    "played": item.get("UserData", {}).get("Played", False),
                }
        return result
    except Exception:
        return {}


def jellyfin_find_episode(season: int, episode: int, config: dict) -> str | None:
    ep_data = jellyfin_season_data(season, config).get(episode)
    return ep_data["id"] if ep_data else None


def _toggle_jellyfin_watched(season: int, episode: int, *, as_json: bool = False):
    config = cargar_config()
    jellyfin_url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    if not jellyfin_url or not token:
        if as_json:
            return jsonify({"ok": False, "error": "Configura Jellyfin primero."}), 400
        flash("Configura Jellyfin primero.")
        return redirect(url_for("setup"))
    user_id = jellyfin_get_user_id(config)
    jf_data = jellyfin_season_data(season, config)
    ep_data = jf_data.get(episode)
    if not ep_data or not user_id:
        msg = f"Episodio S{season:02d}E{episode:02d} no encontrado en Jellyfin."
        if as_json:
            return jsonify({"ok": False, "error": msg}), 404
        flash(msg)
        return redirect(url_for("season_detail", n=season))
    item_id = ep_data["id"]
    played = ep_data["played"]
    headers = {"X-Emby-Authorization": f'MediaBrowser Token="{token}"'}
    try:
        if played:
            requests.delete(f"{jellyfin_url}/Users/{user_id}/PlayedItems/{item_id}", headers=headers, timeout=5)
        else:
            requests.post(f"{jellyfin_url}/Users/{user_id}/PlayedItems/{item_id}", headers=headers, timeout=5)
    except Exception:
        if as_json:
            return jsonify({"ok": False, "error": "Error al actualizar el estado en Jellyfin."}), 502
        flash("Error al actualizar el estado en Jellyfin.")
    if as_json:
        return jsonify({"ok": True, "played": not played})
    return redirect(url_for("season_detail", n=season))


# ── Image routes ───────────────────────────────────────────────────────────────

@app.get("/favicon.svg")
def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 80">'
        # crown
        '<ellipse cx="50" cy="33" rx="26" ry="21" fill="#e8b84b"/>'
        # red ribbon
        '<rect x="23" y="47" width="54" height="9" rx="2" fill="#dc2626"/>'
        # brim
        '<ellipse cx="50" cy="59" rx="46" ry="11" fill="#e8b84b"/>'
        # outlines
        '<ellipse cx="50" cy="33" rx="26" ry="21" fill="none" stroke="#b8860b" stroke-width="2"/>'
        '<ellipse cx="50" cy="59" rx="46" ry="11" fill="none" stroke="#b8860b" stroke-width="2"/>'
        '</svg>'
    )
    return app.response_class(svg, mimetype="image/svg+xml")


@app.get("/img/show/poster")
def img_show_poster():
    config = cargar_config()
    md = str(config.get("metadata_dir", "")).strip()
    if md:
        p = Path(md).expanduser()
        for name in ["poster.png", "poster-2.png", "poster.jpg"]:
            f = p / name
            if f.exists():
                return send_file(f)
    return ("", 404)


@app.get("/img/show/backdrop")
def img_show_backdrop():
    config = cargar_config()
    md = str(config.get("metadata_dir", "")).strip()
    if md:
        p = Path(md).expanduser()
        for name in ["backdrop.jpg", "backdrop-2.jpg", "backdrop.png"]:
            f = p / name
            if f.exists():
                return send_file(f)
    return ("", 404)


@app.get("/img/season/<int:n>/poster")
def img_season_poster(n: int):
    config = cargar_config()
    md = str(config.get("metadata_dir", "")).strip()
    if md:
        season_dir = Path(md).expanduser() / f"Season {n}"
        for name in ["poster.png", "folder.jpg", "folder.png"]:
            f = season_dir / name
            if f.exists():
                return send_file(f)
    return ("", 404)


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def api_jobs():
    snap = jobs_snapshot()
    return jsonify(snap)


@app.post("/api/jobs/clear")
def api_jobs_clear():
    jobs_clear_done()
    return jsonify({"ok": True})


@app.post("/api/jobs/cancel")
def api_jobs_cancel():
    with _jobs_lock:
        cancellable = [jid for jid, j in _jobs.items() if j["status"] in {"queued", "running"}]
    cancelled = [jid for jid in cancellable if job_cancel(jid)]
    return jsonify({"ok": True, "cancelled": cancelled})


@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id: str):
    if not job_cancel(job_id):
        return jsonify({"ok": False, "error": "Job no encontrado o no cancelable."}), 404
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/jobs/<job_id>/retry")
def api_job_retry(job_id: str):
    if not job_retry(job_id):
        return jsonify({"ok": False, "error": "Job no encontrado o no reintentable."}), 404
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/catalog")
def api_catalog():
    config = cargar_config()
    try:
        return jsonify({"ok": True, "catalog": cargar_catalogo(config)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/season/<int:n>")
def api_season(n: int):
    config = cargar_config()
    try:
        detalle = cargar_detalle_temporada(n, config)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not detalle:
        return jsonify({"ok": False, "error": "Temporada no encontrada."}), 404
    detalle["season_meta"] = None
    return jsonify({"ok": True, "season": detalle})


@app.post("/api/download/season/<int:n>")
def api_download_season(n: int):
    return _start_download_season(n, as_json=True)


@app.post("/api/download/season/<int:n>/episode/<int:e>")
def api_download_episode(n: int, e: int):
    return _start_download_episode(n, e, as_json=True)


@app.post("/api/download/season/<int:n>/episodes")
def api_download_episodes(n: int):
    payload = request.get_json(silent=True) or {}
    raw_episodes = payload.get("episodes") or request.form.getlist("episodes")
    try:
        episodes = sorted({int(e) for e in raw_episodes})
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Lista de episodios no válida."}), 400
    return _start_download_episodes(n, episodes, as_json=True)


@app.post("/api/delete/season/<int:n>")
def api_delete_season(n: int):
    return _delete_season(n, as_json=True)


@app.post("/api/delete/season/<int:n>/episode/<int:e>")
def api_delete_episode(n: int, e: int):
    return _delete_episode(n, e, as_json=True)


@app.post("/api/jellyfin/watched/<int:season>/<int:episode>")
def api_jellyfin_toggle_watched(season: int, episode: int):
    return _toggle_jellyfin_watched(season, episode, as_json=True)


@app.get("/jellyfin/play/<int:season>/<int:episode>")
def jellyfin_play(season: int, episode: int):
    config = cargar_config()
    jellyfin_url = str(config.get("jellyfin_url", "")).rstrip("/")
    if not jellyfin_url:
        flash("Configura la URL de Jellyfin en los ajustes.")
        return redirect(url_for("setup"))
    item_id = jellyfin_find_episode(season, episode, config)
    if item_id:
        return redirect(f"{jellyfin_url}/web/index.html#!/details?id={item_id}")
    return redirect(f"{jellyfin_url}/web/index.html")


@app.post("/jellyfin/watched/<int:season>/<int:episode>")
def jellyfin_toggle_watched(season: int, episode: int):
    return _toggle_jellyfin_watched(season, episode)


# ── Setup ──────────────────────────────────────────────────────────────────────

@app.get("/setup")
def setup():
    config = cargar_config()
    cfg_ok = config_completa(config)
    meta_ok = metadatos_ok(config)

    step1_done = cfg_ok
    step2_done = meta_ok

    html = """
<div class="wizard">
  <div class="wizard-hero">
    <img class="wizard-logo" src="/img/show/poster" onerror="this.style.display='none'" alt="">
    <h1>OPDES Web</h1>
    <p>Descargador de One Pace para Jellyfin</p>
  </div>
"""

    # Step 1 – Directorios
    step1_num = '<span class="step-num done">✓</span>' if step1_done else '<span class="step-num">1</span>'
    errors_cfg = []
    if not str(config.get("output_dir", "")).strip():
        errors_cfg.append("Falta la carpeta de episodios.")
    if not str(config.get("metadata_dir", "")).strip():
        errors_cfg.append("Falta la carpeta de metadatos.")
    errors_html = ""
    if errors_cfg and not step1_done:
        errors_html = '<div class="form-errors"><ul>' + "".join(f"<li>{e}</li>" for e in errors_cfg) + "</ul></div>"

    q = config.get("quality", "max")
    opts = "".join(
        f'<option value="{v}" {"selected" if q == v else ""}>{v}</option>'
        for v in ["max", "1080p", "720p", "480p"]
    )
    token_status = "Token configurado. Déjalo vacío para conservarlo." if config.get("jellyfin_token") else "Pega un token nuevo para activar Jellyfin."

    html += f"""
  <div class="card">
    <h2>{step1_num} Directorios y calidad</h2>
    {errors_html}
    <form method="post" action="/setup">
      {csrf_input()}
      <div class="form-group">
        <label for="setup-output-dir">Carpeta de episodios</label>
        <input id="setup-output-dir" name="output_dir" value="{escape(config.get('output_dir',''))}" placeholder="/media/Series/One Pace">
        <div class="form-hint">Aquí se guardarán los vídeos descargados.</div>
      </div>
      <div class="form-group">
        <label for="setup-metadata-dir">Carpeta de metadatos</label>
        <input id="setup-metadata-dir" name="metadata_dir" value="{escape(config.get('metadata_dir',''))}" placeholder="/srv/opdes/metadatos">
        <div class="form-hint">NFOs, pósters y carátulas de temporadas y episodios.</div>
      </div>
      <div class="form-group">
        <label for="setup-url">URL One Pace</label>
        <input id="setup-url" name="url" value="{escape(config.get('url',DEFAULT_CONFIG['url']))}">
      </div>
      <div class="form-group">
        <label for="setup-quality">Calidad preferida</label>
        <select id="setup-quality" name="quality">{opts}</select>
      </div>
      <div class="form-group">
        <label for="setup-jellyfin-url">URL de Jellyfin</label>
        <input id="setup-jellyfin-url" name="jellyfin_url" value="{escape(config.get('jellyfin_url',''))}" placeholder="http://192.168.1.204:8096">
        <div class="form-hint">Para el botón de reproducción directa desde los episodios.</div>
      </div>
      <div class="form-group">
        <label for="setup-jellyfin-token">API Token de Jellyfin</label>
        <input id="setup-jellyfin-token" name="jellyfin_token" value="" placeholder="Nuevo token de API de Jellyfin" autocomplete="off">
        <div class="form-hint">{token_status}</div>
      </div>
      <div class="form-group">
        <label for="setup-jellyfin-user">Usuario de Jellyfin</label>
        <input id="setup-jellyfin-user" name="jellyfin_user" value="{escape(config.get('jellyfin_user',''))}" placeholder="kilian">
        <div class="form-hint">Nombre de usuario para el estado de visto. Vacío usa el primer administrador.</div>
      </div>
      <div class="form-group">
        <label for="setup-jellyfin-series">Nombre de la serie en Jellyfin</label>
        <input id="setup-jellyfin-series" name="jellyfin_series" value="{escape(config.get('jellyfin_series', DEFAULT_CONFIG['jellyfin_series']))}">
        <div class="form-hint">Nombre exacto de la serie tal como aparece en Jellyfin.</div>
      </div>
      <div class="card-footer">
        <button class="btn btn-primary" type="submit">Guardar configuración</button>
      </div>
    </form>
  </div>
"""

    # Step 2 – Metadatos
    if step1_done:
        step2_num = '<span class="step-num done">✓</span>' if step2_done else '<span class="step-num">2</span>'
        sync_btn = f'<form method="post" action="/sync-metadata" style="display:inline">{csrf_input()}<button class="btn btn-primary" type="submit">Descargar metadatos</button></form>'
        if step2_done:
            sync_btn = f'<form method="post" action="/sync-metadata" style="display:inline">{csrf_input()}<button class="btn btn-ghost" type="submit">Actualizar metadatos</button></form>'
        html += f"""
  <div class="card">
    <h2>{step2_num} Metadatos de One Pace</h2>
    <div class="sync-info">
      Los metadatos incluyen carátulas de temporadas, pósters y fichas NFO de cada episodio para Jellyfin.
      Se descargan desde el repositorio GitHub de OPDES. Solo necesitas hacer esto una vez
      (o cuando quieras actualizarlos).
    </div>
    <div class="card-footer">
      {sync_btn}
      {"<span style='color:var(--success);font-size:.875rem'>✓ Metadatos listos</span>" if step2_done else ""}
    </div>
  </div>
"""
        if step2_done:
            html += """
  <div class="card-footer" style="margin-top:16px">
    <a class="btn btn-primary" href="/">Ir a la biblioteca</a>
  </div>
"""
    html += "</div>"
    return page(html, title="Configuración", active="setup")


@app.post("/setup")
def save_setup():
    config = cargar_config()
    try:
        new_config = dict(config)
        new_config["url"] = validate_remote_url(
            request.form.get("url", DEFAULT_CONFIG["url"]).strip() or DEFAULT_CONFIG["url"]
        )
        new_config["output_dir"] = validate_config_path(request.form.get("output_dir", ""), "Carpeta de episodios")
        new_config["metadata_dir"] = validate_config_path(request.form.get("metadata_dir", ""), "Carpeta de metadatos")
        quality = request.form.get("quality", "max").strip().lower() or "max"
        if quality not in {"max", "1080p", "720p", "480p"}:
            raise ValueError("Calidad no válida.")
        new_config["quality"] = quality
        new_config["jellyfin_url"] = validate_remote_url(request.form.get("jellyfin_url", ""), allow_private=True)
        jellyfin_token = request.form.get("jellyfin_token", "").strip()
        if jellyfin_token:
            new_config["jellyfin_token"] = jellyfin_token
        new_config["jellyfin_user"] = request.form.get("jellyfin_user", "").strip()
        new_config["jellyfin_series"] = (
            request.form.get("jellyfin_series", DEFAULT_CONFIG["jellyfin_series"]).strip()
            or DEFAULT_CONFIG["jellyfin_series"]
        )
        config = new_config
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("setup"))
    _jf_user_cache["id"] = None
    _jf_series_cache["id"] = None
    _jf_seasons_cache["data"] = None
    guardar_config(config)
    flash("Configuración guardada.")
    return redirect(url_for("setup"))


@app.post("/sync-metadata")
def sync_metadata_route():
    config = cargar_config()
    if not str(config.get("metadata_dir", "")).strip():
        flash("Define primero el directorio de metadatos.")
        return redirect(url_for("setup"))
    try:
        sync_metadata(config)
        flash("Metadatos sincronizados correctamente.")
    except Exception as e:
        flash(f"Error sincronizando metadatos: {e}")
    return redirect(url_for("setup"))


# ── Home / Catalog ─────────────────────────────────────────────────────────────

@app.before_request
def check_setup():
    ensure_auth_is_configured()
    if auth_required() and request.endpoint not in PUBLIC_ENDPOINTS and not is_authenticated():
        if wants_json_response():
            return jsonify({"ok": False, "error": "auth_required"}), 401
        return redirect(url_for("login", next=request.path))
    if request.method not in SAFE_METHODS and request.endpoint != "login_submit":
        validate_csrf()
    if request.endpoint in {"login", "login_submit", "setup", "save_setup", "sync_metadata_route", "img_show_poster",
                             "img_show_backdrop", "img_season_poster", "api_jobs", "api_jobs_clear",
                             "api_jobs_cancel", "api_job_cancel", "api_job_retry", "favicon", "static"}:
        return
    config = cargar_config()
    if not config_completa(config):
        if wants_json_response():
            return jsonify({"ok": False, "error": "setup_required"}), 428
        return redirect(url_for("setup"))
    if not metadatos_ok(config):
        if wants_json_response():
            return jsonify({"ok": False, "error": "metadata_sync_required"}), 428
        flash("Sincroniza los metadatos antes de continuar.")
        return redirect(url_for("setup"))


@app.get("/")
def home():
    config = cargar_config()
    try:
        catalogo = cargar_catalogo(config)
    except Exception as e:
        flash(f"Error cargando el catálogo: {e}")
        catalogo = []

    total_seasons = len(catalogo)
    total_dl = sum(a.get("descargados", 0) or 0 for a in catalogo)
    total_ep = sum(a.get("total_meta", 0) or 0 for a in catalogo)

    cards = []
    for arc in catalogo:
        n = arc["season_number"]
        title = arc.get("season_title") or arc["id"]
        dl = arc.get("descargados") or 0
        total = arc.get("total_meta") or 0
        pct = int(dl / total * 100) if total else 0
        if dl == 0:
            dot = "none"
        elif dl >= total:
            dot = "full"
        else:
            dot = "partial"
        prog_text = f"{dl}/{total}" if total else "—"
        cards.append(f"""
<a class="season-card" href="/season/{n}" data-season="{n}">
  <div class="poster-wrap">
    <div class="no-poster">OP</div>
    <img src="/img/season/{n}/poster" alt="" onerror="this.style.display='none'">
    <span class="status-dot {dot}"></span>
  </div>
  <div class="card-body">
    <div class="card-num">Temporada {n}</div>
    <div class="card-title">{escape(title)}</div>
    <div class="prog-wrap">
      <div class="prog-bar"><div class="fill" style="width:{pct}%"></div></div>
      <span class="prog-text">{prog_text}</span>
    </div>
  </div>
</a>""")

    cards_html = "\n".join(cards) if cards else '<p style="color:var(--muted)">No se pudo cargar el catálogo.</p>'

    content = f"""
<div class="page-header">
  <div>
    <h1>One Pace</h1>
    <div class="sub">{total_seasons} temporadas &nbsp;·&nbsp; {total_dl}/{total_ep} episodios descargados</div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <a class="btn btn-ghost btn-sm" href="/setup">Configuración</a>
    <form method="post" action="/sync-metadata" style="display:inline">{csrf_input()}<button class="btn btn-ghost btn-sm" type="submit">Sincronizar metadatos</button></form>
  </div>
</div>
<div class="season-grid">{cards_html}</div>
"""
    return page(content, title="Biblioteca", active="home")


# ── Season detail ──────────────────────────────────────────────────────────────

@app.get("/season/<int:n>")
def season_detail(n: int):
    config = cargar_config()
    try:
        detalle = cargar_detalle_temporada(n, config)
    except Exception as e:
        flash(f"Error cargando temporada: {e}")
        return redirect(url_for("home"))
    if not detalle:
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))

    arc = detalle["arc"]
    episodes = detalle["episodes"]
    season_meta = detalle["season_meta"]

    title = arc.get("season_title") or arc["id"]
    dl_count = sum(1 for e in episodes if e["downloaded"])
    total_ep = len(episodes)
    has_link = arc.get("elegida") is not None
    quality = arc["elegida"]["quality"] if has_link else "—"

    qualities_available = ", ".join(
        sorted({x["quality"] for x in arc.get("opciones", []) if x.get("quality") != "desconocida"}, key=ordenar_calidades)
    ) or "sin enlaces"

    jellyfin_url = str(config.get("jellyfin_url", "")).strip()
    jf_data = jellyfin_season_data(n, config) if jellyfin_url else {}

    # Season hero
    dl_all_btn = ""
    del_all_btn = ""
    if has_link:
        dl_all_btn = f'<form method="post" action="/download/season/{n}" style="display:inline">{csrf_input()}<button class="btn btn-primary" type="submit">⬇ Descargar todo</button></form>'
    if dl_count > 0:
        del_all_btn = f'<form method="post" action="/delete/season/{n}" style="display:inline" onsubmit="return confirm(\'¿Eliminar los {dl_count} episodios descargados?\')">{csrf_input()}<button class="btn btn-danger" type="submit">🗑 Eliminar todo</button></form>'

    job_id_season = f"season-{n}"
    jobs = jobs_snapshot()
    season_downloading = job_id_season in jobs and jobs[job_id_season]["status"] in {"queued", "running"}

    content = f"""
<a class="back-link" href="/">← Biblioteca</a>
<div class="season-hero">
  <div class="s-poster">
    <img src="/img/season/{n}/poster" alt="" onerror="this.style.display='none'">
  </div>
  <div class="s-info">
    <div class="s-num">Temporada {n}</div>
    <div class="s-title">{escape(title)}</div>
    <div class="s-stats">
      <div class="stat"><span class="stat-val">{total_ep}</span><span class="stat-lbl">Episodios totales</span></div>
      <div class="stat"><span class="stat-val" style="color:var(--success)">{dl_count}</span><span class="stat-lbl">Descargados</span></div>
      <div class="stat"><span class="stat-val">{escape(quality)}</span><span class="stat-lbl">Calidad elegida</span></div>
      <div class="stat"><span class="stat-val" style="font-size:.9rem">{escape(qualities_available)}</span><span class="stat-lbl">Disponible en</span></div>
    </div>
    <div class="season-actions">
      {"<span style='color:var(--accent);font-size:.875rem'>⏳ Descargando...</span>" if season_downloading else dl_all_btn}
      {del_all_btn}
    </div>
  </div>
</div>
<div class="ep-section-header">Episodios</div>
<form id="bulk-download" method="post" action="/download/season/{n}/episodes" style="display:none">
{csrf_input()}
</form>
<div class="card-footer" style="margin:0 0 12px 0">
  <button class="btn btn-primary btn-sm" type="submit" form="bulk-download">Descargar seleccionados</button>
</div>
<div class="ep-list">
{"<p style='color:var(--muted);font-size:.875rem;padding:16px 0'>No hay metadatos disponibles para esta temporada. Sincroniza los metadatos.</p>" if not episodes else ""}
"""

    for ep in episodes:
        en = ep["number"]
        et = ep["title"]
        ep_plot = ep["plot"][:160] + ("…" if len(ep["plot"]) > 160 else "") if ep["plot"] else ""
        ep_aired = ep["aired"] if ep["aired"] else ""
        downloaded = ep["downloaded"]
        available = ep["available"]

        ep_job_id = f"ep-{n}-{en}"
        ep_downloading = ep_job_id in jobs and jobs[ep_job_id]["status"] in {"queued", "running"}

        badge = '<span class="ep-badge ok">✓ Descargado</span>' if downloaded else '<span class="ep-badge pending">Sin descargar</span>'

        jf_ep = jf_data.get(en)
        watched_badge = ""
        if downloaded and jf_ep:
            if jf_ep["played"]:
                watched_badge = f'<form method="post" action="/jellyfin/watched/{n}/{en}" style="display:inline">{csrf_input()}<button class="ep-badge watched" type="submit" title="Marcar como no visto">✓ Visto</button></form>'
            else:
                watched_badge = f'<form method="post" action="/jellyfin/watched/{n}/{en}" style="display:inline">{csrf_input()}<button class="ep-badge unwatched" type="submit" title="Marcar como visto">○ No visto</button></form>'

        if ep_downloading:
            action = (
                f'<div class="ep-dl-wrap" data-job-id="{ep_job_id}">'
                f'<span class="ep-badge" style="background:rgba(246,162,0,.15);color:var(--accent)">⬇ Descargando</span>'
                f'<div class="ep-dl-progress">'
                f'<div class="ep-dl-bar"><div class="fill" style="width:0%"></div></div>'
                f'<span class="ep-dl-label">…</span>'
                f'</div>'
                f'</div>'
            )
        elif downloaded:
            play_btn = f'<a class="btn btn-primary btn-sm" href="/jellyfin/play/{n}/{en}" title="Ver en Jellyfin">▶</a>' if jellyfin_url else ""
            del_btn = f'<form method="post" action="/delete/season/{n}/episode/{en}" onsubmit="return confirm(\'¿Eliminar episodio {en}?\')">{csrf_input()}<button class="btn btn-danger btn-sm" type="submit">🗑</button></form>'
            action = f'<div style="display:flex;gap:6px;align-items:center">{play_btn}{del_btn}</div>'
        elif available:
            action = f'<form method="post" action="/download/season/{n}/episode/{en}">{csrf_input()}<button class="btn btn-primary btn-sm" type="submit">⬇</button></form>'
        else:
            action = '<span class="no-av">No disponible</span>'

        checkbox = (
            f'<input class="ep-check" form="bulk-download" type="checkbox" name="episodes" value="{en}" aria-label="Seleccionar episodio {en}">'
            if available and not downloaded and not ep_downloading else
            '<span style="width:18px;flex-shrink:0"></span>'
        )

        meta_parts = []
        if ep_aired:
            meta_parts.append(ep_aired)
        meta_str = " · ".join(meta_parts)

        content += f"""
  <div class="ep-card {"ep-downloaded" if downloaded else ""}">
    {checkbox}
    <div class="ep-num">E{en:02d}</div>
    <div class="ep-body">
      <div class="ep-title">{escape(et)}</div>
      {"<div class='ep-plot'>" + str(escape(ep_plot)) + "</div>" if ep_plot else ""}
      {"<div class='ep-meta'>" + str(escape(meta_str)) + "</div>" if meta_str else ""}
    </div>
    <div class="ep-actions">{badge}{watched_badge}{action}</div>
  </div>"""

    content += "\n</div>"
    return page(content, title=f"T{n}: {title}", active="home")


# ── Download / Delete actions ──────────────────────────────────────────────────

def _start_download_season(n: int, *, as_json: bool = False):
    config = cargar_config()
    arc = cargar_arc_por_numero(n, config)
    if not arc:
        if as_json:
            return jsonify({"ok": False, "error": "Temporada no encontrada."}), 404
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))
    job_id = f"season-{n}"
    jobs = jobs_snapshot()
    if job_id in jobs and jobs[job_id]["status"] in {"queued", "running"}:
        if as_json:
            return jsonify({"ok": True, "job_id": job_id, "already_running": True})
        flash("La temporada ya se está descargando.")
        return redirect(url_for("season_detail", n=n))
    job_create(job_id, f"T{n}: {arc.get('season_title', arc['id'])}", descargar_temporada_bg, (arc, config))
    enqueue_job(job_id)
    if as_json:
        return jsonify({"ok": True, "job_id": job_id})
    flash(f"Descarga de la temporada {n} iniciada en segundo plano.")
    return redirect(url_for("season_detail", n=n))


def _start_download_episode(n: int, e: int, *, as_json: bool = False):
    config = cargar_config()
    arc = cargar_arc_por_numero(n, config)
    if not arc:
        if as_json:
            return jsonify({"ok": False, "error": "Temporada no encontrada."}), 404
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))
    job_id = f"ep-{n}-{e}"
    jobs = jobs_snapshot()
    if job_id in jobs and jobs[job_id]["status"] in {"queued", "running"}:
        if as_json:
            return jsonify({"ok": True, "job_id": job_id, "already_running": True})
        flash("El episodio ya se está descargando.")
        return redirect(url_for("season_detail", n=n))
    title = arc.get("season_title", arc["id"])
    job_create(job_id, f"T{n} E{e:02d} - {title}", descargar_episodio_bg, (arc, e, config))
    enqueue_job(job_id)
    if as_json:
        return jsonify({"ok": True, "job_id": job_id})
    flash(f"Descarga del episodio {e} iniciada en segundo plano.")
    return redirect(url_for("season_detail", n=n))


def _start_download_episodes(n: int, episodes: list[int], *, as_json: bool = False):
    if not episodes:
        if as_json:
            return jsonify({"ok": False, "error": "Selecciona al menos un episodio."}), 400
        flash("Selecciona al menos un episodio.", "error")
        return redirect(url_for("season_detail", n=n))
    config = cargar_config()
    detail = cargar_detalle_temporada(n, config)
    if not detail:
        if as_json:
            return jsonify({"ok": False, "error": "Temporada no encontrada."}), 404
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))
    valid = {ep["number"] for ep in detail["episodes"] if ep["available"] and not ep["downloaded"]}
    selected = [ep for ep in episodes if ep in valid]
    if not selected:
        if as_json:
            return jsonify({"ok": False, "error": "No hay episodios seleccionados descargables."}), 400
        flash("No hay episodios seleccionados descargables.", "error")
        return redirect(url_for("season_detail", n=n))
    arc = detail["arc"]
    job_ids = []
    for episode in selected:
        job_id = f"ep-{n}-{episode}"
        jobs = jobs_snapshot()
        if job_id in jobs and jobs[job_id]["status"] in {"queued", "running"}:
            job_ids.append(job_id)
            continue
        title = arc.get("season_title", arc["id"])
        job_create(job_id, f"T{n} E{episode:02d} - {title}", descargar_episodio_bg, (arc, episode, config))
        enqueue_job(job_id)
        job_ids.append(job_id)
    if as_json:
        return jsonify({"ok": True, "job_ids": job_ids})
    flash(f"Iniciadas {len(job_ids)} descarga(s).")
    return redirect(url_for("season_detail", n=n))


def _delete_season(n: int, *, as_json: bool = False):
    config = cargar_config()
    output_dir = Path(validate_config_path(config["output_dir"], "Carpeta de episodios"))
    carpeta = output_dir / f"Season {n}"
    borrados = 0
    if carpeta.exists():
        for f in carpeta.iterdir():
            if f.suffix.lower() in VIDEO_EXTS:
                f.unlink()
                borrados += 1
                nfo = f.with_suffix(".nfo")
                if nfo.exists():
                    nfo.unlink()
    limpiar_temporales_si_ok(output_dir)
    if as_json:
        return jsonify({"ok": True, "deleted": borrados})
    flash(f"Eliminados {borrados} episodio(s) de la temporada {n}.")
    return redirect(url_for("season_detail", n=n))


def _delete_episode(n: int, e: int, *, as_json: bool = False):
    config = cargar_config()
    output_dir = Path(validate_config_path(config["output_dir"], "Carpeta de episodios"))
    f = encontrar_archivo_local(output_dir, n, e)
    if f:
        f.unlink()
        nfo = f.with_suffix(".nfo")
        if nfo.exists():
            nfo.unlink()
        if as_json:
            return jsonify({"ok": True, "deleted": 1})
        flash(f"Episodio {e} eliminado.")
    else:
        if as_json:
            return jsonify({"ok": True, "deleted": 0})
        flash(f"El episodio {e} no estaba descargado.")
    return redirect(url_for("season_detail", n=n))


@app.post("/download/season/<int:n>")
def download_season(n: int):
    return _start_download_season(n)


@app.post("/download/season/<int:n>/episode/<int:e>")
def download_episode(n: int, e: int):
    return _start_download_episode(n, e)


@app.post("/download/season/<int:n>/episodes")
def download_episodes(n: int):
    try:
        episodes = sorted({int(e) for e in request.form.getlist("episodes")})
    except ValueError:
        episodes = []
    return _start_download_episodes(n, episodes)


@app.post("/delete/season/<int:n>")
def delete_season(n: int):
    return _delete_season(n)


@app.post("/delete/season/<int:n>/episode/<int:e>")
def delete_episode(n: int, e: int):
    return _delete_episode(n, e)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("OPDES_DEV_HOST", "127.0.0.1"),
        port=int(os.environ.get("OPDES_DEV_PORT", "8080")),
        debug=os.environ.get("OPDES_DEBUG", "").lower() in {"1", "true", "yes"},
    )
