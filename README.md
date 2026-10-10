# Meater Export

Export full-resolution temperature history from completed MEATER cooks to CSV.

The MEATER app only shares a picture of the cook graph. This script pulls the raw readings from MEATER Cloud using the (unofficial) private API the mobile app uses, so you get every internal and ambient reading for every cook in your account.

> Unofficial and unsupported. It uses an undocumented API reverse-engineered from the iOS app (based on [Zahlii/meater-api](https://github.com/Zahlii/meater-api)) and may break whenever MEATER changes their backend.

## Setup

```bash
pip install -r requirements.txt
```

Keep `meater_cuts.json` in the same folder as the script. It maps the cut and preset IDs stored with each cook to names (beef / sirloin / medium_rare, etc.).

## Usage

```bash
python meater_export.py                    # interactive: list cooks, pick one or "all"
python meater_export.py --list             # list cooks only
python meater_export.py --all              # export every cook
python meater_export.py --cook 60D5F5DA    # export one cook by ID prefix
python meater_export.py --all --dump       # also save the raw JSON per cook
python meater_export.py --reauth           # discard the cached token and log in again
```

Credentials are prompted for, or set `MEATER_EMAIL` / `MEATER_PASSWORD`, or pass `--email` / `--password`.

After the first login the access token is cached in `meater_config.json` next to the script (git-ignored). Exports go to `meater_exports/` by default (`--outdir` to change).

## Output

One CSV per cook:

| column | description |
|---|---|
| `timestamp` | Unix time of the reading |
| `time_utc` | Same, as a UTC string |
| `internal_temp_c` | Meat temperature, °C |
| `ambient_temp_c` | Ambient temperature, °C |
| `category`, `animal`, `cut_type`, `cut` | Meat type, from `meater_cuts.json` |
| `preset` | Selected doneness / temperature range |

## How it works

- `POST https://api.cloud.meater.com/login` with an iOS-style device payload returns an `accessToken` (HTTP 201).
- `GET /v2/cooks?page=N` lists cooks, 10 per page, until `meta.pagination.hasMore` is false.
- `GET /v2/cooks/{id}` returns the cook. Readings are in `raw.history.values`, spaced `raw.history.interval` seconds apart from `raw.history.startTime`.
- Temperatures are stored as integers in 1/16 °C, so the script divides by 16.
- `raw.setup.cutID` and `raw.setup.presetID` are resolved against `meater_cuts.json`.
