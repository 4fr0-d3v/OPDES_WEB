# One Pace DES Web

Aplicación Flask local para gestionar descargas de One Pace, metadatos Jellyfin y estado de episodios.

## Arranque local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export OPDES_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export OPDES_ADMIN_TOKEN="cambia-este-token"
PYTHONPATH=. flask --app src.opdes_web_app run --host 127.0.0.1 --port 8080
```

Abre `http://127.0.0.1:8080` y entra con cualquier usuario y el valor de `OPDES_ADMIN_TOKEN` como contraseña.

## Producción con gunicorn

```bash
export OPDES_ENV=production
export OPDES_SECRET_KEY="valor-largo-aleatorio"
export OPDES_ADMIN_USER="admin"
export OPDES_ADMIN_PASSWORD_HASH="hash-generado-con-werkzeug"
export OPDES_ALLOWED_PATHS="/mnt/nfs/data/media/series/OnePiece,/mnt/nfs/data/media/metadata"
export OPDES_ALLOWED_REMOTE_HOSTS="onepace.net,pixeldrain.net,pixeldrain.com,192.168.1.204"
gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 4 src.opdes_web_app:app
```

`OPDES_SECRET_KEY` y una credencial admin son obligatorios cuando `OPDES_ENV=production`.

Usa un solo worker de `gunicorn`: la cola de descargas vive en memoria del proceso. La concurrencia se controla con hilos y `OPDES_MAX_CONCURRENT_DOWNLOADS`.

## Docker Compose

```bash
cp docker-compose.yml.example docker-compose.yml
# Edita secretos, rutas y hosts permitidos.
docker compose up --build
```

El ejemplo publica la app en `8081` y monta un volumen persistente para `~/.opdes/config.json`.

## Variables de entorno

- `OPDES_ENV`: usa `production` para endurecer arranque y exigir secretos.
- `OPDES_SECRET_KEY`: clave de sesión Flask.
- `OPDES_ADMIN_TOKEN`: token simple de administración local.
- `OPDES_ADMIN_USER` y `OPDES_ADMIN_PASSWORD_HASH`: alternativa con usuario y hash Werkzeug.
- `OPDES_ALLOWED_PATHS`: lista separada por comas de raíces donde se permite configurar rutas de descarga/metadatos.
- `OPDES_ALLOWED_REMOTE_HOSTS`: lista separada por comas de hosts remotos permitidos.
- `OPDES_MAX_CONCURRENT_DOWNLOADS`: concurrencia de la cola de descargas, entre 1 y 8. Por defecto `2`.
- `OPDES_SESSION_SECURE`: usa `true` si sirves la app por HTTPS.

## Seguridad aplicada

- La UI puede protegerse con sesión local y cookies `HttpOnly` / `SameSite=Lax`.
- Todos los endpoints `POST` requieren CSRF.
- `/sync-metadata` solo acepta `POST`.
- El token de Jellyfin no se renderiza en HTML; dejarlo vacío en configuración conserva el valor existente.
- Las rutas configuradas se normalizan y pueden limitarse con `OPDES_ALLOWED_PATHS`.
- La extracción ZIP valida que todos los miembros queden dentro del destino.
- La respuesta añade cabeceras básicas de seguridad.

## Tests

```bash
PYTHONPATH=. pytest -q
```

La suite cubre CSRF, login/configuración sensible, extracción ZIP segura, parsing básico de Pixeldrain, disponibilidad de episodios y endpoints de cola.

## Troubleshooting

### Todo redirige a /setup o /api responde 428

Síntoma: cualquier endpoint funcional devuelve `metadata_sync_required` o
redirige al wizard.

Causa habitual: el contenedor no puede leer `metadata_dir`. Revisa
`POST /sync-metadata` desde la UI — el flash mostrará el `errno` exacto
(p.ej. `Permission denied: '/mnt/nfs/data'`). Confirma que el bind mount
NFS llega al contenedor y que el path en `~/.opdes/config.json` coincide
con la ruta dentro del contenedor (no la del host). `/setup` ahora marca
en rojo los paths que no son accesibles, así que confirma allí primero.

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

Vacía `_catalog_cache`, `_pixeldrain_episode_cache` y los tres caches
Jellyfin.

### La app expone /setup sin pedir login

Significa que `OPDES_ENV` no es `production` y no hay admin configurado.
Revisa `docker logs <container>` — al arranque debería verse el
`WARNING: OPDES corre sin autenticacion admin.` Si lo ves, configura
`OPDES_ENV=production` y `OPDES_ADMIN_TOKEN` (o `OPDES_ADMIN_USER` +
`OPDES_ADMIN_PASSWORD_HASH`) y reinicia.
