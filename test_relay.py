import tempfile
from pathlib import Path

from telegram_relay.analyzer import analyze_message
from telegram_relay.storage import Storage
from telegram_relay.transformers import RemoveTelegramLinksTransformer, TransformerPipeline, ReplaceTextTransformer


def test_analyze_photo_caption():
    raw = {
        "message_id": 1, "date": 123, "chat": {"id": -1001, "title": "Source", "username": "source"},
        "caption": "News https://t.me/a/1",
        "caption_entities": [{"type": "url", "offset": 5, "length": 14, "url": "https://t.me/a/1"}],
        "photo": [{"file_id": "small", "file_unique_id": "u1", "width": 100, "height": 100},
                  {"file_id": "large", "file_unique_id": "u2", "width": 1000, "height": 1000}],
    }
    msg = analyze_message(raw)
    assert msg.content_type == "photo"
    assert msg.media and msg.media.file_id == "large"
    assert msg.caption.value.startswith("News")


def test_transform_does_not_touch_media():
    raw = {
        "message_id": 2, "date": 123, "chat": {"id": -1001},
        "text": "A telegram link: https://t.me/example",
        "photo": [{"file_id": "photo-id", "file_unique_id": "u", "width": 10, "height": 10}],
    }
    msg = analyze_message(raw)
    original_file = msg.media.file_id
    msg = TransformerPipeline([RemoveTelegramLinksTransformer()]).process(msg)
    assert msg.media.file_id == original_file
    assert "t.me" not in (msg.text.value or "")


def test_replace_text():
    raw = {
        "message_id": 3, "date": 123, "chat": {"id": -1001},
        "text": "hello old world", "entities": [{"type": "bold", "offset": 0, "length": 5}],
    }
    msg = analyze_message(raw)
    msg = TransformerPipeline([ReplaceTextTransformer("old", "new")]).process(msg)
    assert msg.text.value == "hello new world"
    assert msg.text.entities[0].offset == 0


def test_storage_ack_and_edit():
    with tempfile.TemporaryDirectory() as d:
        storage = Storage(str(Path(d) / "relay.db"))
        raw = {"message_id": 10, "date": 123, "chat": {"id": -1001}, "text": "one"}
        storage.ingest_message(-1001, 10, 50, "new", None, raw)
        storage.ensure_delivery_for_message("rubika", -1001, 10)
        storage.mark_processing("rubika", -1001, 10)
        storage.mark_sent("rubika", -1001, 10, "r1")
        storage.ingest_message(-1001, 10, 51, "edit", None, {**raw, "text": "two"})
        storage.ensure_delivery_for_message("rubika", -1001, 10, edit=True)
        row = storage.delivery_status("rubika", -1001, 10)
        assert row["status"] == "edit_pending"
        storage.close()


def test_storage_recovers_processing():
    with tempfile.TemporaryDirectory() as d:
        storage = Storage(str(Path(d) / "relay.db"))
        raw = {"message_id": 20, "date": 123, "chat": {"id": -1001}, "text": "x"}
        storage.ingest_message(-1001, 20, 70, "new", None, raw)
        storage.ensure_delivery_for_message("rubika", -1001, 20)
        storage.mark_processing("rubika", -1001, 20)
        assert storage.recover_processing() == 1
        row = storage.delivery_status("rubika", -1001, 20)
        assert row["status"] == "retry"
        assert row["next_attempt_at"] is not None
        storage.close()


def test_storage_album_includes_edited_member():
    with tempfile.TemporaryDirectory() as d:
        storage = Storage(str(Path(d) / "relay.db"))
        for mid, event in ((30, "new"), (31, "edit"), (32, "new")):
            raw = {
                "message_id": mid, "date": 123, "chat": {"id": -1001},
                "media_group_id": "album-x",
                "photo": [{"file_id": f"p{mid}", "file_unique_id": f"u{mid}", "width": 10, "height": 10}],
            }
            storage.ingest_message(-1001, mid, 80 + mid, event, "album-x", raw)
        rows = storage.get_source_group(-1001, "album-x")
        assert [row["source_message_id"] for row in rows] == [30, 31, 32]
        storage.close()


if __name__ == "__main__":
    test_analyze_photo_caption()
    test_transform_does_not_touch_media()
    test_replace_text()
    test_storage_ack_and_edit()
    test_storage_recovers_processing()
    test_storage_album_includes_edited_member()
    print("Self-tests passed")


def test_rubika_m4a_falls_back_to_generic_file_without_conversion(tmp_path):
    from telegram_relay.destinations import RubikaDestination

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.uploads = []
            self.calls = []

        def post(self, url, json=None, files=None, timeout=None, data=None):
            self.calls.append((url, json, files))
            if url.endswith("/requestSendFile"):
                return Response({"status": "OK", "data": {"upload_url": "https://upload.test"}})
            if url == "https://upload.test":
                # Read the uploaded bytes for both attempts.
                uploaded_name, fh, _mime = files["file"]
                self.uploads.append((uploaded_name, fh.read()))
                if len(self.uploads) == 1:
                    return Response({"status": "INVALID_INPUT", "dev_message": "Invalid_format for audio file"})
                return Response({"status": "OK", "file_id": "rubika-file-1"})
            if url.endswith("/sendFile"):
                return Response({"status": "OK", "data": {"message_id": "123"}})
            if url.endswith("/getMe"):
                return Response({"status": "OK", "data": {"user_id": "u1"}})
            raise AssertionError(url)

    media_path = tmp_path / "Voice 001.M4A"
    original = b"exact-original-m4a-bytes"
    media_path.write_bytes(original)

    raw = {
        "message_id": 77,
        "date": 123,
        "chat": {"id": -1001},
        "audio": {
            "file_id": "tg-file",
            "file_unique_id": "tg-unique",
            "file_name": "Voice 001.M4A",
            "mime_type": "audio/mp4",
            "file_size": len(original),
        },
    }
    msg = analyze_message(raw)
    session = FakeSession()
    dest = RubikaDestination("token", "rubika-chat", session)
    result = dest.send(msg, media_path)

    assert result.message_id == "123"
    assert session.uploads == [
        ("Voice 001.M4A", original),
        ("Voice 001.M4A", original),
    ]
    request_types = [call[1]["type"] for call in session.calls if call[0].endswith("/requestSendFile")]
    assert request_types == ["Music", "File"]


def test_rubika_voice_falls_back_to_generic_file(tmp_path):
    from telegram_relay.destinations import RubikaDestination

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.upload_types = []
            self.upload_bytes = []

        def post(self, url, json=None, files=None, timeout=None, data=None):
            if url.endswith("/requestSendFile"):
                self.upload_types.append(json["type"])
                return Response({"status": "OK", "data": {"upload_url": "https://upload.test"}})
            if url == "https://upload.test":
                name, fh, _mime = files["file"]
                self.upload_bytes.append(fh.read())
                if len(self.upload_bytes) == 1:
                    return Response({"status": "INVALID_INPUT", "dev_message": "Invalid format for voice file"})
                return Response({"status": "OK", "file_id": "voice-as-file"})
            if url.endswith("/sendFile"):
                return Response({"status": "OK", "data": {"message_id": "555"}})
            raise AssertionError(url)

    media_path = tmp_path / "voice.ogg"
    original = b"exact-voice-bytes"
    media_path.write_bytes(original)
    raw = {
        "message_id": 78,
        "date": 123,
        "chat": {"id": -1001},
        "voice": {
            "file_id": "tg-voice",
            "file_unique_id": "tg-voice-unique",
            "mime_type": "audio/ogg",
            "file_size": len(original),
        },
    }
    msg = analyze_message(raw)
    session = FakeSession()
    dest = RubikaDestination("token", "chat", session)
    result = dest.send(msg, media_path)

    assert result.message_id == "555"
    assert session.upload_types == ["Voice", "File"]
    assert session.upload_bytes == [original, original]
