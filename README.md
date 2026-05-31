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
gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 4 src.opdes_web_app:app
```

`OPDES_SECRET_KEY` y una credencial admin son obligatorios cuando `OPDES_ENV=production`.

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
