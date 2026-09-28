# Telegram Relay

A persistent, extensible relay for publishing Telegram channel posts to multiple messaging platforms.

Current focus:

- Telegram as the source
- Rubika as a tested destination
- Bale as an implemented destination pending full live validation

The project is structured around a universal message model, independent text transformation, persistent delivery state, and destination adapters. Adding another destination should not require rewriting the Telegram ingestion layer.

## Features

- Telegram Bot API polling with durable local ingestion
- Source-channel filtering
- Text, caption, entity, and media extraction
- Photo, video, document, audio, voice, animation, sticker, and video-note handling
- Telegram media-group aggregation
- Persistent SQLite inbox and delivery state
- Restart recovery for in-flight jobs
- Retry with backoff for transient failures
- Permanent-failure handling for unsupported or invalid input
- Per-destination delivery mapping and idempotency
- Message edit propagation
- Entity-aware text transformation pipeline
- Telegram-link removal transformer
- Rubika media upload with audio/voice fallback to the original file bytes
- Optional Bale destination adapter
- Local operator commands for status, retry, and cancellation

## Architecture

```text
                    Telegram
                       |
                       v
              +-------------------+
              | Telegram Ingestion|
              +---------+---------+
                        |
                        v
              +-------------------+
              |   SQLite Inbox    |
              +---------+---------+
                        |
                        v
              +-------------------+
              | Message Analyzer  |
              +---------+---------+
                        |
                        v
              +-------------------+
              | Text Transformers |
              +---------+---------+
                        |
                        v
              +-------------------+
              | Persistent Queue  |
              +---------+---------+
                        |
               +--------+--------+
               |                 |
               v                 v
        +-------------+   +-------------+
        |   Rubika    |   |     Bale    |
        |  Adapter    |   |   Adapter   |
        +-------------+   +-------------+
```

Telegram ingestion and destination delivery are deliberately separated. A destination failure should not block later Telegram updates from being accepted and stored locally.

## Project Layout

```text
telegram-relay/
|-- main.py
|-- manage.py
|-- requirements.txt
|-- requirements-dev.txt
|-- pyproject.toml
|-- .env.example
|-- .gitignore
|-- README.md
|-- CHANGELOG.md
|-- test_relay.py
`-- telegram_relay/
    |-- __init__.py
    |-- analyzer.py
    |-- config.py
    |-- destinations.py
    |-- dispatcher.py
    |-- media.py
    |-- models.py
    |-- storage.py
    |-- telegram_client.py
    `-- transformers.py
```

## Requirements

- Python 3.10+
- A Telegram bot that can receive posts from the source channel
- At least one configured destination
- Network access to the configured APIs

## Installation

### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Create `.env` beside `main.py` using `.env.example` as the template.

Then run:

```bash
python main.py
```

## Configuration

Example:

```dotenv
TELEGRAM_BOT_TOKEN=123456:replace-me
TELEGRAM_SOURCE_CHANNEL_ID=-1001234567890

RUBIKA_TOKEN=replace-me
RUBIKA_CHAT_ID=replace-me

BALE_BOT_TOKEN=
BALE_CHAT_ID=
```

Runtime options:

```dotenv
POLL_TIMEOUT=30
REQUEST_TIMEOUT=60
RETRY_COUNT=4
RETRY_MAX_DELAY=60
ALBUM_WAIT_SECONDS=1.5
WORKER_INTERVAL=1.0
DATABASE_PATH=data/relay.db
MEDIA_DIR=data/media
TRANSFORM_TELEGRAM_LINKS=false
```

For larger Telegram media downloads, a local Telegram Bot API server can be configured with:

```dotenv
TELEGRAM_API_BASE=http://127.0.0.1:8081
```

## Destination Configuration

### Rubika

Set both values to enable Rubika:

```dotenv
RUBIKA_TOKEN=...
RUBIKA_CHAT_ID=...
```

### Bale

Set both values to enable Bale:

```dotenv
BALE_BOT_TOKEN=...
BALE_CHAT_ID=...
```

`BALE_TOKEN` is also accepted for backward compatibility.

Destinations with missing credentials are not instantiated.

## Message Processing

The relay first converts Telegram messages into an internal model containing fields such as:

- message ID
- source chat ID
- text
- caption
- Telegram entities
- media metadata
- media-group ID
- raw Telegram message data

Text transformations operate on text and captions only. Media bytes are kept separate from the transformation layer.

This makes transformations such as these possible without coupling them to destination-specific code:

```text
Remove Telegram links
Replace channel identifiers
Replace selected words
Add a footer
Normalize text
```

## Delivery State

Delivery state is stored in SQLite. A typical lifecycle is:

```text
pending -> processing -> sent
                    \-> retry -> processing
                    \-> failed_permanent
                    \-> cancelled
```

If the process stops while a job is being processed, startup recovery returns the job to a retryable state.

Successful deliveries are keyed by destination, source chat, and source message. This prevents ordinary restarts from resending successfully recorded messages.

Exact-once delivery cannot be guaranteed across an external API and a local database. If a destination accepts a request but the process crashes before the success record is committed, a later retry may create a duplicate.

## Albums / Media Groups

Telegram media groups are collected by `media_group_id` before dispatch.

- Bale uses a native media-group request when supported.
- Rubika currently has no documented Bot API equivalent for group sending, so the adapter sends the collected items individually in their original order.

The relay core still treats the Telegram media group as one logical source group.

## Media Compatibility

The relay does not transcode media merely to hide destination limitations.

For Rubika, if an audio or voice upload is rejected as an invalid format, the adapter retries the same local file bytes as a generic file. This preserves the original file without transcoding.

Current known behavior:

- Telegram voice may arrive in Rubika as an OGG/Opus file when native voice upload is rejected.
- Sticker messages may be delivered as files.
- Animation/GIF messages may be delivered as files.

## Editing Messages

Source edits are tracked separately from new messages and mapped back to destination message IDs when possible.

The adapter attempts to update an existing destination message before falling back to destination-specific behavior when the destination API does not support the required edit operation.

## Operator Commands

Show recent delivery records:

```bash
python manage.py status
```

Cancel a delivery:

```bash
python manage.py cancel rubika 63
```

Queue a delivery for retry:

```bash
python manage.py retry rubika 63
```

Specify a different source chat ID:

```bash
python manage.py retry rubika 63 --chat-id -1001234567890
```

## Testing

Run the full test suite:

```bash
pytest
```

The current test suite covers message analysis, text transformation, entity handling, SQLite state transitions, restart recovery, media-group persistence, and Rubika audio/voice fallback behavior.

## Security

Never commit or share:

- `.env`
- bot tokens
- `data/relay.db`
- downloaded media under `data/media/`

If a bot token is exposed, revoke and regenerate it immediately through the platform's bot management tools.

## Official API References

- Telegram Bot API: https://core.telegram.org/bots/api
- Bale Bot API: https://docs.bale.ai/
- Rubika Bot API: https://rubika.ir/botapi

## Roadmap

Planned improvements include:

- Native Rubika voice delivery where supported
- More destination-specific media mappings
- Additional destination adapters such as Soroush Plus
- Expanded live integration coverage for Bale
- More robust operational tooling and observability

## License

No license has been selected yet. Until a license is added to the repository, the default copyright rules apply.
