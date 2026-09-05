# phobe / Phone Default Harvester

A GitHub-Actions-first prototype for finding **only the factory default** ringtone, notification sound and alarm for phones in André Louis' Phone Tones archive.

The repository is intentionally tiny. The heavy lifting runs on GitHub's runner and the results come back as a workflow artifact, so the local PC does not need to store whole phone sound collections or firmware dumps.

## Run it

1. Open **Actions**.
2. Choose **Harvest phone defaults**.
3. Click **Run workflow**.
4. Enter a brand folder such as `Samsung`, `Xiaomi`, `Motorola`, `Huawei`, `OnePlus`, `Sony`, etc.
5. Keep `limit_models=3` for the first test. Set it to `0` only when you want the whole brand.
6. Leave **download audio** enabled if you want the matched files themselves.
7. When the job finishes, download the `phone-defaults-...` artifact from the workflow run.

No personal GitHub token is required for the normal Action. The workflow passes its built-in `GITHUB_TOKEN` to the harvester for public firmware-repository searches.

## Artifact contents

```text
out/<brand>/
├── catalog.json
├── results.csv
├── confirmed.csv
├── unresolved.csv
├── results.json
├── evidence/
│   └── <model>.json
└── audio/                 # only when download_audio=true
    └── <brand>/<model>/
        ├── ringtone.*
        ├── notification.*
        ├── alarm.*
        └── metadata.json
```

## Confidence

- **CONFIRMED**: firmware declares all three defaults and all three match files in that model's archive folder.
- **PARTIAL**: at least one declared default matches archived audio.
- **EVIDENCE_ONLY**: firmware/default properties were found but filenames did not match the archive.
- **UNRESOLVED**: no trustworthy default was found. The script deliberately does not guess.

## What the prototype searches

For Android firmware/configuration it looks for values such as:

```text
ro.config.ringtone
ro.config.notification_sound
ro.config.alarm_alert
default_ringtone
default_notification_sound
default_alarm_alert
```

It searches candidate public GitHub firmware/vendor/dump repositories, scans likely `.prop` and settings/config XML files, then matches declared filenames against the audio files in André Louis' per-model archive folder.

## Main limitation

Retail names and Android device codenames are still the big gremlin. An archive might say `Galaxy-S9` while a firmware repository says `dreamlte`. Those can remain unresolved even when the information exists. The next major improvement is a retail-model ↔ codename resolver and caching.

This prototype is Android-oriented. iOS and old proprietary/feature-phone firmware need separate resolvers.

## Storage behaviour

The workflow does **not** mirror the entire sound archive. It crawls directory listings and downloads only the ringtone/notification/alarm that were actually resolved. Results are uploaded as GitHub Actions artifacts with 30-day retention.
