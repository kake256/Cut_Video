"""Automatic canned replies with a fake YouTube client (no network, no OAuth)."""
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, youtube_auto_reply as reply

OWNER = "UC_owner"
# テスト1: 利用者が指定した実際の定型文（複数行）
TEMPLATE_1 = (
    "まだフォローしてない人はフォローしてくれると嬉しいです\n"
    "アマプラ入ってるとサブスクできるんでそっちの方もお願いします\n"
    "XもやってますDiscordサーバーもありますYoutubeチャンネルもねゲラサブロウチャンネルと"
    "ゲラサブロウ保管庫チャンネルがあるんで良かったらそっちの方もお願いします"
)
START = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def _thread(comment_id, *, author="UC_viewer", minutes_after=5, replies=(), total=None, can_reply=True):
    published = (START + timedelta(minutes=minutes_after)).isoformat().replace("+00:00", "Z")
    return {
        "snippet": {
            "videoId": "vid1", "canReply": can_reply,
            "totalReplyCount": len(replies) if total is None else total,
            "topLevelComment": {"id": comment_id, "snippet": {
                "authorChannelId": {"value": author}, "authorDisplayName": "@viewer",
                "textDisplay": "楽しかった", "publishedAt": published,
            }},
        },
        "replies": {"comments": [{"snippet": {"authorChannelId": {"value": a}}} for a in replies]},
    }


class _Service:
    def __init__(self, threads, *, all_replies=None, insert_error=None):
        self.threads = threads
        self.all_replies = all_replies or {}
        self.insert_error = insert_error
        self.inserted = []

    def channels(self):
        return self

    def commentThreads(self):
        return self

    def comments(self):
        return self

    def list(self, **kwargs):
        if kwargs.get("mine"):
            return _Exec({"items": [{"id": OWNER}]})
        if "allThreadsRelatedToChannelId" in kwargs:
            self.thread_query = kwargs
            return _Exec({"items": self.threads})
        authors = self.all_replies.get(kwargs["parentId"], [])
        return _Exec({"items": [{"snippet": {"authorChannelId": {"value": a}}} for a in authors]})

    def list_next(self, request, response):
        return None

    def insert(self, part, body):
        if self.insert_error:
            return _Exec(self.insert_error)
        self.inserted.append(body["snippet"])
        return _Exec({"id": f"reply{len(self.inserted)}"})


class AutoReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "LIBRARY_ROOT", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, *, enabled=True, limit=3, templates=("ありがとう！", "見てくれてありがとう {name}さん"),
                  include_past=False):
        reply.save_settings(enabled, templates, limit, 30, include_past=include_past, now=START)

    def run_at(self, service, minutes, **kwargs):
        return reply.run_once(service_factory=lambda: service, now=START + timedelta(minutes=minutes),
                              rng=random.Random(0), **kwargs)

    def test_1_multiline_template_is_added_and_sent_unchanged(self):
        reply.add_template(TEMPLATE_1, now=START)
        self.assertEqual(reply.load_settings().templates, [TEMPLATE_1])
        reply.save_settings(True, reply.load_settings().templates, 3, 30)
        service = _Service([_thread("c1")])
        self.assertEqual(self.run_at(service, 20).replied, 1)
        self.assertEqual(service.inserted[0], {"parentId": "c1", "textOriginal": TEMPLATE_1})
        self.assertLessEqual(len(TEMPLATE_1), reply.MAX_REPLY_CHARS)

    def test_replies_once_and_never_twice_to_the_same_comment(self):
        self.configure()
        service = _Service([_thread("c1")])
        self.assertEqual(self.run_at(service, 20).replied, 1)
        self.assertEqual(service.inserted[0]["parentId"], "c1")
        self.assertEqual(service.thread_query["allThreadsRelatedToChannelId"], OWNER)
        self.assertEqual(self.run_at(service, 60).replied, 0)
        self.assertEqual(len(service.inserted), 1)

    def test_skips_own_comments_threads_already_answered_and_old_comments(self):
        self.configure()
        threads = [
            _thread("own", author=OWNER),
            _thread("answered", replies=[OWNER]),
            # Owner's reply is only visible after fetching the full reply list.
            _thread("answered_hidden", replies=["UC_x"], total=7),
            _thread("closed", can_reply=False),
            _thread("old", minutes_after=-10),
        ]
        service = _Service(threads, all_replies={"answered_hidden": ["UC_x", OWNER]})
        result = self.run_at(service, 20)
        self.assertEqual(result.replied, 0)
        self.assertEqual(service.inserted, [])

    def test_past_unanswered_comments_are_answered_oldest_first_by_default(self):
        self.assertTrue(reply.Settings().include_past)
        reply.save_settings(True, ["ありがとう！"], 3, 30, now=START)
        self.assertTrue(reply.load_settings().include_past)
        threads = [  # API order: newest first
            _thread("new", minutes_after=5),
            _thread("old_answered", minutes_after=-3000, replies=[OWNER]),
            _thread("old", minutes_after=-2000),
            _thread("oldest_own", minutes_after=-9000, author=OWNER),
        ]
        service = _Service(threads)
        self.assertEqual(self.run_at(service, 20).replied, 1)
        self.assertEqual(self.run_at(service, 40).replied, 1)
        self.assertEqual(self.run_at(service, 60).replied, 0)
        self.assertEqual([c["parentId"] for c in service.inserted], ["old", "new"])

    def test_past_comments_can_be_switched_off_and_the_choice_is_kept(self):
        self.configure(include_past=True)
        reply.save_settings(True, ["x"], 3, 30, include_past=False)
        reply.add_template("y")
        self.assertFalse(reply.load_settings().include_past)
        service = _Service([_thread("new", minutes_after=5), _thread("old", minutes_after=-2000)])
        self.assertEqual(self.run_at(service, 20).replied, 1)
        self.assertEqual(self.run_at(service, 40).replied, 0)
        self.assertEqual([c["parentId"] for c in service.inserted], ["new"])

    def test_daily_limit_one_reply_per_run_and_minimum_gap(self):
        self.configure(limit=2)
        service = _Service([_thread(f"c{i}", minutes_after=i) for i in range(1, 6)])
        self.assertEqual(self.run_at(service, 20).replied, 1)  # one per run
        self.assertEqual(self.run_at(service, 25).replied, 0)  # within the minimum gap
        self.assertEqual(self.run_at(service, 40).replied, 1)
        result = self.run_at(service, 80)
        self.assertEqual(result.replied, 0)                    # daily limit reached
        self.assertIn("上限", result.message)
        self.assertEqual(reply.replies_today(START + timedelta(minutes=80)), 2)
        self.assertEqual(self.run_at(service, 24 * 60 + 60).replied, 1)  # next day

    def test_disabled_only_runs_manually_and_templates_are_required(self):
        self.configure(enabled=False)
        service = _Service([_thread("c1")])
        self.assertEqual(self.run_at(service, 20).replied, 0)
        self.assertEqual(self.run_at(service, 20, manual=True).replied, 1)
        with self.assertRaises(reply.AutoReplyError):
            reply.save_settings(True, ["  \n"], 3, 30)

    def test_failed_insert_is_logged_and_not_retried(self):
        self.configure()
        service = _Service([_thread("c1")], insert_error=RuntimeError("commentsDisabled"))
        self.assertEqual(self.run_at(service, 20).replied, 0)
        service.insert_error = None
        self.assertEqual(self.run_at(service, 40).replied, 0)
        self.assertEqual(reply.recent_log()[0]["status"], "error")
        self.assertEqual(reply.replies_today(START), 0)

    def test_template_rotation_and_name_placeholder(self):
        self.assertEqual(reply.choose_template(["a", "b"], last="a"), "b")
        self.assertEqual(reply.choose_template(["a"], last="a"), "a")
        self.configure(templates=["{name}さん\r\n見てくれてありがとう！\n\nまた来てね"])
        service = _Service([_thread("c1")])
        self.run_at(service, 20)
        self.assertEqual(service.inserted[0]["textOriginal"], "viewerさん\n見てくれてありがとう！\n\nまた来てね")

    def test_templates_are_added_one_by_one_and_removed(self):
        reply.add_template("1行目\n2行目", now=START)
        reply.add_template("ありがとう！")
        self.assertEqual(reply.load_settings().templates, ["1行目\n2行目", "ありがとう！"])
        with self.assertRaises(reply.AutoReplyError):
            reply.add_template("ありがとう！")
        with self.assertRaises(reply.AutoReplyError):
            reply.add_template("  \n ")
        reply.save_settings(True, reply.load_settings().templates, 3, 30)
        reply.remove_template("0")
        self.assertEqual(reply.load_settings().templates, ["ありがとう！"])
        with self.assertRaises(reply.AutoReplyError):
            reply.remove_template("5")
        settings = reply.remove_template(0)
        self.assertEqual(settings.templates, [])
        self.assertFalse(settings.enabled)  # nothing left to send

    def test_since_is_kept_and_limits_are_clamped(self):
        self.configure(limit=999)
        settings = reply.save_settings(True, ["x"], 0, 1, now=START + timedelta(days=3))
        self.assertEqual(reply.parse_time(settings.since), START)
        self.assertEqual(settings.daily_limit, 1)
        self.assertEqual(settings.interval_minutes, reply.MIN_INTERVAL_MINUTES)
        self.assertEqual(reply.load_settings().daily_limit, 1)

    def test_background_tick_needs_enabled_and_login(self):
        calls = []
        replier = reply.BackgroundReplier(runner=lambda: calls.append(1) or reply.RunResult(0, "ok"))
        self.configure()
        replier.tick()
        self.assertEqual(calls, [])  # not linked
        reply.token_path().write_text("{}", encoding="utf-8")
        replier.tick()
        self.assertEqual(calls, [1])
        self.assertEqual(replier.last_message, "ok")


class AutoReplyTabTest(unittest.TestCase):
    def test_tab_handlers_work_without_login(self):
        import auto_publish_app

        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "LIBRARY_ROOT", Path(tmp)):
            self.assertIn("未連携", auto_publish_app.reply_status())
            self.assertIn("まだ", auto_publish_app.reply_log_markdown())
            self.assertIn("定型文", auto_publish_app.reply_save_settings(True, True, 3, 30))
            status, cleared, listing, _choices = auto_publish_app.reply_add_template("ありがとう\nまたね")
            self.assertEqual(cleared, "")
            self.assertIn("ありがとう<br>またね", listing)
            self.assertIn("有効", auto_publish_app.reply_save_settings(True, False, 3, 30))
            self.assertEqual(auto_publish_app.reply_load_settings(), (True, False, 3, 30))


if __name__ == "__main__":
    unittest.main()
