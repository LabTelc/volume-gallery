# Usage & UI Guide

## User management

Nobody but their owner ever chooses a user's password. **Add user** (on the *Users* page) creates the account
without one and shows a one-time link, `https://<host>/set-password.html#token=…`, for the admin to hand to the
new user, who opens it and sets their password (and is signed in). To reset a forgotten password the admin
presses **Reset password**, which makes a new link of the same kind; the user's old password and sessions keep
working until the link is used. A link works once, a newer link for the same user replaces it, and it expires
after `VG_PASSWORD_LINK_HOURS` (default 72). Only a hash of the token is stored (table `usertoken`), the token
travels in the URL fragment and in request bodies (never in a query string, so not in access logs), and the
endpoints `POST /api/users`, `POST /api/users/<id>/password-link` (admin) and
`POST /api/auth/set-password[/check]` (public, rate-limited by nginx) carry the flow. `PATCH /api/users/<id>`
no longer accepts a `password`.

## Audit log
Security-relevant actions are written to the `auditevent` table in the same transaction as the
action, and admins read them on the **Audit log** page (`GET /api/admin/audit`, filterable by kind, user, target and address; *Export CSV* saves the loaded rows). Logged: sign-ins and failed sign-ins, setting passwords, creating, changing, deleting accounts and making password links, uploading and deleting volumes / meshes / point clouds, access grants and revocations, and share links. Viewing is not logged (share links count their own views), and secrets never are: no passwords, no tokens. A failed sign-in for a name that is not an account is logged without that name, because people paste passwords into the username box. Addresses come from the `X-Real-IP` header nginx sets, so they are only reliable behind it. Lines are kept for ever unless `VG_AUDIT_RETENTION_DAYS` is set (older ones are deleted at startup); names are copied into each line, so it stays readable after an account or item is deleted.

## Preparing Volumes to Upload

The volume uploader accepts a **.zip of single-slice TIFF files** (one `.tif` or `.tiff` per Z position), or multiple TIFF files selected together. The backend converts the input to kiln-render's sharded binary layout using out-of-core Dask processing. Supported scalar input types are `uint8` and `uint16`.

**Note on Sorting:** Slice order is sorted naturally by filename (e.g., `slice_0001.tif`, `slice_0002.tif`). Plain numeric names (`1.tif`, `2.tif`) can sort incorrectly once the sequence reaches two digits, so always zero-pad them.

The mesh field accepts any supported mesh file (STL/OBJ/PLY/...) and stores the original for download.

## Viewer Features

*   **Export:** Saves the 3D render or a slice as PNG / JPEG / WebP / TIFF (slices at one pixel per voxel, optionally as raw voxel values).
*   **Animate:** Renders a slice sweep, a 3D turntable, or a camera path to an H.264 MP4 or a zip of images. Both run in the browser on the viewer's own GPU.
*   **Live Playback:** Animation paths can be played live and looped on screen. Shared path links (`viewer ... ?id=<volume>&play=<path>`) start automatically.
*   **Bookmarks:** Save a view (camera, render settings, slice position, thumbnail). Their camera poses can be keyframes of a path or the start of a turntable.
*   **Long-Running Displays:** While anything long-running is going on, the viewer renews its data access token in the background, so a display can run for hours.
*   **Slices at Any Angle:** The three slice panes show three perpendicular planes through the cursor. The server streams exactly the blocks needed for the requested plane angle.

The gallery's **Layout** is *List* or *Squares* (cards that fill the width of the page); it is remembered per browser.

## Reports, attachments and TIFF stacks

Every volume has a **report page** (`/report.html?id=<volume id>`: *Report* on its gallery row, or the Report button of the viewer). Anybody who can see the volume reads it; editors and administrators write it.

- **Editing** opens a full-screen editor: the text on the left and its preview on the right, two panes of the same size that scroll together (a block of the text lines up with the block it renders to, also next to a tall formula or image). *Images* and *Help* open a drawer under the toolbar; Ctrl+S saves.
- **Text:** markdown, with LaTeX formulas (`$E = mc^2$` in a line, `$$ ... $$` on lines of their own) and images (uploaded from the editor, written as `![caption](img:12)`; drop or paste an image into the text to add one). Raw HTML is not shown, and images from other sites are not loaded, so a report cannot run anything in a reader's browser. The text is the file `<data>/volumes/<slug>/report/report.md` (the uploaded images are beside it); the database only records who saved it last and when. A copy of the data folder therefore carries the reports, and a report can be read or restored as a plain file.
- **Attachments:** any files attached to the volume, downloadable by everybody who can see the volume. They are kept in `<data>/volumes/<slug>/attachments/` and are deleted with the volume. A share link shows the report and the list, and lets people take the files only when the link was made with *allow download of the attachments*.
- **TIFF stack:** *Download as TIFF stack* asks for the volume as one TIFF file per slice in a ZIP, at a chosen resolution level (level 0 is the full resolution; each level after it halves every side). The server prepares it in the background: it decodes the bricks of that level and writes the slices straight into the ZIP, in `<data>/volumes/<slug>/exports/`. The **Processing** page (side menu) shows the task and its progress and offers the download when it is ready. The ZIP is kept for `VG_EXPORT_RETENTION_HOURS` (24 by default) and then deleted; the task shows as expired and can be asked for again. Asking for a stack that is already made or being made returns that task. The voxel size is written into the TIFF files (when the volume's unit is a length) and into `metadata.json` in the ZIP. Not for share-link guests (they have no Processing page).

The Processing page is where other background work on volumes will appear too: a task is a row with a kind, its settings, a progress, a result file and an expiry (`backend/app/processing.py` says how to add a kind).

## Histogram

The volume viewer draws a histogram behind the transfer-function curve. It is computed from the original full-resolution TIFF stack during conversion and stored as `histogram.json`. `VG_HISTOGRAM_THREADS` (default: up to 4) limits CPU use during conversion. 

## Branding

The name, logo, tagline, copyright, and contact details come from `branding/branding.toml`.
*   It is read on every request (no restart needed).
*   Docker mounts the folder as `/branding` (`VG_HOST_BRANDING_DIR`, default `./branding`). 
*   Replace `branding/logo.svg` with your own logo; set `logo_dark` if it needs a different version on the dark theme.

*For more details on background processing, see the [Architecture Guide](architecture.md).*