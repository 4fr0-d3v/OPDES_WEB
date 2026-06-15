# OPDES_WEB: Correcciones y Mejoras — Plan de Implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Arreglar el bloqueo total observado en `192.168.1.112:8081`, eliminar bugs reales detectados en el código y endurecer el despliegue, sin romper compatibilidad con la config persistida.

**Architecture:** Fix de despliegue (operador) + correcciones quirúrgicas en `src/opdes_web_app.py` mantenidas pequeñas (TDD por cada fix) + nueva sección de hardening. Todas las modificaciones de código respetan el estilo monolítico actual (HTML/JS inline, español, single-file).

**Tech Stack:** Flask 3, gunicorn (1 worker + 4 threads), requests, BeautifulSoup, pytest, Docker (python:3.12-slim), NFS, Jellyfin API.

---

## Pre-flight: Acciones de operador (no son código)

Estas dos acciones deben ejecutarse **antes** de aplicar los fixes de código para que la app vuelva a responder.

### Op-1: Restaurar acceso al NFS dentro del contenedor

Síntoma observado: `POST /sync-metadata` flashea `[Errno 13] Permission denied: '/mnt/nfs/data'` y por tanto `metadatos_ok()` siempre devuelve False, lo cual hace que toda la app redirija a `/setup`.

```bash
# 1. SSH al LXC 101 (consola por Proxmox si no hay SSH)
pct enter 101                        # desde el host Proxmox

# 2. Verificar mount NFS en el LXC
mount | grep nfs
ls -la /mnt/nfs/data || echo "NFS roto en el LXC"

# 3. Identificar el contenedor docker de opdes
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Ports}}' | grep -i opdes

# 4. Inspeccionar sus mounts y user
docker inspect <container_id> --format '{{json .Mounts}}' | jq
docker inspect <container_id> --format '{{.Config.User}}'

# 5a. Si el contenedor monta /media y /metadata (ejemplo del repo), editar la config:
#     dentro del contenedor:
docker exec -it <container_id> sh -c 'cat /root/.opdes/config.json'
#     cambiar output_dir a "/media" y metadata_dir a "/metadata" via la UI /setup
#     o editando el JSON directamente y reiniciando el contenedor.

# 5b. Si el contenedor pretende montar /mnt/nfs/data directamente, añadir el bind mount
#     al compose y reiniciar:
#     volumes:
#       - /mnt/nfs/data:/mnt/nfs/data
```

**Criterio de éxito:** `curl http://192.168.1.112:8081/api/catalog` devuelve `{"ok":true,"catalog":[...]}` en vez de 428.

### Op-2: Habilitar auth admin (riesgo de seguridad confirmado)

Síntoma: `GET /login` redirige a `/`, lo cual significa `auth_required() == False`. Cualquiera en la LAN puede modificar la config y borrar episodios.

```bash
# Generar hash de contraseña
docker run --rm python:3.12-slim python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('TU_PASSWORD'))"

# Editar docker-compose.yml del deployment:
#   environment:
#     OPDES_ENV: production
#     OPDES_SECRET_KEY: "<64 chars aleatorios>"
#     OPDES_ADMIN_USER: "admin"
#     OPDES_ADMIN_PASSWORD_HASH: "<hash generado>"

docker compose up -d
```

**Criterio de éxito:** `curl -i http://192.168.1.112:8081/` devuelve 302 → `/login` (no a `/setup`).

---

## Phase A: Bugs críticos de código

### Task 1: Limitar `limpiar_temporales_si_ok` al tmp del job actual

**Files:**
- Modify: `src/opdes_web_app.py:805-813` (definición de `limpiar_temporales_si_ok`)
- Modify: `src/opdes_web_app.py:977,980,983,1035,1038,1041,2346` (callers)
- Test: `tests/test_security.py` (añadir test de race)

**Contexto:** `limpiar_temporales_si_ok(output_dir)` borra `output_dir/_tmp` entero (línea 806). Cada job descarga en `output_dir/_tmp/<slug>/`. Con dos jobs concurrentes, el primero que termina barre los `.part` del segundo → fallo durante descarga.

- [ ] **Step 1: Test que reproduce el bug**

Añadir al final de `tests/test_security.py`:
```python
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
```

- [ ] **Step 2: Verificar que el test falla**

```bash
cd /home/lian/codex/onepace/OPDES_WEB && . .venv/bin/activate && PYTHONPATH=. pytest tests/test_security.py::test_limpiar_temporales_no_borra_descargas_concurrentes -v
```
Expected: FAIL — `limpiar_temporales_si_ok()` no acepta `slug`.

- [ ] **Step 3: Modificar `limpiar_temporales_si_ok`**

Reemplazar líneas 805-813:
```python
def limpiar_temporales_si_ok(base_dir: Path, slug: str | None = None) -> None:
    if slug:
        tmp_dir = base_dir / "_tmp" / slug
        if tmp_dir.exists() and tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return
    tmp_dir = base_dir / "_tmp"
    if tmp_dir.exists() and tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

Nota: se elimina el `rglob(".DS_Store")` — bloqueaba el worker en NFS (ver Task 2).

- [ ] **Step 4: Actualizar callers en `descargar_temporada_bg` y `descargar_episodio_bg`**

En las 6 llamadas dentro de los `bg`, pasar el slug del arc:
- Líneas 977, 980, 983 (dentro de `descargar_temporada_bg`):
  ```python
  limpiar_temporales_si_ok(output_dir, slug=slugify(arc["id"]))
  ```
- Líneas 1035, 1038, 1041 (dentro de `descargar_episodio_bg`): igual.
- Línea 2346 (`_delete_season`): dejar la llamada sin `slug` (es cleanup global tras borrar episodios — comportamiento correcto).

- [ ] **Step 5: Verificar que el test pasa**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_limpiar_temporales_no_borra_descargas_concurrentes -v
```
Expected: PASS.

- [ ] **Step 6: Verificar que la suite completa sigue verde**

```bash
PYTHONPATH=. pytest -q
```
Expected: todos los tests pasan.

- [ ] **Step 7: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "fix: limpiar_temporales_si_ok solo borra tmp del job actual

Antes borraba output_dir/_tmp entero, lo cual eliminaba archivos .part
de descargas concurrentes y las hacia fallar con FileNotFoundError."
```

---

### Task 2: Eliminar `rglob(".DS_Store")` que bloquea worker en NFS

**Files:**
- Modify: `src/opdes_web_app.py:805-813` (ya hecho parcialmente en Task 1)
- Test: `tests/test_security.py`

**Contexto:** El antiguo `limpiar_temporales_si_ok` hacía `base_dir.rglob(".DS_Store")` tras cada descarga. En NFS con 30+ temporadas escanea miles de archivos y bloquea el worker varios minutos. Útil únicamente para macOS sembrando basura, no aplicable al deployment Linux.

Task 1 ya removió la llamada al `rglob`. Esta tarea añade el test guardrail.

- [ ] **Step 1: Test que confirma que NO se hace rglob**

```python
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
```

(Importar `Path` arriba en el test si no está ya.)

- [ ] **Step 2: Ejecutar y verificar PASS** (porque Task 1 ya eliminó el rglob)

```bash
PYTHONPATH=. pytest tests/test_security.py::test_limpiar_temporales_no_escanea_arbol_completo -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_security.py
git commit -m "test: guardar que limpiar_temporales no recorre output_dir entero"
```

---

### Task 3: Forzar liberación de jobs estancados

**Files:**
- Modify: `src/opdes_web_app.py:200-218` (`job_cancel`)
- Modify: `src/opdes_web_app.py:1749-1761` (handler `api_job_cancel`)
- Test: `tests/test_security.py`

**Contexto:** Si un worker queda colgado en `requests.get` (NFS lento, Pixeldrain timeout encadenado), el job sigue en `status="running"` para siempre. `_start_download_*` bloquea reintentos del mismo `season-N`/`ep-N-E`. El usuario tiene que reiniciar el contenedor. Solución: aceptar `?force=1` en cancel para marcar el job como cancelled aunque el thread esté colgado.

- [ ] **Step 1: Test que reproduce el caso zombie**

```python
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
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_job_cancel_force_libera_jobs_colgados -v
```
Expected: FAIL — el job sigue en `running` porque hoy `job_cancel` solo set-ea el event.

- [ ] **Step 3: Añadir parámetro `force` a `job_cancel`**

Reemplazar líneas 200-218:
```python
def job_cancel(job_id: str, force: bool = False) -> bool:
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
            if force:
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                job["updated_at"] = job["finished_at"]
                job["msg"] = "Cancelado a la fuerza (worker podría seguir activo)."
            return True
        return False
```

- [ ] **Step 4: Aceptar `?force=1` en el endpoint**

Reemplazar líneas 1757-1761:
```python
@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id: str):
    force = request.args.get("force", "").lower() in {"1", "true", "yes"}
    if not job_cancel(job_id, force=force):
        return jsonify({"ok": False, "error": "Job no encontrado o no cancelable."}), 404
    return jsonify({"ok": True, "job_id": job_id, "forced": force})
```

- [ ] **Step 5: Verificar que el test pasa y los anteriores siguen verdes**

```bash
PYTHONPATH=. pytest -q
```
Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "feat: cancel forzado para jobs zombie

Permite recuperar slots de season-N / ep-N-E cuando el worker
queda colgado, sin reiniciar el contenedor."
```

---

### Task 4: Restaurar título dinámico del HTML

**Files:**
- Modify: `src/opdes_web_app.py:1387-1418` (template `LAYOUT`)
- Test: `tests/test_security.py`

**Contexto:** `render(LAYOUT, ..., page_title=title)` pasa la variable, pero `LAYOUT` solo declara `{% block title %}ONE PACE DES{% endblock %}` sin uso de `page_title`. El title del browser siempre dice "ONE PACE DES".

- [ ] **Step 1: Test**

```python
def test_setup_page_title_es_dinamico(isolated_app):
    with isolated_app.test_client() as client:
        html = client.get("/setup").get_data(as_text=True)
    assert "<title>Configuración" in html or "<title>Configuracion" in html
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_setup_page_title_es_dinamico -v
```
Expected: FAIL — el title es estático.

- [ ] **Step 3: Fix del LAYOUT**

En `src/opdes_web_app.py:1392`, reemplazar:
```html
<title>{% block title %}ONE PACE DES{% endblock %}</title>
```
por:
```html
<title>{{ page_title }}</title>
```

Y en `page()` (línea 1421-1422), asegurar el default:
```python
def page(content: str, title: str = "ONE PACE DES", active: str = "") -> str:
    return render(LAYOUT, content=content, active=active, page_title=title)
```
(Ya está así — no requiere cambio.)

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "fix: HTML title dinamico por pagina"
```

---

## Phase B: Robustez y validación

### Task 5: Mostrar estado de los paths configurados en `/setup`

**Files:**
- Modify: `src/opdes_web_app.py:1849-1963` (handler `setup`)
- Test: `tests/test_security.py`

**Contexto:** `config_completa()` solo chequea que las strings no estén vacías. El wizard marca paso 1 "✓" aunque el path no exista. El usuario solo descubre el problema al hacer "Descargar metadatos" y recibir flash de error.

- [ ] **Step 1: Test**

```python
def test_setup_indica_path_inaccesible(isolated_app):
    webapp.guardar_config({
        **webapp.DEFAULT_CONFIG,
        "output_dir": "/no/existe/output",
        "metadata_dir": "/no/existe/meta",
    })
    with isolated_app.test_client() as client:
        html = client.get("/setup").get_data(as_text=True)
    assert "no es accesible" in html.lower()
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_setup_indica_path_inaccesible -v
```
Expected: FAIL.

- [ ] **Step 3: Añadir helper y mensaje en el form**

Justo después de `def metadatos_ok(config: dict) -> bool:` (línea 281), añadir:
```python
def path_status(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    try:
        p = Path(value).expanduser()
        if not p.exists():
            return "Path no es accesible desde el contenedor."
        if not p.is_dir():
            return "Path no es un directorio."
    except OSError as exc:
        return f"Path no es accesible desde el contenedor: {exc}"
    return ""
```

En el handler `setup()` (cerca de la línea 1869), añadir antes de los inputs:
```python
output_status = path_status(config.get("output_dir", ""))
metadata_status = path_status(config.get("metadata_dir", ""))
```

Y al lado de cada input (líneas 1893 y 1898), añadir un span con la advertencia:
```python
# Reemplazar el form-hint actual de output_dir por:
f'<div class="form-hint">Aquí se guardarán los vídeos descargados.</div>'
f'{"<div class=\"form-hint\" style=\"color:var(--danger)\">⚠ " + output_status + "</div>" if output_status else ""}'
```
(Lo mismo para `metadata_dir` con `metadata_status`.)

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "feat: setup muestra si los paths configurados son accesibles"
```

---

### Task 6: Verificar status_code en toggle "visto" de Jellyfin

**Files:**
- Modify: `src/opdes_web_app.py:1641-1673` (`_toggle_jellyfin_watched`)
- Test: `tests/test_security.py`

**Contexto:** `_toggle_jellyfin_watched` manda `requests.post`/`requests.delete` sin verificar `resp.status_code`. Si Jellyfin responde 401/403/500, devuelve `{ok: True, played: not played}` mintiendo. La UI marca como visto/no-visto algo que en Jellyfin no cambió.

- [ ] **Step 1: Test**

```python
def test_toggle_watched_falla_si_jellyfin_devuelve_error(isolated_app, monkeypatch):
    monkeypatch.setattr(webapp, "jellyfin_get_user_id", lambda cfg: "u1")
    monkeypatch.setattr(webapp, "jellyfin_season_data",
                        lambda s, cfg: {1: {"id": "i1", "played": False}})

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
```

(Importar `import requests` al inicio del test si no está.)

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_toggle_watched_falla_si_jellyfin_devuelve_error -v
```
Expected: FAIL.

- [ ] **Step 3: Patch en `_toggle_jellyfin_watched`**

Reemplazar el bloque `try/except` (líneas 1662-1670):
```python
    try:
        if played:
            r = requests.delete(f"{jellyfin_url}/Users/{user_id}/PlayedItems/{item_id}",
                                headers=headers, timeout=5)
        else:
            r = requests.post(f"{jellyfin_url}/Users/{user_id}/PlayedItems/{item_id}",
                              headers=headers, timeout=5)
        r.raise_for_status()
    except Exception as exc:
        msg = f"Error al actualizar el estado en Jellyfin: {exc}"
        if as_json:
            return jsonify({"ok": False, "error": msg}), 502
        flash(msg, "error")
        return redirect(url_for("season_detail", n=season))
```

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "fix: toggle watched no oculta errores de Jellyfin"
```

---

### Task 7: Retries minimos en helpers Jellyfin

**Files:**
- Modify: `src/opdes_web_app.py:1500-1633` (helpers `jellyfin_*`)
- Test: `tests/test_security.py`

**Contexto:** `jellyfin_get_user_id`, `jellyfin_get_series_id`, `jellyfin_get_season_id`, `jellyfin_season_data` capturan `Exception` y devuelven `None`/`{}` silenciosamente. Un blip transitorio convierte al icono ▶ en inutilizable hasta que el TTL de cache (5min/1h) expira. Añadir 2 retries con backoff corto.

- [ ] **Step 1: Test**

```python
def test_jellyfin_get_user_id_reintenta_en_fallo_transitorio(monkeypatch):
    calls = {"n": 0}

    class GoodResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"Id": "u1", "Name": "kilian",
                                  "Policy": {"IsAdministrator": True}}]

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.ConnectionError("blip")
        return GoodResp()

    monkeypatch.setattr(webapp.requests, "get", flaky)
    webapp._jf_user_cache["id"] = None
    uid = webapp.jellyfin_get_user_id({
        "jellyfin_url": "http://x", "jellyfin_token": "t", "jellyfin_user": "kilian"
    })
    assert uid == "u1"
    assert calls["n"] == 2
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_jellyfin_get_user_id_reintenta_en_fallo_transitorio -v
```
Expected: FAIL — la primera excepción retorna None.

- [ ] **Step 3: Añadir helper de retry**

Justo encima de `_jf_user_cache` (línea 1495), añadir:
```python
def _jf_request_json(method: str, url: str, *, headers: dict, params: dict | None = None,
                     timeout: int = 5, retries: int = 2) -> dict | list | None:
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=headers, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    return None
```

Reemplazar las llamadas `requests.get(...).json()` dentro de `jellyfin_get_user_id`, `jellyfin_get_series_id`, `jellyfin_get_season_id`, `jellyfin_season_data` por `_jf_request_json("GET", url, headers=..., params=...)`. Cuando devuelva None se mantiene el comportamiento actual (retorna None / {}).

Ejemplo `jellyfin_get_user_id`:
```python
def jellyfin_get_user_id(config: dict) -> str | None:
    now = time.time()
    if _jf_user_cache["id"] and now - _jf_user_cache["ts"] < 300:
        return _jf_user_cache["id"]
    url = str(config.get("jellyfin_url", "")).rstrip("/")
    token = str(config.get("jellyfin_token", "")).strip()
    username = str(config.get("jellyfin_user", "")).strip().lower()
    if not url or not token:
        return None
    users = _jf_request_json("GET", f"{url}/Users", headers=_jf_headers(token))
    if users is None:
        return None
    user_id = None
    if username:
        for u in users:
            if u.get("Name", "").lower() == username:
                user_id = u["Id"]; break
    if not user_id:
        for u in users:
            if u.get("Policy", {}).get("IsAdministrator", False):
                user_id = u["Id"]; break
    if not user_id and users:
        user_id = users[0]["Id"]
    _jf_user_cache["id"] = user_id
    _jf_user_cache["ts"] = now
    return user_id
```

Aplicar el mismo refactor (extraer `requests.get` → `_jf_request_json`) a las otras tres funciones.

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "feat: retries y centralizacion de llamadas Jellyfin"
```

---

### Task 8: Endpoint para invalidar cachés manualmente

**Files:**
- Modify: `src/opdes_web_app.py:1736-1761` (zona de API jobs)
- Test: `tests/test_security.py`

**Contexto:** `_catalog_cache`, `_pixeldrain_episode_cache`, `_jf_*_cache` tienen TTL fijo (5min–1h) y sólo se invalidan parcialmente en `save_setup`. Si onepace.net o Jellyfin cambian, hay que esperar o reiniciar.

- [ ] **Step 1: Test**

```python
def test_api_cache_clear_resetea_caches(isolated_app):
    webapp._catalog_cache = {"data": ["x"], "ts": 9999999999.0}
    webapp._pixeldrain_episode_cache["http://x"] = {"episodes": [1], "ts": 9999999999.0}
    webapp._jf_user_cache["id"] = "cache"; webapp._jf_user_cache["ts"] = 9999999999.0

    with isolated_app.test_client() as client:
        token = csrf_from(client.get("/setup").get_data(as_text=True))
        resp = client.post("/api/cache/clear", headers={"X-CSRF-Token": token})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert webapp._catalog_cache["data"] is None
    assert webapp._pixeldrain_episode_cache == {}
    assert webapp._jf_user_cache["id"] is None
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_api_cache_clear_resetea_caches -v
```
Expected: FAIL — 404.

- [ ] **Step 3: Añadir endpoint y exencion en `check_setup`**

Después de `api_job_retry` (línea 1768) añadir:
```python
@app.post("/api/cache/clear")
def api_cache_clear():
    global _catalog_cache
    _catalog_cache = {"data": None, "ts": 0.0}
    _pixeldrain_episode_cache.clear()
    _jf_user_cache["id"] = None; _jf_user_cache["ts"] = 0.0
    _jf_series_cache["id"] = None; _jf_series_cache["ts"] = 0.0
    _jf_seasons_cache["data"] = None; _jf_seasons_cache["ts"] = 0.0
    return jsonify({"ok": True})
```

Añadir `"api_cache_clear"` al set de exenciones de `check_setup` (línea 2026-2028).

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "feat: POST /api/cache/clear invalida caches en caliente"
```

---

### Task 9: Verificar número de episodio en Pixeldrain single-file

**Files:**
- Modify: `src/opdes_web_app.py:998-1019` (`descargar_episodio_bg`, rama type="file")
- Test: `tests/test_security.py`

**Contexto:** Cuando un arc apunta a `pixeldrain.net/u/<id>` (single file), el código asume que ese file es el episodio pedido sin verificar. Si el filename contiene `episode_in_arc=5` pero el usuario pidió `episode_number=3`, se descarga el incorrecto y se renombra al S{n}E{05} en disco.

- [ ] **Step 1: Test**

```python
def test_descargar_episodio_bg_single_file_rechaza_episodio_distinto(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "extraer_tipo_e_id", lambda u: ("file", "abc"))
    monkeypatch.setattr(webapp, "pedir_json_resistente",
        lambda path, url: {"name": "[One Pace][1080p] Arc 05 [crc][quality][12345678].mkv"})
    monkeypatch.setattr(webapp, "archivo_ya_existe_en_destino_final", lambda *a, **k: None)
    monkeypatch.setattr(webapp, "construir_indice_metadatos", lambda md: {})

    webapp.job_create("ep-1-3", "ep", lambda jid: None, ())
    webapp.descargar_episodio_bg(
        "ep-1-3",
        {"id": "arc", "season_number": 1, "opciones": [{"url": "https://pixeldrain.net/u/abc", "quality": "1080p"}]},
        3,
        {"output_dir": str(tmp_path / "out"), "metadata_dir": str(tmp_path / "meta"),
         "quality": "max"},
    )
    assert webapp._jobs["ep-1-3"]["status"] == "error"
    assert "no coincide" in webapp._jobs["ep-1-3"]["msg"].lower()
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_descargar_episodio_bg_single_file_rechaza_episodio_distinto -v
```
Expected: FAIL — descarga sin verificar.

- [ ] **Step 3: Validar número en la rama single-file**

En `descargar_episodio_bg`, después de obtener `pd_nombre` para `tipo == "file"` (línea 1002):
```python
        if tipo == "file":
            info = pedir_json_resistente(f"/file/{item_id}/info", url)
            file_id = item_id
            pd_nombre = info.get("name") or f"{item_id}.bin"
            parsed = parsear_nombre_descargado(pd_nombre)
            if parsed and parsed["episode_in_arc"] != episode_number:
                job_update(job_id, status="error",
                           msg=f"El enlace single-file no coincide con el episodio {episode_number}.")
                return
```

- [ ] **Step 4: Verificar PASS y suite completa**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "fix: rechazar descarga single-file con episodio incorrecto"
```

---

## Phase C: Hardening y deuda menor

### Task 10: Detectar 2160p / 4K en `extraer_calidad_desde_texto`

**Files:**
- Modify: `src/opdes_web_app.py:468-475`
- Modify: `src/opdes_web_app.py:473-475` (`ordenar_calidades`)
- Modify: `src/opdes_web_app.py:494, 501, 1978` (sets de calidades válidas)
- Test: `tests/test_security.py`

- [ ] **Step 1: Test**

```python
def test_extraer_calidad_detecta_2160p_y_4k():
    assert webapp.extraer_calidad_desde_texto("One Pace 2160p HEVC") == "2160p"
    assert webapp.extraer_calidad_desde_texto("Arco 4K") == "2160p"
    assert webapp.ordenar_calidades("2160p") > webapp.ordenar_calidades("1080p")
```

- [ ] **Step 2: Verificar fallo**

```bash
PYTHONPATH=. pytest tests/test_security.py::test_extraer_calidad_detecta_2160p_y_4k -v
```
Expected: FAIL.

- [ ] **Step 3: Ampliar regex y mapa**

```python
def extraer_calidad_desde_texto(texto: str) -> str | None:
    m = re.search(r"(2160p|1080p|720p|480p)", texto, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    if re.search(r"\b4k\b", texto, re.IGNORECASE):
        return "2160p"
    return None

def ordenar_calidades(calidad: str) -> int:
    return {"480p": 480, "720p": 720, "1080p": 1080, "2160p": 2160}.get(calidad.lower(), 0)
```

Sustituir `{"480p", "720p", "1080p"}` por `{"480p", "720p", "1080p", "2160p"}` en `elegir_opcion_por_calidad` (líneas 494, 501) y en `save_setup` (línea 1978). Añadir `"2160p"` al `<select>` (línea 1881).

- [ ] **Step 4: Verificar PASS**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/opdes_web_app.py tests/test_security.py
git commit -m "feat: soportar calidad 2160p/4K"
```

---

### Task 11: Warning de arranque cuando auth está deshabilitada

**Files:**
- Modify: `src/opdes_web_app.py:65-71` (después de `app.config.update(...)`)
- Test: manual (logs)

**Contexto:** Hoy si `OPDES_ENV` no es `production` y no hay admin, la app arranca silenciosamente sin auth. El deploy de Proxmox cayó en este caso. Añadir warning visible.

- [ ] **Step 1: Añadir warning explícito al arranque**

En `src/opdes_web_app.py`, justo después de `app.config.update({...})` (línea 70):
```python
if not admin_auth_configured() and os.environ.get("OPDES_ENV", "development").lower() != "production":
    import sys
    print(
        "WARNING: OPDES corre sin autenticacion admin. "
        "Define OPDES_ENV=production y OPDES_ADMIN_TOKEN o OPDES_ADMIN_USER/OPDES_ADMIN_PASSWORD_HASH "
        "antes de exponerlo en red.",
        file=sys.stderr, flush=True,
    )
```

Nota: este código depende de `admin_auth_configured()` definida más abajo. Reordenar — mover la definición de `admin_auth_configured` y `env_list` (líneas 290-301) **antes** del `app.config.update`, o convertir el warning en un `@app.before_first_request`. Optar por mover las helpers arriba (es la opción más simple y no rompe el orden de imports).

- [ ] **Step 2: Verificar manualmente**

```bash
unset OPDES_ENV OPDES_ADMIN_TOKEN OPDES_ADMIN_USER OPDES_ADMIN_PASSWORD_HASH
PYTHONPATH=. python -c "import src.opdes_web_app" 2>&1 | grep WARNING
```
Expected: línea WARNING en stderr.

- [ ] **Step 3: Verificar PASS de la suite**

```bash
PYTHONPATH=. pytest -q
```

- [ ] **Step 4: Commit**

```bash
git add src/opdes_web_app.py
git commit -m "chore: warning explicito si OPDES arranca sin auth admin"
```

---

### Task 12: Documentar troubleshooting en README

**Files:**
- Modify: `README.md`

**Contexto:** Los problemas vistos hoy (NFS denied, auth ausente, "metadata_sync_required" loop, jobs zombie) son fáciles de evitar si el operador sabe dónde mirar. Añadir sección.

- [ ] **Step 1: Añadir sección al final del README**

Pegar al final de `README.md`:
```markdown
## Troubleshooting

### Todo redirige a /setup o /api responde 428

Síntoma: cualquier endpoint funcional devuelve `metadata_sync_required` o
redirige al wizard.

Causa habitual: el contenedor no puede leer `metadata_dir`. Revisa
`POST /sync-metadata` desde la UI — el flash mostrará el `errno` exacto
(p.ej. `Permission denied: '/mnt/nfs/data'`). Confirma que el bind mount
NFS llega al contenedor y que el path en `~/.opdes/config.json` coincide
con la ruta dentro del contenedor (no la del host).

### Un job se queda "running" para siempre

Si un worker queda colgado (timeout NFS, Pixeldrain caído):
```bash
curl -X POST -H "X-CSRF-Token: $T" \
  "http://HOST/api/jobs/<job-id>/cancel?force=1"
```
El estado pasa a `cancelled` y permite re-encolar. El thread original
puede seguir vivo hasta que termine su request en curso.

### Refrescar catálogo/Jellyfin antes del TTL

```bash
curl -X POST -H "X-CSRF-Token: $T" "http://HOST/api/cache/clear"
```

### La app expone /setup sin pedir login

Significa que `OPDES_ENV` no es `production` y no hay admin configurado.
Revisa `docker logs <container>` — al arranque debería verse el
`WARNING: OPDES corre sin autenticacion admin.`
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: troubleshooting para metadata 428, jobs zombie y auth"
```

---

## Verificación end-to-end

Tras ejecutar todas las tareas:

- [ ] **Suite verde:**
  ```bash
  PYTHONPATH=. pytest -q
  ```
- [ ] **Arranque limpio en dev:**
  ```bash
  PYTHONPATH=. flask --app src.opdes_web_app run --port 8080
  curl -i http://127.0.0.1:8080/setup    # 200
  curl -i http://127.0.0.1:8080/login    # 302 → / (sin auth en dev) y WARNING en logs
  ```
- [ ] **Arranque limpio en prod sim:**
  ```bash
  OPDES_ENV=production OPDES_SECRET_KEY=xxx OPDES_ADMIN_TOKEN=tok \
    PYTHONPATH=. flask --app src.opdes_web_app run --port 8080
  curl -i http://127.0.0.1:8080/         # 302 → /login
  ```
- [ ] **Smoke test contra deployment** (después de Op-1 y Op-2):
  ```bash
  curl http://192.168.1.112:8081/api/catalog | jq '.ok'     # true
  curl -o /dev/null -w '%{http_code}\n' http://192.168.1.112:8081/img/show/poster   # 200
  ```

---

## Fuera de scope (deliberadamente)

- Migrar a templates externos / cleanup del JS monolito — implicaría refactor amplio.
- Reemplazar la cola en memoria por Redis/RQ — requiere infra adicional y rompe el modelo single-worker.
- Persistir historial de descargas — requiere DB.
- Métricas/observabilidad (Prometheus, etc.) — proyecto distinto.
- Sustituir polling 1s por SSE/WebSocket — cambio significativo en JS inline.
