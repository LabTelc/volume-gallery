# Configuration

All settings are environment variables. With Docker Compose you set them in `.env` (copy
`.env.example`, which lists every one of them); `docker-compose.yml` passes them on to the
containers. Defaults below are the ones used when a variable is unset or empty.

"Used by" names the container that reads the variable: **compose** = read by Docker Compose
itself (mounts, ports, user, image), **init** = the one-shot folder helper, **app** = the web
application, **worker** = the conversion worker, **nginx** = the reverse proxy.

| Variable | Default | Used by | Description |
|---|---|---|---|
| `VG_SECRET_KEY` | none (required) | app, worker | Signs login and download tokens; at least 32 characters. The app refuses to start without it; changing it signs everyone out. |
| `VG_ADMIN_USER` | `admin` | app | User name of the first-run admin, created only when there are no users. |
| `VG_ADMIN_PASSWORD` | none (`.env.example`: `changeme`) | app | Password of that first admin. |
| `VG_HOST_DATA_DIR` | `./data` (`.env.example`: `/mnt/tank/apps/volume-gallery/data`) | compose, init, nginx | Host folder mounted as `/data`: database, volumes, meshes, point clouds. |
| `VG_HOST_PROCESSING_DIR` | `./processing` | compose, init, nginx | Host scratch folder mounted as `/processing` for uploads and conversions; allow 2–3 times the largest upload. |
| `VG_PROCESSING_DIR` | empty (= `/processing`) | app, worker | Processing path inside the containers; normally left empty. |
| `VG_HOST_BRANDING_DIR` | `./branding` | compose | Host folder with `branding.toml` and logos, mounted read-only at `/branding`. |
| `VG_CERT_DIR` | `./nginx/certs` | compose, nginx | Host folder with `fullchain.pem` and `privkey.pem`. |
| `VG_IMAGE` | `volume-gallery:latest` | compose | Image of the app and the worker (e.g. a Docker Hub image). |
| `VG_UID` | `0` | compose, init, nginx | Numeric user the app, worker and nginx workers run as (TrueNAS apps user: 568). |
| `VG_GID` | `0` | compose, init, nginx | Numeric group they run as. |
| `VG_HTTP_PORT` | `8080` | compose | Host port for HTTP (redirects to HTTPS). |
| `VG_HTTPS_PORT` | `8443` | compose, nginx | Host port for HTTPS; also used in redirects and forwarded headers. |
| `VG_NGINX_REQUEST_BUFFERING` | `off` | nginx | `off` streams uploads straight to disk; `on` buffers them in nginx. |
| `VG_SHARD_ACCEL_REDIRECT` | `true` | app | Let nginx serve volume data files (X-Accel-Redirect). Use `false` without nginx. |
| `VG_REDIS_URL` | `redis://redis:6379/0` | app, worker | Redis job queue. Without it the web process converts uploads itself. |
| `VG_QUEUE_NAME` | `volume-gallery` | app, worker | Name of the queue; must be the same for both. |
| `VG_JOB_TIMEOUT_HOURS` | `72` | app | Longest a queued conversion may run (0 = no limit). |
| `VG_CONVERTER_TIMEOUT_HOURS` | `24` | app, worker | Longest a mesh or point-cloud converter may run on one upload (0 = no limit). |
| `VG_MESH_MAX_TEXTURE_SIZE` | `8192` | app, worker | Mesh textures larger than this (longest side, px) are scaled down while converting; 0 keeps them. |
| `VG_MESH_EXTRA_MAX_MB` | `256` | app | Largest texture or `.mtl` file uploaded together with a mesh, in MB. |
| `VG_UPLOAD_STAGING_MAX_AGE_HOURS` | `24` | app | Unclaimed staged uploads older than this are deleted. |
| `VG_ACCESS_TOKEN_MINUTES` | `480` | app | Login session length. |
| `VG_FILE_TOKEN_MINUTES` | `30` | app | Lifetime of the token that authorizes a data or mesh download. |
| `VG_MIN_PASSWORD_LENGTH` | `8` | app | Shortest password accepted. |
| `VG_PASSWORD_LINK_HOURS` | `72` | app | How long a set-password / reset link works. |
| `VG_AUDIT_RETENTION_DAYS` | `0` | app | Days the audit log is kept (0 = for ever). |
| `VG_SHARE_CODE_MIN_LENGTH` | `8` | app | Shortest share-link code (at least 4 is enforced). |
| `VG_SHARE_CODE_MAX_LENGTH` | `24` | app | Longest share-link code (at least 8 is enforced). |
| `VG_THUMBNAIL_MAX_MB` | `5` | app | Largest accepted thumbnail upload, in MB. |
| `VG_FILE_META_CACHE_TTL_SECONDS` | `60` | app | How long a scan of a volume's files is cached. |
| `VG_SLICE_CACHE_MB` | `2048` | app | RAM for decoded slice slabs shared by all viewers (0 = no cache, much slower). |
| `VG_SLICE_BUILD_THREADS` | `2` | app | Slabs decoded in parallel. |
| `VG_SLICE_ZSTD_LEVEL` | `1` | app | zstd level for planes sent to the browser (1 = fastest). |
| `VG_HISTOGRAM_THREADS` | empty (= smaller of 4 and the CPU count) | app, worker | Threads used to compute histograms. |
| `VG_HISTOGRAM_BACKFILL` | `1` | app | Compute missing histograms of existing volumes in the background at start-up (`0` turns it off). |

## Fixed inside the image

These are set by the image or are tied to other files, so they are not meant to be changed from
`.env`: `VG_DATA_DIR` (`/data`), `VG_FRONTEND_DIR`, `VG_BRANDING_DIR` (use `VG_HOST_BRANDING_DIR`),
`VG_NXSBUILD_BIN`, `VG_NXSCOMPRESS_BIN`, `VG_POTREE_CONVERTER_BIN` (converter paths) and
`VG_SHARD_ACCEL_PREFIX` (must match the internal location in `nginx/nginx.conf.template`).
