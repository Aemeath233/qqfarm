import json
from datetime import UTC, datetime
from zipfile import ZipFile

import pytest

from qqfarm import wire
from qqfarm.config import FarmError
from qqfarm.web_authorization import inspect_authorization, require_fresh_authorization


def login_archive(path, timestamps, *, reply_seq=1, reply_service="gamepb.userpb.UserService"):
    with ZipFile(path, "w") as archive:
        for index, timestamp in enumerate(timestamps, 1):
            archive.writestr(
                f"raw/{index}_c.txt",
                "GET https://gate-obt.nqf.qq.com/prod/ws?code=private-test-code HTTP/1.1\r\n\r\n",
            )
            request = wire.encode_gate("gamepb.userpb.UserService", "Login", 1, 0, b"template")
            meta = (
                wire.text(1, reply_service)
                + wire.text(2, "Login")
                + wire.uint(3, 2)
                + wire.uint(4, reply_seq)
            )
            reply = wire.blob(1, meta) + wire.blob(2, wire.blob(1, wire.uint(1, 123)))
            records = b""
            for outgoing, body in ((True, request), (False, reply)):
                if timestamp == "incomplete" and not outgoing:
                    continue
                frame = b"\x82" + bytes([len(body)]) + body
                direction = "Request" if outgoing else "Response"
                stamp = "" if timestamp in {None, "incomplete"} else f"DoneRead: {timestamp}\r\n"
                records += (
                    f"{direction}-Length: {len(frame)}\r\n{stamp}\r\n".encode() + frame + b"\r\n"
                )
            archive.writestr(f"raw/{index}_w.txt", records)


def test_actual_login_time_is_used_instead_of_new_archive_timestamp(tmp_path):
    path = tmp_path / "just-exported.saz"
    login_archive(path, ["2026-10-06T18:58:43+08:00"])
    now = datetime(2026, 10, 6, 11, 11, 38, tzinfo=UTC).timestamp()
    report = inspect_authorization(path, now=now)
    assert report["age_seconds"] == 775
    assert report["latest_login_complete"]
    assert "private-test-code" not in json.dumps(report)
    assert "gate-obt" not in json.dumps(report)
    with pytest.raises(FarmError, match="刚导出文件不代表新登录"):
        require_fresh_authorization(report, 300)


def test_fresh_successful_login_is_accepted(tmp_path):
    path = tmp_path / "fresh.saz"
    login_archive(path, ["2026-10-06T18:58:43+08:00"])
    now = datetime(2026, 10, 6, 10, 59, 0, tzinfo=UTC).timestamp()
    report = inspect_authorization(path, now=now)
    assert report["age_seconds"] == 17
    require_fresh_authorization(report, 300)


def test_incomplete_latest_session_does_not_fall_back_to_old_authorization(tmp_path):
    path = tmp_path / "incomplete.saz"
    login_archive(path, ["2026-10-06T18:58:43+08:00", "incomplete"])
    report = inspect_authorization(path)
    assert report["session_count"] == 2
    assert not report["latest_login_complete"]
    with pytest.raises(FarmError, match="不会退回旧会话"):
        require_fresh_authorization(report, 300)


def test_missing_timestamp_is_not_inferred_from_file_modification_time(tmp_path):
    path = tmp_path / "missing-time.saz"
    login_archive(path, [None])
    report = inspect_authorization(path)
    assert report["age_seconds"] is None
    with pytest.raises(FarmError, match="缺少带时区"):
        require_fresh_authorization(report, 300)


def test_stale_inspection_does_not_create_an_authorization_file(tmp_path):
    path = tmp_path / "old.saz"
    login_archive(path, ["2020-01-01T00:00:00+00:00"])
    output = tmp_path / "authorization.json"
    output.write_text("existing-authorization")
    with pytest.raises(FarmError, match="超过导出时间窗口"):
        require_fresh_authorization(inspect_authorization(path), 300)
    assert output.read_text() == "existing-authorization"


@pytest.mark.parametrize("options", [{"reply_seq": 2}, {"reply_service": "other.Service"}])
def test_unmatched_login_reply_cannot_supply_freshness(tmp_path, options):
    path = tmp_path / "unmatched.saz"
    login_archive(path, [datetime.now(UTC).isoformat()], **options)
    details = inspect_authorization(path)
    assert not details["latest_login_complete"]
    assert details["captured_at"] is None
