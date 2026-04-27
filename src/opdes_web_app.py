from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, flash, redirect, render_template_string, request, url_for
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError, RequestException, Timeout
from urllib3.util.retry import Retry

# =========================
# Configuración base
# =========================
CONFIG_DIR = Path.home() / ".opdes"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "url": "https://onepace.net/es/watch",
    "output_dir": "",
    "metadata_dir": "",
    "quality": "max",
    "log_level": "error",
}

HEADERS = {"User-Agent": "Mozilla/5.0"}
API_HOSTS = ["pixeldrain.net", "pixeldrain.com"]
CHUNK_SIZE = 1024 * 1024
MAX_REINTENTOS_DESCARGA = 8
MAX_REINTENTOS_JSON = 8
LOG_LEVELS = {"error": 0, "debug": 1}
METADATA_REPO_ZIP_URL = "https://github.com/4fr0-d3v/OPDES/archive/refs/heads/main.zip"
METADATA_SOURCE_SUFFIX = "one-pace-jellyfin-master/One Pace"

app = Flask(__name__)
app.secret_key = "opdes-local-dev"

# =========================
# Helpers de configuración
# =========================
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


def config_basica_valida(config: dict) -> tuple[bool, list[str]]:
    errores: list[str] = []

    output_dir = Path(str(config.get("output_dir", "")).strip()).expanduser() if str(config.get("output_dir", "")).strip() else None
    metadata_dir = Path(str(config.get("metadata_dir", "")).strip()).expanduser() if str(config.get("metadata_dir", "")).strip() else None

    if output_dir is None:
        errores.append("Falta configurar el directorio raíz de salida.")
    else:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            errores.append(f"No se puede usar output_dir: {e}")

    if metadata_dir is None:
        errores.append("Falta configurar el directorio de metadatos.")
    elif not metadata_dir.exists():
        errores.append("El directorio de metadatos no existe todavía. Sincroniza los metadatos.")
    else:
        seasons = list(metadata_dir.glob("Season *"))
        if not seasons:
            errores.append("El directorio de metadatos no contiene carpetas Season *.")

    return (len(errores) == 0, errores)


# =========================
# Logging
# =========================
def get_log_level(config: dict | None = None) -> str:
    if config is None:
        config = cargar_config()
    level = str(config.get("log_level", "error")).lower()
    return level if level in LOG_LEVELS else "error"


def should_log(level: str, config: dict | None = None) -> bool:
    current = get_log_level(config)
    return LOG_LEVELS.get(current, 0) >= LOG_LEVELS.get(level, 0)


def log_debug(msg: str, config: dict | None = None) -> None:
    if should_log("debug", config):
        print(f"[debug] {msg}")


def log_error(msg: str) -> None:
    print(f"[error] {msg}")


# =========================
# Red / Pixeldrain / One Pace
# =========================
def obtener_html(url: str) -> str:
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
                pixeldrain_links.append({
                    "texto": a.get_text(separator=" ", strip=True),
                    "url": href,
                })

        temporadas.append({"id": season_id, "pixeldrain": pixeldrain_links})

    return temporadas


def extraer_calidad_desde_texto(texto: str) -> str | None:
    m = re.search(r"(480p|720p|1080p)", texto, re.IGNORECASE)
    return m.group(1).lower() if m else None


def ordenar_calidades(calidad: str) -> int:
    orden = {"480p": 480, "720p": 720, "1080p": 1080}
    return orden.get(calidad.lower(), 0)


def agrupar_por_temporada(items: list[dict]) -> list[dict]:
    agrupado: dict[str, dict] = {}

    for item in items:
        arc_id = item.get("id", "sin_id")
        bucket = agrupado.setdefault(arc_id, {"id": arc_id, "opciones": []})

        for enlace in item.get("pixeldrain", []):
            calidad = extraer_calidad_desde_texto(enlace.get("texto", "")) or "desconocida"
            bucket["opciones"].append({
                "texto": enlace.get("texto", ""),
                "url": enlace.get("url", ""),
                "quality": calidad,
            })

    resultado = list(agrupado.values())
    for idx, arc in enumerate(resultado, start=1):
        arc["season_number"] = idx
    return resultado


def elegir_opcion_por_calidad(opciones: list[dict], quality_config: str) -> dict | None:
    if not opciones:
        return None

    opciones_validas = [x for x in opciones if x.get("quality") in {"480p", "720p", "1080p"}]
    if not opciones_validas:
        return opciones[0]

    opciones_ordenadas = sorted(opciones_validas, key=lambda x: ordenar_calidades(x["quality"]))
    if quality_config == "max":
        return opciones_ordenadas[-1]

    for op in opciones_ordenadas:
        if op["quality"] == quality_config:
            return op

    return opciones_ordenadas[-1]


def extraer_tipo_e_id(url: str):
    parsed = urlparse(url)
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
    host = urlparse(url).netloc.lower()
    if "pixeldrain.net" in host:
        return ["pixeldrain.net", "pixeldrain.com"]
    if "pixeldrain.com" in host:
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
                    url,
                    timeout=(10, 30),
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                        "Connection": "close",
                    },
                ) as resp:
                    if resp.status_code == 404:
                        raise RuntimeError(f"No encontrado en Pixeldrain: {url}")
                    if resp.status_code == 403:
                        try:
                            detalle = resp.json()
                        except Exception:
                            detalle = {"message": "403 Forbidden"}
                        raise RuntimeError(f"No se puede acceder a {url}: {detalle.get('message', '403 Forbidden')}")
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

    raise RuntimeError(f"Falló la petición JSON tras varios intentos: {ultimo_error}")


# =========================
# Metadatos
# =========================
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
            zf.extractall(extract_dir)

        source_dir = None
        for candidate in extract_dir.rglob("One Pace"):
            if candidate.as_posix().endswith(METADATA_SOURCE_SUFFIX):
                source_dir = candidate
                break

        if source_dir is None or not source_dir.exists():
            raise RuntimeError("No se encontró la carpeta de metadatos dentro del repositorio descargado.")

        copiar_contenido_directorio(source_dir, metadata_dir)


def leer_titulo_season_nfo(season_nfo: Path) -> str | None:
    try:
        root = ET.parse(season_nfo).getroot()
        title = root.findtext("title")
        return title.strip() if title else None
    except Exception:
        return None


def extraer_season_episode_de_nfo_name(nfo_name: str):
    m = re.search(r"S(\d+)E(\d+)", nfo_name, re.IGNORECASE)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def construir_indice_metadatos(metadata_root: Path):
    indice = {}
    if not metadata_root.exists():
        return indice

    for season_dir in sorted(metadata_root.glob("Season *")):
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
    patron = re.compile(r"^\[One Pace\]\[[^\]]+\]\s+(.+?)\s+(\d{2})\s+\[[^\]]+\]\[[^\]]+\]\[[0-9A-Fa-f]{8}\](\.[^.]+)$")
    m = patron.match(nombre_archivo)
    if not m:
        return None
    return {"arc_name": m.group(1).strip(), "episode_in_arc": int(m.group(2)), "ext": m.group(3)}


def obtener_ruta_final_esperada(nombre_archivo: str, destino_base: Path, indice_metadatos: dict | None, season_number: int | None) -> Path | None:
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


def archivo_ya_existe_en_destino_final(nombre_archivo: str, destino_base: Path, indice_metadatos: dict | None, season_number: int | None) -> Path | None:
    ruta_final = obtener_ruta_final_esperada(nombre_archivo, destino_base, indice_metadatos, season_number)
    if ruta_final and ruta_final.exists():
        return ruta_final
    return None


def asegurar_estructura_temporada(destino_base: Path, season_meta: dict) -> Path:
    season_number = season_meta["season_number"]
    carpeta_temporada = destino_base / f"Season {season_number}"
    carpeta_temporada.mkdir(parents=True, exist_ok=True)

    season_nfo_dest = carpeta_temporada / "season.nfo"
    if not season_nfo_dest.exists():
        shutil.copy2(season_meta["season_nfo"], season_nfo_dest)

    poster_candidates = [
        season_meta["season_dir"] / "poster.png",
        season_meta["season_dir"] / "folder.jpg",
        season_meta["season_dir"] / "folder.png",
        season_meta["season_dir"] / "season.jpg",
        season_meta["season_dir"] / "season.png",
    ]
    for poster_src in poster_candidates:
        if poster_src.exists():
            poster_dest = carpeta_temporada / poster_src.name
            if not poster_dest.exists():
                shutil.copy2(poster_src, poster_dest)
            break

    return carpeta_temporada


def renombrar_y_copiar_nfo_segun_metadata(video_path: Path, destino_base: Path, indice_metadatos: dict, season_number: int) -> Path:
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


# =========================
# Descarga y borrado
# =========================
def descargar_archivo_reanudable(file_id: str, nombre_archivo: str, carpeta_destino: Path, session: requests.Session, url_original: str):
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
                        raise RuntimeError(f"No se puede descargar {file_id}: 403 Forbidden")
                    if resp.status_code == 404:
                        raise RuntimeError(f"Archivo no encontrado: {file_id}")
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"HTTP inesperado {resp.status_code} al descargar {file_id}")

                    modo = "ab" if resp.status_code == 206 and descargado > 0 else "wb"
                    if modo == "wb" and temp.exists():
                        temp.unlink()
                        descargado = 0

                    total = None
                    content_length = resp.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        total_respuesta = int(content_length)
                        total = descargado + total_respuesta if resp.status_code == 206 else total_respuesta

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
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))

                temp.rename(destino)
                return destino
            except (ConnectionError, Timeout, ChunkedEncodingError, OSError) as e:
                ultimo_error = e
                if intento == MAX_REINTENTOS_DESCARGA:
                    break
                time.sleep(min(2 ** intento, 30))
            except RuntimeError:
                raise

    raise RuntimeError(f"Fallo descargando {file_id}: {ultimo_error}")


def procesar_url_pixeldrain(url: str, carpeta_base: Path, session: requests.Session, destino_base: Path, indice_metadatos: dict | None = None, season_number: int | None = None):
    tipo, item_id = extraer_tipo_e_id(url)
    descargados = []

    if tipo == "file":
        info = pedir_json_resistente(f"/file/{item_id}/info", url)
        nombre = info.get("name") or f"{item_id}.bin"
        ruta_final_existente = archivo_ya_existe_en_destino_final(nombre, destino_base, indice_metadatos, season_number)
        if ruta_final_existente:
            descargados.append(ruta_final_existente)
            return descargados
        ruta = descargar_archivo_reanudable(item_id, nombre, carpeta_base, session, url)
        descargados.append(ruta)

    elif tipo == "list":
        data = pedir_json_resistente(f"/list/{item_id}", url)
        archivos = data.get("files", [])
        for archivo in archivos:
            file_id = archivo.get("id")
            if not file_id:
                continue
            nombre = archivo.get("name") or f"{file_id}.bin"
            ruta_final_existente = archivo_ya_existe_en_destino_final(nombre, destino_base, indice_metadatos, season_number)
            if ruta_final_existente:
                descargados.append(ruta_final_existente)
                continue
            ruta = descargar_archivo_reanudable(file_id, nombre, carpeta_base, session, url)
            descargados.append(ruta)

    return descargados


def obtener_archivos_descargados_para_temporada(url: str, destino_base: Path, indice_metadatos: dict, season_number: int) -> list[Path]:
    archivos = obtener_archivos_lista_pixeldrain(url)
    resultado = []
    for archivo in archivos:
        nombre = archivo.get("name") or f"{archivo.get('id', 'sin_id')}.bin"
        ruta = obtener_ruta_final_esperada(nombre, destino_base, indice_metadatos, season_number)
        if ruta and ruta.exists():
            resultado.append(ruta)
    return resultado


def descargar_temporada(arc: dict, config: dict) -> tuple[bool, str]:
    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()
    opcion = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
    if not opcion:
        return False, "No hay enlaces disponibles para esta temporada."

    session = crear_sesion()
    tmp_dir = output_dir / "_tmp" / slugify(arc["id"])

    rutas = procesar_url_pixeldrain(
        url=opcion["url"],
        carpeta_base=tmp_dir,
        session=session,
        destino_base=output_dir,
        indice_metadatos=indice_metadatos,
        season_number=arc["season_number"],
    )

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
    return True, f"Descargados {len(rutas_finales)} archivo(s)."


def borrar_temporada(arc: dict, config: dict) -> tuple[bool, str]:
    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()
    opcion = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
    if not opcion:
        return False, "No hay enlaces disponibles para resolver los archivos a borrar."

    objetivos = obtener_archivos_descargados_para_temporada(opcion["url"], output_dir, indice_metadatos, arc["season_number"])
    borrados = 0
    for path in objetivos:
        try:
            path.unlink()
            borrados += 1
            nfo = path.with_suffix(".nfo")
            if nfo.exists():
                nfo.unlink()
        except Exception:
            pass

    limpiar_temporales_si_ok(output_dir)
    return True, f"Borrados {borrados} archivo(s)."


def contar_descargados_para_enlace(item_id: str, url: str, output_dir: Path, season_number: int, indice_metadatos: dict | None = None):
    try:
        archivos = obtener_archivos_lista_pixeldrain(url)
    except Exception:
        return None, None

    disponibles = 0
    descargados = 0

    for archivo in archivos:
        file_id = archivo.get("id")
        if not file_id:
            continue
        disponibles += 1

        nombre = archivo.get("name") or f"{file_id}.bin"
        encontrado = False
        if indice_metadatos:
            ruta = obtener_ruta_final_esperada(nombre, output_dir, indice_metadatos, season_number)
            if ruta and ruta.exists():
                encontrado = True
        if not encontrado:
            carpeta_item = output_dir / "_tmp" / slugify(item_id)
            if (carpeta_item / nombre).exists():
                encontrado = True
        if encontrado:
            descargados += 1

    return descargados, disponibles


def obtener_archivos_lista_pixeldrain(url: str) -> list[dict]:
    tipo, item_id = extraer_tipo_e_id(url)
    if tipo == "file":
        info = pedir_json_resistente(f"/file/{item_id}/info", url)
        return [info]
    data = pedir_json_resistente(f"/list/{item_id}", url)
    return data.get("files", [])


def limpiar_ds_store(base_dir: Path):
    for ds_store in base_dir.rglob(".DS_Store"):
        try:
            ds_store.unlink()
        except Exception:
            pass


def limpiar_temporales_si_ok(base_dir: Path):
    tmp_dir = base_dir / "_tmp"
    if tmp_dir.exists() and tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    limpiar_ds_store(base_dir)


# =========================
# Presentación web
# =========================
BASE_TEMPLATE = """
<!doctype html>
<html lang=\"es\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>OPDES Web</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; background: #0b1020; color: #eef2ff; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
    .card { background: #141b34; border: 1px solid #263154; border-radius: 16px; padding: 18px; margin-bottom: 16px; }
    input, select, button { border-radius: 10px; border: 1px solid #3a4a7a; padding: 10px 12px; }
    input, select { width: 100%; background: #0f1630; color: #eef2ff; }
    button { background: #4f7cff; color: white; cursor: pointer; }
    button.danger { background: #c43d57; }
    button.secondary { background: #29365f; }
    .grid { display: grid; gap: 12px; }
    .grid.two { grid-template-columns: 1fr 1fr; }
    .muted { color: #b8c0e0; }
    .flash { padding: 12px 14px; border-radius: 12px; margin-bottom: 12px; background: #243055; }
    .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    .badge { display:inline-block; padding: 4px 8px; border-radius: 999px; background:#243055; color:#d9e2ff; font-size: 12px; }
    a { color: #8bb2ff; text-decoration: none; }
    .topnav { display:flex; gap:12px; margin-bottom:20px; }
  </style>
</head>
<body>
<div class=\"wrap\">
  <div class=\"topnav\">
    <a href=\"{{ url_for('home') }}\">Inicio</a>
    <a href=\"{{ url_for('setup') }}\">Configuración</a>
    <a href=\"{{ url_for('sync_metadata_route') }}\">Sincronizar metadatos</a>
  </div>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}
      <div class=\"flash\">{{ message }}</div>
    {% endfor %}
  {% endfor %}
  {{ content|safe }}
</div>
</body>
</html>
"""


def render_page(content: str, **ctx):
    return render_template_string(BASE_TEMPLATE, content=content, **ctx)


def cargar_catalogo(config: dict) -> list[dict]:
    html = obtener_html(config["url"])
    temporadas = extraer_temporadas_y_pixeldrain(html)
    agrupadas = agrupar_por_temporada(temporadas)

    output_dir = Path(config["output_dir"]).expanduser()
    metadata_dir = Path(config["metadata_dir"]).expanduser()
    indice_metadatos = construir_indice_metadatos(metadata_dir)
    quality = config.get("quality", "max").lower()

    for arc in agrupadas:
        arc["elegida"] = elegir_opcion_por_calidad(arc.get("opciones", []), quality)
        if arc["elegida"]:
            descargados, total = contar_descargados_para_enlace(
                arc["id"],
                arc["elegida"]["url"],
                output_dir,
                arc["season_number"],
                indice_metadatos,
            )
        else:
            descargados, total = None, None
        arc["descargados"] = descargados
        arc["total"] = total
    return agrupadas


@app.before_request
def exigir_setup():
    if request.endpoint in {"setup", "save_setup", "static"}:
        return
    config = cargar_config()
    ok, _ = config_basica_valida(config)
    if not ok:
        return redirect(url_for("setup"))


@app.get("/")
def home():
    config = cargar_config()
    ok, errores = config_basica_valida(config)
    if not ok:
        return redirect(url_for("setup"))

    try:
        catalogo = cargar_catalogo(config)
    except Exception as e:
        flash(f"Error cargando catálogo: {e}")
        catalogo = []

    cards = []
    for arc in catalogo:
        disponibles = ", ".join(sorted({x['quality'] for x in arc['opciones'] if x.get('quality') != 'desconocida'}, key=ordenar_calidades)) or "sin enlaces"
        elegida = arc["elegida"]["quality"] if arc.get("elegida") else "ninguna"
        estado = "-/-" if arc["descargados"] is None else f"{arc['descargados']}/{arc['total']}"
        cards.append(f"""
        <div class='card'>
          <div class='row'><strong>{arc['season_number']}. {arc['id']}</strong>
            <span class='badge'>disponible: {disponibles}</span>
            <span class='badge'>elegida: {elegida}</span>
            <span class='badge'>{estado}</span>
          </div>
          <div class='row' style='margin-top:12px;'>
            <form method='post' action='{url_for('download_arc', season_number=arc['season_number'])}'>
              <button type='submit'>Descargar</button>
            </form>
            <form method='post' action='{url_for('delete_arc', season_number=arc['season_number'])}'>
              <button class='danger' type='submit'>Eliminar descargado</button>
            </form>
          </div>
        </div>
        """)

    content = f"""
    <div class='card'>
      <h2>OPDES Web</h2>
      <div class='muted'>Raíz de serie: {config['output_dir']}</div>
      <div class='muted'>Metadatos: {config['metadata_dir']}</div>
      <div class='muted'>URL: {config['url']}</div>
      <div class='muted'>Calidad: {config['quality']}</div>
    </div>
    {''.join(cards) if cards else '<div class="card">No se pudo cargar el catálogo.</div>'}
    """
    return render_page(content)


@app.get("/setup")
def setup():
    config = cargar_config()
    _, errores = config_basica_valida(config)
    errores_html = "".join([f"<li>{e}</li>" for e in errores])
    content = f"""
    <div class='card'>
      <h2>Configuración inicial</h2>
      <p class='muted'>Antes de usar la aplicación, configura al menos la raíz de salida y el directorio de metadatos.</p>
      {'<ul>' + errores_html + '</ul>' if errores else ''}
      <form method='post' action='{url_for('save_setup')}' class='grid'>
        <div>
          <label>URL One Pace</label>
          <input name='url' value='{config.get('url', '')}'>
        </div>
        <div>
          <label>Raíz de la serie</label>
          <input name='output_dir' value='{config.get('output_dir', '')}' placeholder='/media/Series/One Pace'>
        </div>
        <div>
          <label>Directorio de metadatos</label>
          <input name='metadata_dir' value='{config.get('metadata_dir', '')}' placeholder='/srv/opdes/metadatos'>
        </div>
        <div>
          <label>Calidad</label>
          <select name='quality'>
            <option value='max' {'selected' if config.get('quality') == 'max' else ''}>max</option>
            <option value='480p' {'selected' if config.get('quality') == '480p' else ''}>480p</option>
            <option value='720p' {'selected' if config.get('quality') == '720p' else ''}>720p</option>
            <option value='1080p' {'selected' if config.get('quality') == '1080p' else ''}>1080p</option>
          </select>
        </div>
        <div class='row'>
          <button type='submit'>Guardar configuración</button>
          <a href='{url_for('sync_metadata_route')}'>Sincronizar metadatos</a>
        </div>
      </form>
    </div>
    """
    return render_page(content)


@app.post("/setup")
def save_setup():
    config = cargar_config()
    config["url"] = request.form.get("url", DEFAULT_CONFIG["url"]).strip() or DEFAULT_CONFIG["url"]
    config["output_dir"] = request.form.get("output_dir", "").strip()
    config["metadata_dir"] = request.form.get("metadata_dir", "").strip()
    config["quality"] = request.form.get("quality", "max").strip().lower() or "max"
    guardar_config(config)
    flash("Configuración guardada.")
    return redirect(url_for("setup"))


@app.get("/sync-metadata")
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
    return redirect(url_for("home"))


@app.post("/download/<int:season_number>")
def download_arc(season_number: int):
    config = cargar_config()
    catalogo = cargar_catalogo(config)
    arc = next((x for x in catalogo if x["season_number"] == season_number), None)
    if not arc:
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))

    try:
        ok, msg = descargar_temporada(arc, config)
        flash(msg)
    except Exception as e:
        flash(f"Error descargando: {e}")
    return redirect(url_for("home"))


@app.post("/delete/<int:season_number>")
def delete_arc(season_number: int):
    config = cargar_config()
    catalogo = cargar_catalogo(config)
    arc = next((x for x in catalogo if x["season_number"] == season_number), None)
    if not arc:
        flash("Temporada no encontrada.")
        return redirect(url_for("home"))

    try:
        ok, msg = borrar_temporada(arc, config)
        flash(msg)
    except Exception as e:
        flash(f"Error borrando: {e}")
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
