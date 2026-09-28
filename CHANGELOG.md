# Changelog

## 0.4.0 - 2026-09-29

### Added
- Persistent local inbox and delivery queue separation.
- Restart recovery for in-flight delivery jobs.
- Media-group aggregation at the relay core.
- Rubika voice/audio fallback to generic file upload without transcoding.
- Operator commands for delivery inspection, retry, and cancellation.

### Changed
- Permanent destination input errors no longer retry indefinitely.
- Telegram ingestion advances the update offset after durable local persistence, not after successful destination delivery.
- Bale configuration accepts `BALE_BOT_TOKEN` while retaining `BALE_TOKEN` compatibility.

### Known limitations
- Rubika does not currently expose a documented album/group-send method in the Bot API; media groups are therefore delivered item-by-item there.
- Telegram voice messages are currently delivered to Rubika as the original OGG/Opus file when native voice upload is rejected.
- Sticker and animation/GIF messages may be delivered as generic files depending on destination capabilities.
- Bale integration is implemented but still needs full end-to-end validation in a live Bale environment.
