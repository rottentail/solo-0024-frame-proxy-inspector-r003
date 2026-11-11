# Frame Proxy Inspector

Frame Proxy Inspector is a small Python backend for turning uncompressed AVI
footage into two review artifacts:

- a deterministic shot index with contiguous scene ranges;
- a bounded PPM contact sheet with one tile per indexed scene.

The service verifies the RIFF/AVI container, frame geometry, and frame chunk
count before it admits a clip. Shot indexing then computes luma and color
fingerprints from sampled frames and applies a minimum-scene guard to hard
cuts. Contact sheet requests are checked against a fixed pixel budget before
frame decoding starts.

Automated unit, integration, and browser tests are intentionally deferred to a
later task stage. This baseline ships executable workflow checks instead.

## Requirements

- Python 3.11 or newer
- No third-party packages
- No external media service

## Start the service

```bash
export PYTHONPATH=src
python3 -m frame_proxy serve --host 127.0.0.1 --port 8765
```

The service prints its bound URL. Press `Ctrl+C` to stop it.

## Generate demonstration footage

```bash
PYTHONPATH=src python3 -m frame_proxy demo-avi \
  --output demo/scene-walk.avi \
  --width 128 \
  --height 72 \
  --fps 12
```

The generated file is an uncompressed 24-bit AVI with four color scenes and
17 total frames. It is suitable for exercising all three workflows.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | service check |
| `PUT` | `/v1/clips/{clip_id}` | admit an `application/x-avi` payload |
| `GET` | `/v1/clips/{clip_id}` | read clip geometry and digest |
| `POST` | `/v1/clips/{clip_id}/inspections` | index shot boundaries |
| `GET` | `/v1/inspections/{inspection_id}` | read an inspection with its attempt history |
| `POST` | `/v1/inspections/{inspection_id}/attempts` | run a failed inspection again under the same identifier |
| `GET` | `/v1/inspections/{inspection_id}/attempts` | list every execution attempt |
| `POST` | `/v1/inspections/{inspection_id}/contact-sheets` | compose a PPM grid |
| `GET` | `/v1/contact-sheets/{sheet_id}` | read the tile map and digest |
| `GET` | `/v1/contact-sheets/{sheet_id}/artifact` | fetch the PPM bytes |

Identifiers use lowercase letters, digits, `_`, and `-`, begin with a letter,
and contain 3 to 48 characters.

Example inspection request:

```json
{
  "inspection_id": "inspection-demo-a",
  "sensitivity": 0.55,
  "sample_stride": 1,
  "min_scene_frames": 2
}
```

Example contact sheet request:

```json
{
  "sheet_id": "sheet-demo-a",
  "columns": 3,
  "tile_width": 96,
  "gutter": 4,
  "selection": "stored",
  "background": "#20252c"
}
```

`selection` may be `stored`, `midpoint`, or `first`. The rendered PPM is
binary P6 with a maximum value of 255.

## Inspection attempts, retries, and recovery

Each execution of an inspection is an attempt with its own identifier,
timestamps, and outcome (`succeeded`, `failed`, or `interrupted`). Attempts
are append-only and are returned inside the inspection record, so a failure
reason never disappears when the inspection runs again.

A `failed` inspection is retried in place — no new business identifier is
needed:

```bash
curl -X POST http://127.0.0.1:8765/v1/inspections/inspection-demo-a/attempts
```

The retry reuses the stored sensitivity, stride, and minimum scene length
and decodes the clip from the first frame. It answers `409` while another
attempt is running or once the inspection is `indexed`. An inspection
commits at most one scene result: commit and failure writes are fenced by
the active attempt identifier, so a superseded or late attempt cannot
overwrite the result that contact sheets consume.

On startup the service scans persisted inspections. Any record still in
`pending` or `scanning` belongs to an attempt the previous process could
not finish; recovery closes it as `interrupted` with the stable
`inspection_interrupted` code and moves the inspection to `failed`, where
the same retry endpoint picks it up again.

## Data layout

`FRAME_PROXY_DATA_DIR` selects the data root. Its default is
`.frame-proxy-data` in the current working directory.

```text
.frame-proxy-data/
├── clips/<clip_id>/
│   ├── clip.avi
│   └── record.json
├── inspections/<inspection_id>.json
├── contact-sheets/<sheet_id>/
│   ├── record.json
│   └── sheet.ppm
└── locks/
```

Records are written through temporary files and renamed into place. Contact
sheet bytes and their ready record are verified before the record becomes
visible.

## Source layout

```text
src/frame_proxy/
├── domain/      records, request rules, and state transitions
├── http/        request routing and response mapping
├── media/       AVI parsing, frame access, fingerprints, and PPM grids
├── service/     workflow orchestration and failure handling
└── storage/     namespaced files, locks, and atomic writes

tools/
├── harness.py
├── check_clip_intake.py
├── check_shot_index.py
├── check_inspection_retry.py
└── check_contact_sheet.py
```

## Build and workflow checks

Build:

```bash
python3 -m compileall -q src tools
```

Run one workflow check at a time:

```bash
PYTHONPATH=src:. python3 tools/check_clip_intake.py
PYTHONPATH=src:. python3 tools/check_shot_index.py
PYTHONPATH=src:. python3 tools/check_inspection_retry.py
PYTHONPATH=src:. python3 tools/check_contact_sheet.py
```

Each check starts a real loopback HTTP server, generates media through the
production AVI writer, completes the named workflow, and verifies persisted
state or artifact bytes. Temporary records are removed when the check exits.

## Environment variables

| Name | Default | Purpose |
| --- | --- | --- |
| `FRAME_PROXY_DATA_DIR` | `.frame-proxy-data` | durable data root |

Clip, upload, JSON, frame dimension, frame count, and contact sheet limits are
declared in `src/frame_proxy/config.py`.

## Input boundary

The current adapter accepts one uncompressed 24-bit BGR video stream in a
RIFF/AVI container. Audio streams, compressed video, and additional container
formats are intentionally outside this baseline. The extension seams for those
capabilities are recorded in `.task-source.json`.

