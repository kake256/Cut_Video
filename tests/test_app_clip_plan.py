import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gradio as gr

from moment_retrieval import utils
import app as app_module
from app import (
    _APP_CSS,
    _INTUITIVE_COLLAPSE_VIDEO_PICKER_JS,
    _INTUITIVE_EDITOR_JS,
    _INTUITIVE_INITIAL_VIDEO_IDS,
    _QUIT_JS,
    _clip_plan_exclusions,
    _clip_plan_ranges,
    _intuitive_preview_path,
    _load_intuitive_search_result,
    _new_intuitive_state,
    adjust_exclusion_time,
    adjust_exclusion_time_with_step,
    always_refresh,
    build_video_gallery,
    build_intuitive_video_gallery,
    do_search,
    dispatch_intuitive_command,
    handle_intuitive_command,
    exclude_clip_range,
    list_video_choices,
    load_intuitive_video,
    intuitive_state_to_clip_plan,
    on_intuitive_search_select,
    parse_video_choice,
    preview_intuitive_editor,
    remove_clip_exclusion,
    render_clip_plan_timeline,
    render_intuitive_exclusion_list,
    render_intuitive_summary,
    render_intuitive_toolbar,
    render_intuitive_transcript,
    render_intuitive_video_cards,
    render_intuitive_state_overview,
    render_intuitive_state_zoom,
    refresh_intuitive_video_picker,
    reset_clip_plan,
    reset_clip_plan_after_range_change,
    select_clip_exclusion,
    select_intuitive_video_from_gallery,
    select_video_from_gallery,
    sync_exclusion_controls,
    selected_video_info,
    save_intuitive_editor,
    return_intuitive_source,
    demo,
)


class ClipPlanTest(unittest.TestCase):
    @staticmethod
    def _intuitive_state():
        return _new_intuitive_state(
            {
                "video_id": "video-1",
                "path": r"F:\videos\sample.mp4",
                "duration": 200.0,
            },
            10.0,
            100.0,
        )

    @staticmethod
    def _intuitive_command(state, command_type, **values):
        return dispatch_intuitive_command({
            "type": command_type,
            "revision": state["revision"],
            "nonce": state["nonce"],
            **values,
        }, state)

    def test_video_choices_show_filename_while_returning_internal_id(self):
        class FakeConnection:
            def close(self):
                pass

        video = {
            "video_id": "opaque-internal-id",
            "path": r"F:\videos\20260705_130555_wIhYikXPQbs.mp4",
            "duration": 125.0,
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.init_db"),
            patch("app.db.list_videos", return_value=[video]),
        ):
            choices = list_video_choices()

        self.assertEqual(choices[0], ("すべての動画", "__all_videos__"))
        self.assertEqual(choices[1][1], "opaque-internal-id")
        self.assertTrue(choices[1][0].startswith("20260705_130555_wIhYikXPQbs.mp4"))
        self.assertNotIn("opaque-internal-id", choices[1][0])
        self.assertEqual(parse_video_choice(choices[1][1]), "opaque-internal-id")

    def test_selected_video_info_includes_thumbnail_name_and_duration(self):
        class FakeConnection:
            def close(self):
                pass

        video = {
            "video_id": "video-1",
            "path": r"F:\videos\sample.mp4",
            "duration": 125.0,
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.get_video", return_value=video),
            patch("app._make_video_thumbnail", return_value=r"F:\thumb.jpg"),
        ):
            image_update, detail = selected_video_info("video-1")

        self.assertEqual(image_update["value"], r"F:\thumb.jpg")
        self.assertTrue(image_update["visible"])
        self.assertIn("sample.mp4", detail)
        self.assertIn("00:02:05", detail)

    def test_video_gallery_maps_visible_cards_to_internal_ids(self):
        class FakeConnection:
            def close(self):
                pass

        videos = [
            {"video_id": "video-a", "path": r"F:\videos\alpha.mp4", "duration": 60.0},
            {"video_id": "video-b", "path": r"F:\videos\beta.mp4", "duration": 90.0},
        ]
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.init_db"),
            patch("app.db.list_videos", return_value=videos),
            patch("app._make_video_thumbnail", side_effect=["alpha.jpg", "beta.jpg"]),
        ):
            gallery_update, ids = build_video_gallery("beta", "video-b")

        self.assertEqual(ids, ["__all_videos__", "video-b"])
        self.assertEqual(gallery_update["selected_index"], 1)
        self.assertIn("beta.mp4", gallery_update["value"][1][1])

    def test_video_gallery_selection_updates_search_target(self):
        event = gr.SelectData(None, {"index": 1, "value": None, "selected": True})
        with patch(
            "app.selected_video_info",
            return_value=(gr.update(value="thumb.jpg", visible=True), "video detail"),
        ):
            video_id, image_update, detail, gallery_update = select_video_from_gallery(
                ["__all_videos__", "video-a"], event
            )

        self.assertEqual(video_id, "video-a")
        self.assertEqual(image_update["value"], "thumb.jpg")
        self.assertEqual(detail, "video detail")
        self.assertEqual(gallery_update["selected_index"], 1)

    def test_intuitive_video_gallery_excludes_all_and_filters_caption(self):
        class FakeConnection:
            def close(self):
                pass

        videos = [
            {"video_id": "video-a", "path": r"F:\videos\alpha.mp4", "duration": 60.0},
            {"video_id": "video-b", "path": r"F:\videos\beta.mp4", "duration": 90.0},
        ]
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.init_db"),
            patch("app.db.list_videos", return_value=videos),
            patch("app.db.get_indexed_video_ids", return_value={"video-b"}),
            patch("app._make_video_thumbnail", return_value="thumb.jpg") as thumbnail,
            patch("app._thumbnail_servable_url", return_value="thumb-url"),
        ):
            gallery_update, ids, cards_html = build_intuitive_video_gallery(
                "beta", "video-b"
            )

        self.assertEqual(ids, ["video-b"])
        self.assertNotIn("__all_videos__", ids)
        self.assertEqual(gallery_update["selected_index"], 0)
        self.assertIn("beta.mp4", gallery_update["value"][0][1])
        self.assertIn("00:01:30", gallery_update["value"][0][1])
        thumbnail.assert_called_once_with(videos[1])
        self.assertIn("beta.mp4", cards_html)
        self.assertNotIn("alpha.mp4", cards_html)
        self.assertIn("索引あり", cards_html)
        self.assertNotIn("文字起こし済み", cards_html)

    def test_render_intuitive_video_cards_shows_badges_only_when_judgable(self):
        cards = [
            {
                "video_id": "dummy-1",
                "name": "dummy-clip-a.mp4",
                "duration": 75.0,
                "thumbnail_url": "/gradio_api/file=/dummy/thumb-a.jpg",
                "asr_complete": True,
                "indexed": True,
            },
            {
                "video_id": "dummy-2",
                "name": "dummy-clip-b.mp4",
                "duration": 5.0,
                "thumbnail_url": "/gradio_api/file=/dummy/thumb-b.jpg",
                "asr_complete": False,
                "indexed": False,
            },
        ]

        rendered = render_intuitive_video_cards(cards)

        self.assertEqual(rendered.count('class="intuitive-video-badge"'), 2)
        self.assertEqual(rendered.count("文字起こし済み"), 1)
        self.assertEqual(rendered.count("索引あり"), 1)
        self.assertIn('data-index="0"', rendered)
        self.assertIn('data-index="1"', rendered)
        self.assertIn(utils.format_timestamp(75.0), rendered)

    def test_render_intuitive_video_cards_escapes_name_and_marks_selection(self):
        cards = [{
            "video_id": "dummy-<3>",
            "name": "<script>alert(1)</script>.mp4",
            "duration": 12.0,
            "thumbnail_url": "/gradio_api/file=/dummy/thumb.jpg",
            "asr_complete": False,
            "indexed": False,
        }]

        rendered = render_intuitive_video_cards(cards, selected_video_id="dummy-<3>")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("is-selected", rendered)

    def test_render_intuitive_video_cards_empty_state(self):
        self.assertIn(
            "該当する動画がありません", render_intuitive_video_cards([])
        )

    def test_intuitive_gallery_selection_loads_video_with_single_output_tuple(self):
        event = gr.SelectData(None, {"index": 1, "value": None, "selected": True})
        load_outputs = tuple(f"load-{index}" for index in range(10))
        with (
            patch("app.load_intuitive_editor", return_value=load_outputs) as load,
            patch(
                "app.build_intuitive_video_gallery",
                return_value=(gr.update(), ["video-a", "video-b"], "<div>cards</div>"),
            ) as build_gallery,
        ):
            output = select_intuitive_video_from_gallery(
                ["video-a", "video-b"], "", event
            )

        self.assertEqual(len(output), 13)
        self.assertEqual(output[0]["selected_index"], 1)
        self.assertEqual(output[1], "video-b")
        self.assertEqual(output[2], "<div>cards</div>")
        self.assertEqual(output[3:], load_outputs)
        load.assert_called_once_with("video-b")
        build_gallery.assert_called_once_with("", "video-b")

        with self.assertRaises(gr.Error):
            select_intuitive_video_from_gallery(["__all_videos__"], "", gr.SelectData(
                None, {"index": 0, "value": None, "selected": True}
            ))

    def test_intuitive_picker_refresh_keeps_all_only_in_search_target(self):
        gallery = gr.update(
            value=[("thumb.jpg", "sample.mp4\n00:01:00")], selected_index=0
        )
        with patch(
            "app.build_intuitive_video_gallery",
            return_value=(gallery, ["video-a"], "<div>cards</div>"),
        ):
            output = refresh_intuitive_video_picker("sample", "video-a")

        self.assertEqual(output[2], "<div>cards</div>")
        fallback_choices = output[3]["choices"]
        search_choices = output[4]["choices"]
        self.assertEqual(fallback_choices, [("sample.mp4  —  00:01:00", "video-a")])
        self.assertEqual(search_choices[0], ("すべての動画", "__all_videos__"))
        self.assertEqual(search_choices[1:], fallback_choices)

    def test_middle_exclusion_splits_selection(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)

        plan, table, summary = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        self.assertEqual(plan["ranges"], [[0.0, 20.0], [30.0, 60.0]])
        self.assertEqual([row[3] for row in table], [10.0])
        self.assertEqual(table[0][0], "○ 1")
        self.assertEqual(table[0][1:3], ["00:00:20.00", "00:00:30.00"])
        self.assertIn("途中カット:** 1箇所 / 10.0秒", summary)
        self.assertIn("50.0秒", summary)

        timeline = render_clip_plan_timeline(0.0, 60.0, plan)
        self.assertIn("clip-timeline-cut", timeline)
        self.assertIn("left:33.3333%;width:16.6667%", timeline)
        self.assertIn("斜線: 除外", timeline)

    def test_multiple_exclusions_create_multiple_kept_windows(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 10.0, 20.0, plan)

        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 45.0, plan)

        self.assertEqual(plan["ranges"], [[0.0, 10.0], [20.0, 40.0], [45.0, 60.0]])

    def test_changed_outer_selection_discards_stale_plan(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        self.assertEqual(_clip_plan_ranges(5.0, 55.0, plan), [[5.0, 55.0]])

    def test_ui_exclusions_are_derived_from_internal_kept_ranges(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 10.0, 20.0, plan)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 45.0, plan)

        self.assertEqual(
            _clip_plan_exclusions(0.0, 60.0, plan),
            [[10.0, 20.0], [40.0, 45.0]],
        )

    def test_overlapping_exclusion_is_noop_or_merged(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        unchanged, table, _ = exclude_clip_range(
            0.0, 60.0, 22.0, 28.0, plan
        )
        self.assertEqual(unchanged["ranges"], plan["ranges"])
        self.assertEqual([row[1:3] for row in table], [[
            "00:00:20.00", "00:00:30.00"
        ]])

        extended, _, _ = exclude_clip_range(
            0.0, 60.0, 25.0, 35.0, plan
        )
        self.assertEqual(
            _clip_plan_exclusions(0.0, 60.0, extended),
            [[20.0, 35.0]],
        )

        adjacent_plan, _, _ = exclude_clip_range(
            0.0, 60.0, 30.0, 35.0, plan
        )
        self.assertEqual(
            _clip_plan_exclusions(0.0, 60.0, adjacent_plan),
            [[20.0, 35.0]],
        )

    def test_exclusion_can_merge_across_multiple_existing_cuts(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 50.0, plan)

        merged, _, _ = exclude_clip_range(0.0, 60.0, 25.0, 45.0, plan)

        self.assertEqual(
            _clip_plan_exclusions(0.0, 60.0, merged),
            [[20.0, 50.0]],
        )

    def test_selecting_exclusion_redraws_marker(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 10.0, 20.0, plan)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 45.0, plan)

        selected, table, _ = select_clip_exclusion(
            0.0, 60.0, plan, SimpleNamespace(index=(1, 2))
        )

        self.assertEqual(selected, 1)
        self.assertEqual([row[0] for row in table], ["○ 1", "● 2"])

    def test_remove_selected_exclusion_restores_that_window(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 10.0, 20.0, plan)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 40.0, 45.0, plan)

        plan, table, summary, selected = remove_clip_exclusion(
            0.0, 60.0, 0, plan
        )

        self.assertEqual(plan["ranges"], [[0.0, 40.0], [45.0, 60.0]])
        self.assertEqual(
            [row[1:3] for row in table],
            [["00:00:40.00", "00:00:45.00"]],
        )
        self.assertIn("完成予定:** 55.0秒", summary)
        self.assertIsNone(selected)

    def test_outer_range_change_explicitly_resets_exclusions(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)
        plan, _, _ = exclude_clip_range(0.0, 60.0, 20.0, 30.0, plan)

        new_plan, table, summary, selected = reset_clip_plan_after_range_change(
            5.0, 55.0, plan
        )

        self.assertEqual(new_plan["ranges"], [[5.0, 55.0]])
        self.assertEqual(table, [])
        self.assertIn("途中カットをリセットしました", summary)
        self.assertIsNone(selected)

    def test_exclusion_adjustment_is_clamped_to_outer_range(self):
        self.assertEqual(adjust_exclusion_time(10.0, 10.0, 20.0, -1.0), 10.0)
        self.assertEqual(adjust_exclusion_time(19.9, 10.0, 20.0, 1.0), 20.0)
        self.assertEqual(
            adjust_exclusion_time_with_step(15.0, 10.0, 20.0, 0.1, -1.0),
            14.9,
        )

    def test_exclusion_controls_follow_outer_range(self):
        start_slider, end_slider, start_number, end_number = (
            sync_exclusion_controls(12.5, 42.0)
        )

        self.assertEqual(start_slider["minimum"], 12.5)
        self.assertEqual(start_slider["maximum"], 42.0)
        self.assertEqual(start_slider["value"], 12.5)
        self.assertEqual(end_slider["value"], 42.0)
        self.assertEqual(start_number["value"], 12.5)
        self.assertEqual(end_number["value"], 42.0)

    def test_intuitive_transcript_escapes_words_and_falls_back_to_segments(self):
        segments = [
            {
                "start_sec": 1.0,
                "end_sec": 2.0,
                "text": "word fallback",
                "words_json": '[{"word":"<script>alert(1)</script>","start":1.1,"end":1.5}]',
            },
            {
                "start_sec": 2.0,
                "end_sec": 3.0,
                "text": "<b>segment</b>",
                "words_json": "not-json",
            },
        ]

        rendered = render_intuitive_transcript(segments)

        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<b>segment</b>", rendered)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&lt;b&gt;segment&lt;/b&gt;", rendered)
        self.assertIn('data-start="1.100"', rendered)
        self.assertIn('data-time-granularity="word"', rendered)
        self.assertIn('data-time-granularity="segment"', rendered)
        self.assertIn("時刻指定: 単語単位＋発話区間単位", rendered)
        self.assertIn("intuitive-segment", rendered)

    def test_intuitive_transcript_never_invents_missing_word_timestamps(self):
        segments = [
            {
                "start_sec": 10.0,
                "end_sec": 12.0,
                "text": "時刻が不完全な発話",
                "words_json": (
                    '[{"word":"時刻あり","start":10.1,"end":10.5},'
                    '{"word":"時刻なし"}]'
                ),
            },
            {
                "start_sec": 12.0,
                "end_sec": 14.0,
                "text": "空の単語一覧",
                "words_json": "[]",
            },
        ]

        rendered = render_intuitive_transcript(segments)

        self.assertNotIn('data-time-granularity="word"', rendered)
        self.assertEqual(rendered.count('data-time-granularity="segment"'), 2)
        self.assertIn('data-start="10.000" data-end="12.000"', rendered)
        self.assertNotIn('data-start="10.100"', rendered)
        self.assertIn("時刻指定: 発話区間単位（単語時刻なし）", rendered)

    def test_intuitive_transcript_rejects_non_finite_word_timestamps(self):
        segments = [{
            "start_sec": 20.0,
            "end_sec": 22.0,
            "text": "invalid timestamp",
            "words_json": '[{"word":"bad","start":"NaN","end":21.0}]',
        }]

        rendered = render_intuitive_transcript(segments)

        self.assertIn('data-time-granularity="segment"', rendered)
        self.assertNotIn('data-time-granularity="word"', rendered)
        self.assertIn('data-start="20.000" data-end="22.000"', rendered)

    def test_intuitive_transcript_shows_row_start_time_chip(self):
        segments = [{
            "start_sec": 65.0,
            "end_sec": 70.0,
            "text": "発話ごとの行",
            "words_json": None,
        }]

        rendered = render_intuitive_transcript(segments)

        self.assertIn('class="intuitive-segment-row"', rendered)
        self.assertIn(
            f'<span class="intuitive-ts-chip">{utils.format_timestamp(65.0)}</span>',
            rendered,
        )

    def test_intuitive_transcript_omits_chip_when_start_time_missing(self):
        segments = [{
            "end_sec": 70.0,
            "text": "開始時刻なしの発話",
            "words_json": None,
        }]

        rendered = render_intuitive_transcript(segments)

        self.assertIn('class="intuitive-segment-row"', rendered)
        self.assertNotIn("intuitive-ts-chip", rendered)

    def test_intuitive_transcript_and_zoom_render_edit_state_classes(self):
        state = self._intuitive_state()
        state.update({
            "overall_start": 20.0,
            "overall_end": 30.0,
            "selected_word": {"start": 20.0, "end": 30.0},
            "pending_cut_start": 25.0,
            "selected_boundary": {"kind": "exclusion_end", "id": "cut-1"},
            "exclusions": [{"id": "cut-1", "start": 25.0, "end": 28.0}],
        })
        segments = [{
            "start_sec": 10.0,
            "end_sec": 40.0,
            "text": "",
            "words_json": (
                '[{"word":"before","start":10,"end":20},'
                '{"word":"inside","start":20,"end":30},'
                '{"word":"after","start":30,"end":40}]'
            ),
        }]

        rendered = render_intuitive_transcript(segments, state)

        self.assertEqual(rendered.count("marks-overall-start"), 1)
        self.assertEqual(rendered.count("marks-overall-end"), 1)
        self.assertIn("is-selected-word", rendered)
        self.assertIn("is-outside-overall", rendered)
        self.assertIn("is-excluded-word", rendered)
        self.assertIn("marks-pending-cut", rendered)
        self.assertIn("marks-exclusion-start", rendered)
        self.assertIn("marks-exclusion-end", rendered)

        zoom = render_intuitive_state_zoom(state)
        self.assertIn("intuitive-zoom-overall", zoom)
        self.assertIn("is-selected-cut", zoom)
        self.assertIn('data-overall-start="20.000"', zoom)
        self.assertIn('data-overall-end="30.000"', zoom)
        self.assertIn("全体範囲内をドラッグ", zoom)

    def test_intuitive_reducer_word_tools_adjustment_and_plan(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "set_tool", tool="overall_start"
        )
        state = self._intuitive_command(
            state, "set_from_word", start=20.0, end=21.0
        )
        self.assertEqual(state["overall_start"], 20.0)
        self.assertEqual(state["selected_boundary"], {"kind": "overall_start"})
        self.assertIsNone(state["active_tool"])

        state = self._intuitive_command(
            state, "adjust_selected", delta=1.0
        )
        self.assertEqual(state["overall_start"], 21.0)

        state = self._intuitive_command(
            state, "set_tool", tool="exclude_start"
        )
        state = self._intuitive_command(
            state, "set_from_word", start=30.0, end=31.0
        )
        self.assertEqual(state["active_tool"], "exclude_end")
        self.assertEqual(state["pending_cut_start"], 30.0)
        state = self._intuitive_command(
            state, "set_from_word", start=39.0, end=40.0
        )
        self.assertIsNone(state["active_tool"])
        self.assertEqual(len(state["exclusions"]), 1)
        self.assertEqual(
            intuitive_state_to_clip_plan(state)["ranges"],
            [[21.0, 30.0], [40.0, 100.0]],
        )

    def test_intuitive_word_selection_first_and_exclusion_sequence(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "set_from_word", start=20.0, end=21.0
        )
        self.assertEqual(state["selected_word"], {"start": 20.0, "end": 21.0})
        self.assertIsNone(state["active_tool"])
        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        self.assertEqual(state["overall_start"], 20.0)
        self.assertIsNone(state["selected_word"])
        self.assertIsNone(state["active_tool"])

        state = self._intuitive_command(
            state, "set_from_word", start=30.0, end=31.0
        )
        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        self.assertEqual(state["pending_cut_start"], 30.0)
        self.assertEqual(state["active_tool"], "exclude_end")
        self.assertIsNone(state["selected_word"])
        state = self._intuitive_command(
            state, "set_from_word", start=39.0, end=40.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(30.0, 40.0)],
        )
        self.assertIsNone(state["selected_word"])
        self.assertIsNone(state["active_tool"])

    def test_intuitive_timeline_tool_click_applies_exact_boundaries(self):
        state = self._intuitive_state()
        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        state = self._intuitive_command(state, "set_from_timeline", time=12.345)
        self.assertEqual(state["overall_start"], 12.345)
        self.assertEqual(state["selected_boundary"], {"kind": "overall_start"})
        self.assertIsNone(state["active_tool"])

        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        state = self._intuitive_command(state, "set_from_timeline", time=88.765)
        self.assertEqual(state["overall_end"], 88.765)
        self.assertEqual(state["selected_boundary"], {"kind": "overall_end"})

        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(state, "set_from_timeline", time=30.25)
        self.assertEqual(state["pending_cut_start"], 30.25)
        self.assertEqual(state["active_tool"], "exclude_end")
        state = self._intuitive_command(state, "set_from_timeline", time=42.75)
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(30.25, 42.75)],
        )
        self.assertIsNone(state["pending_cut_start"])
        self.assertIsNone(state["active_tool"])

    def test_intuitive_timeline_time_requires_selected_tool(self):
        state = self._intuitive_state()
        with self.assertRaises(gr.Error):
            self._intuitive_command(state, "set_from_timeline", time=20.0)

    def test_intuitive_summary_is_derived_from_canonical_state(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "add_exclusion", start=20.0, end=25.0
        )
        state = self._intuitive_command(
            state, "add_exclusion", start=40.0, end=50.0
        )

        rendered = render_intuitive_summary(state)

        self.assertIn("00:00:10", rendered)
        self.assertIn("00:01:40", rendered)
        self.assertIn("（90.0秒）", rendered)
        self.assertIn("2箇所 / 15.0秒", rendered)
        self.assertIn("完成予定</strong> 75.0秒", rendered)
        self.assertNotIn("summary", state)
        self.assertNotIn("completed_duration", state)

    def test_intuitive_dirty_tracks_only_canonical_edit_plan_changes(self):
        state = self._intuitive_state()
        self.assertFalse(state["edit_dirty"])

        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        self.assertFalse(state["edit_dirty"])
        state = self._intuitive_command(
            state, "set_viewport", start=20.0, end=80.0
        )
        self.assertFalse(state["edit_dirty"])
        state = self._intuitive_command(
            state, "set_transcript_focus", time=50.0
        )
        self.assertFalse(state["edit_dirty"])
        state = self._intuitive_command(
            state, "select_boundary", kind="overall_start"
        )
        self.assertFalse(state["edit_dirty"])

        state = self._intuitive_command(
            state, "set_selected_time", time=25.0
        )
        self.assertTrue(state["edit_dirty"])
        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        self.assertTrue(state["edit_dirty"])

        exclusion_state = self._intuitive_state()
        exclusion_state = self._intuitive_command(
            exclusion_state, "add_exclusion", start=20.0, end=25.0
        )
        self.assertTrue(exclusion_state["edit_dirty"])

        manually_seeded = self._intuitive_state()
        manually_seeded["exclusions"] = [
            {"id": "cut-1", "start": 20.0, "end": 25.0},
        ]
        manually_seeded = self._intuitive_command(
            manually_seeded, "remove_exclusion", id="cut-1"
        )
        self.assertTrue(manually_seeded["edit_dirty"])

    def test_timeline_edit_mode_defaults_off_and_toggles_via_fifo(self):
        state = self._intuitive_state()
        self.assertFalse(state["timeline_edit_mode"])

        zoom_off = render_intuitive_state_zoom(state)
        self.assertIn('data-timeline-edit-mode="false"', zoom_off)
        self.assertIn('data-intuitive-toggle-edit-mode', zoom_off)
        self.assertIn('編集モード: OFF', zoom_off)
        self.assertNotIn('is-on', zoom_off)
        toolbar_off = render_intuitive_toolbar(state)
        self.assertIn('data-timeline-edit-mode="false"', toolbar_off)

        state = self._intuitive_command(state, "set_timeline_edit_mode", enabled=True)
        self.assertTrue(state["timeline_edit_mode"])
        zoom_on = render_intuitive_state_zoom(state)
        self.assertIn('data-timeline-edit-mode="true"', zoom_on)
        self.assertIn('編集モード: ON', zoom_on)
        self.assertIn('intuitive-edit-mode-toggle is-on', zoom_on)

        state = self._intuitive_command(state, "set_timeline_edit_mode", enabled=False)
        self.assertFalse(state["timeline_edit_mode"])

    def test_timeline_edit_mode_off_clears_armed_tool_and_pending_cut(self):
        state = self._intuitive_state()
        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(
            state, "set_from_word", start=40.0, end=40.5
        )
        self.assertEqual(state["active_tool"], "exclude_end")
        self.assertIsNotNone(state["pending_cut_start"])

        state = self._intuitive_command(
            state, "set_timeline_edit_mode", enabled=False
        )
        self.assertFalse(state["timeline_edit_mode"])
        self.assertIsNone(state["active_tool"])
        self.assertIsNone(state["pending_cut_start"])

    def test_set_tool_from_the_timeline_toolbox_auto_enables_edit_mode(self):
        """Bug 1 regression: pressing a tool button that lives inside the
        zoom timeline's own toolbox is itself the "I want to edit" gesture,
        so it must also flip timeline_edit_mode on in the same round trip --
        otherwise the very next click (to place the boundary) silently falls
        back to a seek instead of setting it."""
        state = self._intuitive_state()
        self.assertFalse(state["timeline_edit_mode"])

        state = self._intuitive_command(
            state, "set_tool", tool="overall_start",
            enable_timeline_edit_mode=True,
        )
        self.assertTrue(state["timeline_edit_mode"])
        self.assertEqual(state["active_tool"], "overall_start")

        # A plain set_tool (e.g. from the transcript panel's own toolbox,
        # which never sends this flag) must not touch edit mode either way.
        state["timeline_edit_mode"] = False
        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        self.assertFalse(state["timeline_edit_mode"])

    def test_timeline_tool_buttons_stay_clickable_and_hint_when_edit_mode_off(self):
        """Bug 1: unlike handles/cut-drag, the timeline toolbox's tool
        buttons must never get `pointer-events: none` -- that is what made
        them look clickable but do nothing when edit mode was off."""
        state = self._intuitive_state()
        zoom_off = render_intuitive_state_zoom(state)
        self.assertIn(
            "クリックで編集モードをONにして選択します。", zoom_off
        )
        # Only the dim opacity rule may apply to tool buttons while off;
        # pointer-events:none must be reserved for handles/cut-handles.
        off_rule = (
            '[data-intuitive-zoom][data-timeline-edit-mode="false"] '
            '.intuitive-timeline-toolbox .intuitive-tool-button {\n'
            '  opacity: .65;\n}'
        )
        self.assertIn(off_rule, _APP_CSS)

        state["timeline_edit_mode"] = True
        zoom_on = render_intuitive_state_zoom(state)
        self.assertNotIn(
            "クリックで編集モードをONにして選択します。", zoom_on
        )

    def test_overview_label_clarifies_viewport_does_not_change_save_range(self):
        state = self._intuitive_state()
        overview = render_intuitive_state_overview(state)
        self.assertIn("表示範囲（保存範囲は変わりません）", overview)

        zoom = render_intuitive_state_zoom(state)
        self.assertIn("data-intuitive-fit-overall", zoom)
        self.assertIn("保存範囲を表示範囲に合わせる", zoom)

    def test_fit_overall_to_viewport_sets_save_range_to_current_display_range(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "set_viewport", start=30.0, end=70.0
        )
        self.assertEqual((state["overall_start"], state["overall_end"]), (10.0, 100.0))

        state = self._intuitive_command(
            state, "fit_overall_to_viewport", enable_timeline_edit_mode=True,
        )

        self.assertEqual((state["overall_start"], state["overall_end"]), (30.0, 70.0))
        self.assertTrue(state["timeline_edit_mode"])

    def test_fit_overall_to_viewport_clamps_to_duration_bounds(self):
        state = self._intuitive_state()  # duration=200.0
        state = self._intuitive_command(
            state, "set_viewport", start=-50.0, end=300.0
        )
        self.assertEqual(
            (state["viewport_start"], state["viewport_end"]), (0.0, 200.0)
        )

        state = self._intuitive_command(state, "fit_overall_to_viewport")

        self.assertEqual((state["overall_start"], state["overall_end"]), (0.0, 200.0))

    def test_fit_overall_to_viewport_clips_exclusions_and_remaps_selection(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "add_exclusion", start=15.0, end=18.0
        )
        state = self._intuitive_command(
            state, "add_exclusion", start=60.0, end=65.0
        )
        cut_id_outside = state["exclusions"][0]["id"]
        cut_id_inside = state["exclusions"][1]["id"]
        state = self._intuitive_command(
            state, "select_boundary", kind="exclusion_start", id=cut_id_outside
        )

        # Shrink the viewport so it no longer covers the first cut (15-18)
        # but still covers the second (60-65).
        state = self._intuitive_command(
            state, "set_viewport", start=30.0, end=90.0
        )
        state = self._intuitive_command(state, "fit_overall_to_viewport")

        self.assertEqual((state["overall_start"], state["overall_end"]), (30.0, 90.0))
        remaining_ids = {cut["id"] for cut in state["exclusions"]}
        self.assertNotIn(cut_id_outside, remaining_ids)
        self.assertIn(cut_id_inside, remaining_ids)
        # The boundary selection pointed at the now-clipped-away cut, so it
        # must be safely dropped (existing _remap_intuitive_boundary rule),
        # not left dangling on a nonexistent id.
        self.assertIsNone(state["selected_boundary"])

    def test_fit_overall_to_viewport_notifies_the_user(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "set_viewport", start=20.0, end=80.0
        )
        command = {
            "type": "fit_overall_to_viewport",
            "revision": state["revision"], "nonce": state["nonce"],
        }
        with patch("app.gr.Info") as info:
            outputs = handle_intuitive_command(command, state)
        recovered = outputs[0]
        self.assertEqual((recovered["overall_start"], recovered["overall_end"]), (20.0, 80.0))
        info.assert_called_once()
        self.assertIn("00:00:20", info.call_args.args[0])
        self.assertIn("00:01:20", info.call_args.args[0])

    def test_adjust_selected_works_for_every_boundary_kind(self):
        """Bug 2 regression: 前へ/後ろへ must move overall_start, overall_end,
        pending_cut_start, and both ends of an existing exclusion -- not just
        overall_start."""
        state = self._intuitive_state()

        state = self._intuitive_command(
            state, "set_tool", tool="overall_start", enable_timeline_edit_mode=True
        )
        state = self._intuitive_command(state, "set_from_word", start=20.0, end=20.5)
        state = self._intuitive_command(state, "adjust_selected", delta=1.0)
        self.assertEqual(state["overall_start"], 21.0)

        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        state = self._intuitive_command(state, "set_from_word", start=90.0, end=90.5)
        state = self._intuitive_command(state, "adjust_selected", delta=-1.0)
        self.assertEqual(state["overall_end"], 89.5)

        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(state, "set_from_word", start=40.0, end=40.5)
        self.assertEqual(state["selected_boundary"], {"kind": "pending_cut_start"})
        state = self._intuitive_command(state, "adjust_selected", delta=1.0)
        self.assertEqual(state["pending_cut_start"], 41.0)

        state = self._intuitive_command(state, "set_from_word", start=50.0, end=50.5)
        cut_id = state["exclusions"][0]["id"]
        self.assertEqual(state["selected_boundary"], {"kind": "exclusion_end", "id": cut_id})
        state = self._intuitive_command(state, "adjust_selected", delta=1.0)
        self.assertEqual(state["exclusions"][0]["end"], 51.5)

        state = self._intuitive_command(
            state, "select_boundary", kind="exclusion_start", id=cut_id
        )
        state = self._intuitive_command(state, "adjust_selected", delta=1.0)
        self.assertEqual(state["exclusions"][0]["start"], 42.0)

    def test_intuitive_exclusion_list_renders_compact_safe_delete_controls(self):
        state = self._intuitive_state()
        state["exclusions"] = [
            {"id": 'cut-<unsafe>"', "start": 20.0, "end": 25.5},
        ]
        state["selected_boundary"] = {
            "kind": "exclusion_end", "id": 'cut-<unsafe>"',
        }

        rendered = render_intuitive_exclusion_list(state)

        self.assertIn("途中カット一覧（1箇所）", rendered)
        self.assertIn("00:00:20", rendered)
        self.assertIn("5.5秒", rendered)
        self.assertIn("data-intuitive-remove-exclusion", rendered)
        self.assertIn("cut-&lt;unsafe&gt;&quot;", rendered)
        self.assertIn("is-selected", rendered)
        self.assertIn("data-intuitive-clear-exclusions", rendered)

        state["preview_mode"] = "result"
        rendered = render_intuitive_exclusion_list(state)
        self.assertEqual(rendered.count(" disabled"), 2)

    def test_intuitive_remove_and_clear_exclusions_keep_selection_safe(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "add_exclusion", start=20.0, end=25.0
        )
        first_id = state["exclusions"][0]["id"]
        state = self._intuitive_command(
            state, "add_exclusion", start=40.0, end=45.0
        )
        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(
            state, "set_from_timeline", time=60.0
        )
        self.assertEqual(state["active_tool"], "exclude_end")

        state = self._intuitive_command(
            state, "remove_exclusion", id=first_id
        )
        self.assertEqual(len(state["exclusions"]), 1)
        self.assertIsNone(state["pending_cut_start"])
        self.assertIsNone(state["active_tool"])
        self.assertIsNone(state["selected_boundary"])

        remaining_id = state["exclusions"][0]["id"]
        state = self._intuitive_command(
            state, "select_boundary", kind="exclusion_end", id=remaining_id
        )
        state = self._intuitive_command(state, "clear_exclusions")
        self.assertEqual(state["exclusions"], [])
        self.assertIsNone(state["selected_boundary"])

    def test_intuitive_remove_does_not_regenerate_video_preview(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "add_exclusion", start=20.0, end=25.0
        )
        command = {
            "type": "remove_exclusion", "id": state["exclusions"][0]["id"],
            "revision": state["revision"], "nonce": state["nonce"],
        }
        with (
            patch("app._render_intuitive_transcript_for_state", return_value="transcript"),
            patch("app.make_intuitive_preview") as preview,
        ):
            output = handle_intuitive_command(command, state)

        preview.assert_not_called()
        self.assertEqual(output[0]["exclusions"], [])
        self.assertIn("途中カット</strong> 0箇所 / 0.0秒", output[7])
        self.assertIn("途中カット一覧（0箇所）", output[8])

    def test_intuitive_current_position_and_direct_time_use_existing_constraints(self):
        unarmed = self._intuitive_state()
        with self.assertRaises(gr.Error):
            self._intuitive_command(
                unarmed, "set_current_position", time=20.0
            )
        unarmed = self._intuitive_command(
            unarmed, "set_tool", tool="overall_start"
        )
        with self.assertRaises(gr.Error):
            self._intuitive_command(
                unarmed, "set_current_position", time=150.0
            )

        state = self._intuitive_state()
        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        state = self._intuitive_command(
            state, "set_current_position", time=22.25
        )
        self.assertEqual(state["overall_start"], 22.25)
        self.assertEqual(state["selected_boundary"], {"kind": "overall_start"})

        state = self._intuitive_command(
            state, "set_selected_time", time=28.75
        )
        self.assertEqual(state["overall_start"], 28.75)
        self.assertEqual(state["selected_boundary"], {"kind": "overall_start"})

        state = self._intuitive_command(
            state, "add_exclusion", start=40.0, end=50.0
        )
        cut_id = state["exclusions"][0]["id"]
        state = self._intuitive_command(
            state, "select_boundary", kind="exclusion_start", id=cut_id
        )
        state = self._intuitive_command(
            state, "set_selected_time", time=45.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(45.0, 50.0)],
        )

    def test_intuitive_new_edit_commands_are_rejected_in_result_mode(self):
        for action in (
            {"type": "set_current_position", "time": 20.0},
            {"type": "set_selected_time", "time": 20.0},
            {"type": "remove_exclusion", "id": "cut-1"},
            {"type": "clear_exclusions"},
        ):
            state = self._intuitive_state()
            state["preview_mode"] = "result"
            with self.assertRaises(gr.Error):
                self._intuitive_command(
                    state,
                    action["type"],
                    **{key: value for key, value in action.items() if key != "type"},
                )

    def test_intuitive_toolbars_share_active_and_result_state(self):
        state = self._intuitive_state()
        state["active_tool"] = "overall_end"

        toolbar = render_intuitive_toolbar(state)
        zoom = render_intuitive_state_zoom(state)

        self.assertIn('data-active-tool="overall_end"', toolbar)
        self.assertIn('data-edit-dirty="false"', toolbar)
        self.assertIn("境界ツール（文字・タイムライン共通）", toolbar)
        self.assertNotIn('data-intuitive-preview-action', toolbar)
        self.assertIn("intuitive-timeline-toolbox", zoom)
        self.assertIn("共通境界ツール", zoom)
        self.assertIn(
            'class="intuitive-tool-button is-selected" data-intuitive-tool="overall_end"',
            zoom,
        )

        state["preview_mode"] = "result"
        state["active_tool"] = None
        toolbar = render_intuitive_toolbar(state)
        zoom = render_intuitive_state_zoom(state)
        self.assertIn("編集ツールを選ぶと元動画へ戻り", toolbar)
        self.assertEqual(toolbar.count(" disabled"), 0)
        self.assertEqual(zoom.count(" disabled"), 0)

    def test_toolbar_status_line_absorbs_the_removed_selected_boundary_box(self):
        """C-1: the standalone "選択中の境界" box was removed; its info now
        lives in the toolbox status line (.intuitive-tool-status)."""
        state = self._intuitive_state()
        toolbar = render_intuitive_toolbar(state)
        self.assertIn('intuitive-selected-boundary-chip is-empty">未選択', toolbar)

        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        state = self._intuitive_command(
            state, "set_from_word", start=20.0, end=21.0
        )
        toolbar = render_intuitive_toolbar(state)
        self.assertIn("intuitive-selected-boundary-chip\">選択中: 全体開始", toolbar)
        self.assertIn("00:00:20", toolbar)
        self.assertNotIn("is-empty", toolbar)

    def test_intuitive_tools_can_be_repeated_without_stale_selection(self):
        state = self._intuitive_state()
        for tool, start, end in (
            ("overall_start", 20.0, 21.0),
            ("overall_end", 79.0, 80.0),
        ):
            state = self._intuitive_command(state, "set_tool", tool=tool)
            state = self._intuitive_command(
                state, "set_from_word", start=start, end=end
            )
            self.assertIsNone(state["active_tool"])
            self.assertIsNone(state["selected_word"])
            self.assertIsNone(state["pending_cut_start"])

        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(
            state, "set_from_word", start=30.0, end=31.0
        )
        self.assertEqual(state["active_tool"], "exclude_end")
        self.assertEqual(state["pending_cut_start"], 30.0)
        state = self._intuitive_command(
            state, "set_from_word", start=39.0, end=40.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(30.0, 40.0)],
        )
        self.assertIsNone(state["active_tool"])
        self.assertIsNone(state["selected_word"])
        self.assertIsNone(state["pending_cut_start"])

        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        state = self._intuitive_command(
            state, "set_from_word", start=25.0, end=26.0
        )
        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        state = self._intuitive_command(
            state, "set_from_word", start=69.0, end=70.0
        )
        self.assertEqual((state["overall_start"], state["overall_end"]), (25.0, 70.0))
        self.assertIsNone(state["active_tool"])
        self.assertIsNone(state["selected_word"])
        self.assertIsNone(state["pending_cut_start"])

    def test_switching_tool_cancels_an_unfinished_exclusion(self):
        state = self._intuitive_state()
        state = self._intuitive_command(state, "set_tool", tool="exclude_start")
        state = self._intuitive_command(
            state, "set_from_word", start=30.0, end=31.0
        )
        self.assertEqual(state["active_tool"], "exclude_end")

        state = self._intuitive_command(state, "set_tool", tool="overall_end")
        self.assertEqual(state["active_tool"], "overall_end")
        self.assertIsNone(state["pending_cut_start"])
        self.assertIsNone(state["selected_boundary"])

    def test_intuitive_reducer_merges_overlap_and_clips_on_outer_change(self):
        state = self._intuitive_state()
        for start, end in ((20.0, 30.0), (25.0, 40.0)):
            state = self._intuitive_command(
                state, "set_tool", tool="exclude_start"
            )
            state = self._intuitive_command(
                state, "set_from_word", start=start, end=start + 0.5
            )
            state = self._intuitive_command(
                state, "set_from_word", start=end - 0.5, end=end
            )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(20.0, 40.0)],
        )

        state = self._intuitive_command(
            state, "set_boundary", kind="overall_start", time=25.0
        )
        state = self._intuitive_command(
            state, "set_boundary", kind="overall_end", time=50.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(25.0, 40.0)],
        )

    def test_intuitive_timeline_drag_adds_and_merges_exclusions(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "add_exclusion", start=20.0, end=30.0
        )
        state = self._intuitive_command(
            state, "add_exclusion", start=25.0, end=40.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(20.0, 40.0)],
        )
        self.assertEqual(state["selected_boundary"]["kind"], "exclusion_end")

    def test_intuitive_reducer_rejects_stale_nonfinite_and_entire_cut(self):
        state = self._intuitive_state()
        with self.assertRaises(gr.Error):
            dispatch_intuitive_command({
                "type": "set_tool", "tool": "overall_end",
                "revision": state["revision"] - 1, "nonce": state["nonce"],
            }, state)
        with self.assertRaises(gr.Error):
            dispatch_intuitive_command({
                "type": "set_tool", "tool": "overall_end",
                "revision": state["revision"], "nonce": "stale-session",
            }, state)
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(gr.Error):
                self._intuitive_command(
                    state, "set_boundary", kind="overall_start", time=invalid
                )

        state = self._intuitive_command(
            state, "set_tool", tool="exclude_start"
        )
        state = self._intuitive_command(
            state, "set_from_word", start=10.0, end=10.1
        )
        with self.assertRaises(gr.Error):
            self._intuitive_command(
                state, "set_from_word", start=99.9, end=100.0
            )

    def test_intuitive_bridge_recovers_from_invalid_operation(self):
        state = self._intuitive_state()
        state["active_tool"] = "exclude_end"
        command = {
            "type": "set_from_word",
            "start": 20.0,
            "end": 21.0,
            "revision": state["revision"],
            "nonce": state["nonce"],
        }

        with patch("app.gr.Warning") as warning:
            outputs = handle_intuitive_command(command, state)

        recovered = outputs[0]
        self.assertEqual(recovered["revision"], state["revision"] + 1)
        self.assertEqual(recovered["overall_start"], state["overall_start"])
        self.assertEqual(recovered["overall_end"], state["overall_end"])
        warning.assert_called_once()

    def test_intuitive_bridge_recovers_from_unexpected_non_gr_error_exception(self):
        """FIFO deadlock regression: any exception escaping
        dispatch_intuitive_command -- not just gr.Error -- must still
        advance revision and complete the Gradio callback normally.
        Letting it propagate would fail the callback without ever updating
        the toolbox's data-revision, so the JS awaitRevision loop that
        gates the next queued command never sees a change and every
        subsequent command (including viewport drags) stops being sent
        until the page is reloaded."""
        state = self._intuitive_state()
        command = {
            "type": "set_tool", "tool": "overall_start",
            "revision": state["revision"], "nonce": state["nonce"],
        }

        with (
            patch(
                "app.dispatch_intuitive_command",
                side_effect=TypeError("boom: not a gr.Error"),
            ),
            patch("app.gr.Warning") as warning,
        ):
            outputs = handle_intuitive_command(command, state)

        recovered = outputs[0]
        self.assertEqual(recovered["revision"], state["revision"] + 1)
        warning.assert_called_once()
        self.assertIn("TypeError", warning.call_args.args[0])
        self.assertNotIn("boom", warning.call_args.args[0])

    def test_intuitive_bridge_rejects_invalid_json_shapes_and_recovers_refresh(self):
        state = self._intuitive_state()
        for invalid in ("[]", "1", "x" * 65537):
            with self.assertRaises(gr.Error):
                dispatch_intuitive_command(invalid, state)

        command = {
            "type": "set_viewport",
            "start": 120.0,
            "end": 180.0,
            "revision": state["revision"],
            "nonce": state["nonce"],
        }
        with (
            patch("app._refresh_intuitive_source", side_effect=OSError("failed")),
            patch("app.gr.Warning") as warning,
        ):
            outputs = handle_intuitive_command(command, state)

        recovered = outputs[0]
        self.assertEqual(
            (recovered["viewport_start"], recovered["viewport_end"]),
            (state["viewport_start"], state["viewport_end"]),
        )
        self.assertEqual(recovered["revision"], state["revision"] + 1)
        warning.assert_called_once()

    def test_intuitive_viewport_can_move_outside_initial_preview(self):
        state = self._intuitive_state()
        state = self._intuitive_command(
            state, "set_viewport", start=120.0, end=180.0
        )
        self.assertEqual((state["viewport_start"], state["viewport_end"]), (120.0, 180.0))

        state = self._intuitive_command(
            state, "set_viewport", start=150.0, end=150.2
        )
        self.assertAlmostEqual(state["viewport_end"] - state["viewport_start"], 5.0)
        state = self._intuitive_command(
            state, "set_viewport", start=-50.0, end=500.0
        )
        self.assertEqual((state["viewport_start"], state["viewport_end"]), (0.0, 200.0))
        state["active_tool"] = "overall_end"
        state["preview_mode"] = "result"
        state = self._intuitive_command(
            state, "set_viewport", start=100.0, end=150.0
        )
        self.assertIsNone(state["active_tool"])
        self.assertEqual(state["preview_mode"], "source")

    def test_intuitive_viewport_can_expand_beyond_initial_ninety_seconds(self):
        state = _new_intuitive_state(
            {
                "video_id": "video-long",
                "path": r"F:\videos\long.mp4",
                "duration": 1000.0,
            },
            10.0,
            100.0,
        )

        state = self._intuitive_command(
            state, "set_viewport", start=0.0, end=300.0
        )
        self.assertEqual(
            (state["viewport_start"], state["viewport_end"]),
            (0.0, 300.0),
        )

        # Generated source previews remain bounded even if a drag requests the
        # entire long video.
        state = self._intuitive_command(
            state, "set_viewport", start=-100.0, end=1000.0
        )
        self.assertAlmostEqual(state["viewport_end"] - state["viewport_start"], 600.0)

        overview = render_intuitive_state_overview(state)
        self.assertIn('data-viewport-min-span="5.000"', overview)
        self.assertIn('data-viewport-max-span="600.000"', overview)
        self.assertIn("data-viewport-summary", overview)
        self.assertIn("（600.0秒）", overview)

    def test_overview_and_zoom_timelines_always_render_the_same_viewport(self):
        """A-2 regression: both timelines must derive from the same
        viewport_start/viewport_end so an overview extend is always visible in
        the zoom timeline immediately after the command that produced it.
        """
        state = _new_intuitive_state(
            {
                "video_id": "video-long",
                "path": r"F:\videos\long.mp4",
                "duration": 1000.0,
            },
            10.0,
            100.0,
        )

        def assert_overview_matches_zoom(current_state):
            overview = render_intuitive_state_overview(current_state)
            zoom = render_intuitive_state_zoom(current_state)
            self.assertIn(
                f'data-viewport-start="{current_state["viewport_start"]:.3f}"',
                overview,
            )
            self.assertIn(
                f'data-viewport-end="{current_state["viewport_end"]:.3f}"',
                overview,
            )
            self.assertIn(
                f'data-view-start="{current_state["viewport_start"]:.3f}"', zoom
            )
            self.assertIn(
                f'data-view-end="{current_state["viewport_end"]:.3f}"', zoom
            )

        # A moderate "end handle" extend (well inside min/max span bounds).
        state = self._intuitive_command(state, "set_viewport", start=0.0, end=250.0)
        assert_overview_matches_zoom(state)

        # A moderate "start handle" extend (widen backwards).
        state = self._intuitive_command(state, "set_viewport", start=-40.0, end=250.0)
        assert_overview_matches_zoom(state)

        # An extend that hits the max-span clamp (600s).
        state = self._intuitive_command(state, "set_viewport", start=0.0, end=1000.0)
        assert_overview_matches_zoom(state)
        self.assertAlmostEqual(state["viewport_end"] - state["viewport_start"], 600.0)

        # A shrink that hits the min-span clamp (5s).
        state = self._intuitive_command(state, "set_viewport", start=300.0, end=300.1)
        assert_overview_matches_zoom(state)
        self.assertAlmostEqual(state["viewport_end"] - state["viewport_start"], 5.0)

        # A move near the right edge of the video (clamped against duration).
        state = self._intuitive_command(state, "set_viewport", start=950.0, end=1200.0)
        assert_overview_matches_zoom(state)
        self.assertEqual(state["viewport_end"], 1000.0)

    def test_transcript_focus_is_short_and_clamped_to_zoom_viewport(self):
        state = _new_intuitive_state(
            {
                "video_id": "video-1",
                "path": r"F:\videos\sample.mp4",
                "duration": 1000.0,
            },
            0.0,
            90.0,
        )
        state = self._intuitive_command(
            state, "set_viewport", start=0.0, end=600.0
        )
        self.assertEqual(
            (state["transcript_start"], state["transcript_end"]),
            (0.0, 90.0),
        )

        state = self._intuitive_command(
            state, "set_transcript_focus", time=480.0
        )
        self.assertEqual(state["transcript_focus_sec"], 480.0)
        self.assertEqual(
            (state["transcript_start"], state["transcript_end"]),
            (435.0, 525.0),
        )
        transcript = render_intuitive_transcript([], state)
        self.assertIn("表示中:", transcript)
        self.assertIn("00:07:15", transcript)
        self.assertIn("00:08:45", transcript)
        zoom = render_intuitive_state_zoom(state)
        self.assertIn("intuitive-transcript-window", zoom)
        self.assertIn("文字起こし表示:", zoom)
        self.assertIn('class="intuitive-playhead" style="left:80.0000%"', zoom)

        boundary_state = self._intuitive_command(
            state, "set_boundary", kind="overall_start", time=20.0
        )
        self.assertEqual(boundary_state["playhead_sec"], 480.0)

        # Moving the zoom viewport away clamps the focus and its query interval.
        state = self._intuitive_command(
            state, "set_viewport", start=700.0, end=900.0
        )
        self.assertEqual(state["transcript_focus_sec"], 700.0)
        self.assertEqual(
            (state["transcript_start"], state["transcript_end"]),
            (700.0, 790.0),
        )

    def test_transcript_focus_refresh_does_not_regenerate_video_preview(self):
        calls = []

        class FakeCursor:
            def fetchall(self):
                return [{
                    "start_sec": 435.0, "end_sec": 436.0,
                    "text": "focused", "words_json": None,
                }]

        class FakeConnection:
            def execute(self, _sql, params):
                calls.append(params)
                return FakeCursor()

            def close(self):
                pass

        state = _new_intuitive_state(
            {
                "video_id": "video-1",
                "path": r"F:\videos\sample.mp4",
                "duration": 1000.0,
            },
            0.0,
            90.0,
        )
        state = self._intuitive_command(
            state, "set_viewport", start=0.0, end=600.0
        )
        command = {
            "type": "set_transcript_focus", "time": 480.0,
            "revision": state["revision"], "nonce": state["nonce"],
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.make_intuitive_preview") as preview,
        ):
            output = handle_intuitive_command(command, state)

        preview.assert_not_called()
        self.assertEqual(calls, [("video-1", 435.0, 525.0)])
        self.assertIn("focused", output[5])
        self.assertEqual(output[0]["preview_start"], 0.0)
        self.assertEqual(output[0]["preview_end"], 90.0)

    def test_overview_viewport_edits_remain_valid_after_boundary_and_cut_edits(self):
        state = self._intuitive_state()

        # 全体開始の変更後もviewportの移動・両端リサイズを続けられる。
        state = self._intuitive_command(
            state, "set_boundary", kind="overall_start", time=25.0
        )
        for start, end in ((30.0, 90.0), (35.0, 90.0), (35.0, 80.0)):
            state = self._intuitive_command(
                state, "set_viewport", start=start, end=end
            )

        # 全体終了を変更した後も同じ操作がcanonical state上で成功する。
        state = self._intuitive_command(
            state, "set_boundary", kind="overall_end", time=75.0
        )
        for start, end in ((40.0, 85.0), (45.0, 85.0), (45.0, 78.0)):
            state = self._intuitive_command(
                state, "set_viewport", start=start, end=end
            )

        # 途中カット追加後もviewport操作は編集planを壊さない。
        state = self._intuitive_command(
            state, "add_exclusion", start=50.0, end=55.0
        )
        exclusions = [dict(cut) for cut in state["exclusions"]]
        for start, end in ((50.0, 80.0), (52.0, 80.0), (52.0, 74.0)):
            state = self._intuitive_command(
                state, "set_viewport", start=start, end=end
            )
        self.assertEqual(state["exclusions"], exclusions)
        self.assertEqual((state["overall_start"], state["overall_end"]), (25.0, 75.0))

    def test_viewport_command_refreshes_preview_transcript_and_source_bounds(self):
        class FakeCursor:
            def fetchall(self):
                return [{
                    "start_sec": 120.0, "end_sec": 121.0,
                    "text": "refreshed", "words_json": None,
                }]

        class FakeConnection:
            def execute(self, _sql, _params):
                return FakeCursor()

            def close(self):
                pass

        state = self._intuitive_state()
        command = {
            "type": "set_viewport", "start": 120.0, "end": 180.0,
            "revision": state["revision"], "nonce": state["nonce"],
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.make_intuitive_preview", return_value="viewport.mp4") as preview,
        ):
            output = handle_intuitive_command(command, state)

        self.assertEqual((output[0]["preview_start"], output[0]["preview_end"]), (120.0, 180.0))
        self.assertEqual(output[4]["value"], "viewport.mp4")
        self.assertIn("refreshed", output[5])
        self.assertIn("00:02:00", output[6])
        preview.assert_called_once()

    def test_intuitive_search_result_sets_overall_and_padded_viewport(self):
        class FakeCursor:
            def fetchall(self):
                return [{
                    "start_sec": 20.0, "end_sec": 21.0,
                    "text": "hit", "words_json": None,
                }]

        class FakeConnection:
            def execute(self, _sql, _params):
                return FakeCursor()

            def close(self):
                pass

        video = {
            "video_id": "video-1", "path": r"F:\videos\sample.mp4", "duration": 200.0,
        }
        results = [{"video_id": "video-1", "start": 22.0, "end": 28.0}]
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.get_video", return_value=video),
            patch("app.expand_to_speech_boundary", return_value=(20.0, 30.0)),
            patch("app.make_intuitive_preview", return_value="search.mp4"),
        ):
            output = _load_intuitive_search_result(0, results)

        state = output[0]
        self.assertEqual((state["overall_start"], state["overall_end"]), (20.0, 30.0))
        self.assertEqual((state["viewport_start"], state["viewport_end"]), (10.0, 40.0))
        self.assertEqual(output[1]["value"], "search.mp4")

    def test_intuitive_search_selection_and_bounds_are_robust(self):
        with (
            patch("app._build_table", return_value=[["table"]]),
            patch("app._load_intuitive_search_result", return_value=("loaded",)) as load,
        ):
            tuple_output = on_intuitive_search_select(
                [{}, {}], SimpleNamespace(index=(1, 3))
            )
            int_output = on_intuitive_search_select(
                [{}, {}], SimpleNamespace(index=1)
            )
        self.assertEqual(tuple_output, ([["table"]], "loaded"))
        self.assertEqual(int_output, ([["table"]], "loaded"))
        self.assertEqual([call.args[0] for call in load.call_args_list], [1, 1])

        class FakeCursor:
            def fetchall(self):
                return []

        class FakeConnection:
            def execute(self, _sql, _params):
                return FakeCursor()

            def close(self):
                pass

        video = {
            "video_id": "video-1", "path": r"F:\videos\sample.mp4", "duration": 200.0,
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.get_video", return_value=video),
            patch("app.expand_to_speech_boundary", return_value=(-2.0, 205.0)),
            patch("app.make_intuitive_preview", return_value="search.mp4"),
        ):
            output = _load_intuitive_search_result(
                0, [{"video_id": "video-1", "start": 0.0, "end": 205.0}]
            )
        self.assertEqual(
            (output[0]["overall_start"], output[0]["overall_end"]),
            (0.0, 200.0),
        )

    def test_intuitive_preview_and_save_use_canonical_plan(self):
        state = self._intuitive_state()
        state["exclusions"] = [{"id": "cut-1", "start": 20.0, "end": 30.0}]
        with patch(
            "app.preview_clip_plan",
            return_value=(gr.update(value="edited.mp4"), "edited info", "ignored"),
        ) as preview:
            output = preview_intuitive_editor(state)

        preview.assert_called_once_with(
            10.0,
            100.0,
            {
                "video_id": "video-1",
                "video_path": r"F:\videos\sample.mp4",
                "duration": 200.0,
            },
            {
                "base_start": 10.0,
                "base_end": 100.0,
                "ranges": [[10.0, 20.0], [30.0, 100.0]],
            },
        )
        self.assertIsNone(output[0]["active_tool"])
        self.assertEqual(output[0]["preview_mode"], "result")
        self.assertIn("編集結果プレビュー", output[6])
        self.assertIn('data-preview-mode="result"', output[3])

        with patch("app.on_save", return_value="saved.mp4") as save:
            saved = save_intuitive_editor(state, True, "clips", "sample.mp4")
        self.assertEqual(saved, "saved.mp4")

        for unsafe_name in (r"..\outside.mp4", "folder/outside.mp4"):
            with self.assertRaises(gr.Error):
                save_intuitive_editor(state, True, "clips", unsafe_name)
        self.assertEqual(save.call_args.args[3]["ranges"], [[10.0, 20.0], [30.0, 100.0]])

    def test_exclusion_boundary_remaps_to_surviving_id_after_merge(self):
        state = self._intuitive_state()
        state["exclusions"] = [
            {"id": "cut-a", "start": 20.0, "end": 30.0},
            {"id": "cut-b", "start": 40.0, "end": 50.0},
        ]
        state["selected_boundary"] = {"kind": "exclusion_start", "id": "cut-b"}
        state = self._intuitive_command(
            state,
            "set_boundary",
            kind="exclusion_start",
            id="cut-b",
            time=25.0,
        )
        self.assertEqual(state["exclusions"], [
            {"id": "cut-a", "start": 20.0, "end": 50.0},
        ])
        self.assertEqual(
            state["selected_boundary"],
            {"kind": "exclusion_start", "id": "cut-a"},
        )
        state = self._intuitive_command(state, "adjust_selected", delta=1.0)
        self.assertEqual(state["exclusions"][0]["start"], 21.0)

        state["selected_boundary"] = {"kind": "exclusion_end", "id": "missing"}
        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        self.assertIsNone(state["selected_boundary"])

    def test_return_to_source_refreshes_current_viewport(self):
        class FakeCursor:
            def fetchall(self):
                return []

        class FakeConnection:
            def execute(self, _sql, _params):
                return FakeCursor()

            def close(self):
                pass

        state = self._intuitive_state()
        state["preview_mode"] = "result"
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.make_intuitive_preview", return_value="source.mp4"),
        ):
            output = return_intuitive_source(state)

        self.assertEqual(output[0]["preview_mode"], "source")
        self.assertIsNone(output[0]["active_tool"])
        self.assertEqual(output[4]["value"], "source.mp4")
        self.assertIn("元動画プレビュー", output[6])
        self.assertIn('data-preview-mode="source"', render_intuitive_state_zoom(output[0]))

    def test_result_preview_tool_click_returns_to_source_and_arms_tool(self):
        class FakeCursor:
            def fetchall(self):
                return []

        class FakeConnection:
            def execute(self, _sql, _params):
                return FakeCursor()

            def close(self):
                pass

        def show_result(current):
            with patch(
                "app.preview_clip_plan",
                return_value=(gr.update(value="edited.mp4"), "edited info", "ignored"),
            ):
                return preview_intuitive_editor(current)[0]

        def resume_with_tool(current, tool):
            command = {
                "type": "set_tool",
                "tool": tool,
                "revision": current["revision"],
                "nonce": current["nonce"],
            }
            with (
                patch("app.db.get_conn", return_value=FakeConnection()),
                patch("app.make_intuitive_preview", return_value="source.mp4"),
            ):
                return handle_intuitive_command(command, current)

        state = self._intuitive_state()
        state = self._intuitive_command(state, "set_tool", tool="overall_start")
        state = self._intuitive_command(
            state, "set_from_word", start=20.0, end=21.0
        )

        state = show_result(state)
        self.assertEqual(state["preview_mode"], "result")
        self.assertIsNone(state["selected_word"])
        output = resume_with_tool(state, "overall_end")
        state = output[0]
        self.assertEqual(state["preview_mode"], "source")
        self.assertEqual(state["active_tool"], "overall_end")
        self.assertEqual(output[4]["value"], "source.mp4")
        state = self._intuitive_command(
            state, "set_from_word", start=79.0, end=80.0
        )

        state = show_result(state)
        state = resume_with_tool(state, "exclude_start")[0]
        state = self._intuitive_command(
            state, "set_from_word", start=30.0, end=31.0
        )
        state = self._intuitive_command(
            state, "set_from_word", start=39.0, end=40.0
        )
        self.assertEqual(
            [(cut["start"], cut["end"]) for cut in state["exclusions"]],
            [(30.0, 40.0)],
        )

        state = show_result(state)
        state = resume_with_tool(state, "overall_start")[0]
        state = self._intuitive_command(
            state, "set_from_word", start=25.0, end=26.0
        )
        self.assertEqual((state["overall_start"], state["overall_end"]), (25.0, 80.0))
        self.assertIsNone(state["active_tool"])
        self.assertIsNone(state["selected_word"])
        self.assertIsNone(state["pending_cut_start"])

    def test_intuitive_preview_cache_name_is_safe_and_video_specific(self):
        first = _intuitive_preview_path(
            "video/../unsafe", r"F:\videos\sample.mp4", 10.0, 20.0
        )
        second = _intuitive_preview_path(
            "another-video", r"F:\videos\sample.mp4", 10.0, 20.0
        )

        self.assertTrue(first.name.startswith("intuitive_"))
        self.assertTrue(first.name.endswith(".mp4"))
        self.assertNotIn("unsafe", first.name)
        self.assertNotEqual(first.name, second.name)

    def test_intuitive_loader_uses_first_speech_and_real_transcript(self):
        class FakeCursor:
            def __init__(self, one=None, rows=None):
                self.one = one
                self.rows = rows or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def execute(self, sql, _params):
                if "LIMIT 1" in sql:
                    return FakeCursor(one={"start_sec": 12.0, "end_sec": 13.0})
                return FakeCursor(rows=[{
                    "start_sec": 12.0,
                    "end_sec": 13.0,
                    "text": "<real transcript>",
                    "words_json": None,
                }])

            def close(self):
                pass

        video = {
            "video_id": "video-1",
            "path": r"F:\videos\sample.mp4",
            "duration": 200.0,
        }
        with (
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.get_video", return_value=video),
            patch("app.make_intuitive_preview", return_value="preview.mp4") as preview,
        ):
            output = load_intuitive_video("video-1")

        preview.assert_called_once_with(
            "video-1", video["path"], 10.0, 100.0, 200.0
        )
        self.assertEqual(output[0]["value"], "preview.mp4")
        self.assertIn("sample.mp4", output[0]["label"])
        self.assertIn("&lt;real transcript&gt;", output[1])
        self.assertIn("00:00:10", output[2])
        self.assertIn("00:01:40", output[2])
        self.assertIn("00:03:20", output[3])
        self.assertIn("00:00:10", output[4])
        self.assertIn("00:01:40", output[4])

    def test_tabbed_clip_editor_and_shared_preview_are_built(self):
        config = demo.get_config_file()
        components = config["components"]
        tab_labels = {
            component.get("props", {}).get("label")
            for component in components
            if component.get("type") == "tabitem"
        }
        self.assertIn("① 全体範囲", tab_labels)
        self.assertIn("② 途中カット（任意）", tab_labels)
        accordion_labels = {
            component.get("props", {}).get("label")
            for component in components
            if component.get("type") == "accordion"
        }
        self.assertIn("文単位で全体範囲を調整", accordion_labels)
        self.assertIn("現在位置で全体範囲を設定", accordion_labels)
        self.assertIn("文単位で除外範囲を調整", accordion_labels)
        self.assertNotIn("文単位で区間を調整", accordion_labels)
        component_labels = {
            component.get("props", {}).get("value")
            for component in components
            if component.get("type") == "button"
        }
        self.assertIn("現在位置を全体開始に設定", component_labels)
        self.assertIn("現在位置を全体終了に設定", component_labels)
        self.assertIn("現在位置を除外開始に設定", component_labels)
        self.assertIn("現在位置を除外終了に設定", component_labels)
        self.assertTrue(any(
            component.get("props", {}).get("elem_id") == "clip-preview-video"
            for component in components
        ))
        self.assertTrue(any(
            component.get("props", {}).get("elem_id") == "video-picker-gallery"
            for component in components
        ))
        self.assertTrue(any(
            "reverse-fill-slider" in component.get("props", {}).get("elem_classes", [])
            for component in components
        ))
        self.assertIn("repeating-linear-gradient", _APP_CSS)
        self.assertIn(".time-slider input[type=number]", _APP_CSS)

        # render=Falseの表示コンポーネントをイベントに接続すると、検索結果は
        # 描画されてもGradio側がコンポーネントエラーを重ねて表示してしまう。
        component_ids = {component["id"] for component in components}
        missing_dependency_ids = {
            component_id
            for dependency in config["dependencies"]
            for component_id in dependency.get("inputs", []) + dependency.get("outputs", [])
            if component_id not in component_ids
        }
        self.assertEqual(missing_dependency_ids, set())

        # 現在位置の計算はブラウザ側で行う。基準時刻にはブラウザにも
        # 存在する非表示Number、現在値には画面上のSliderを渡す。
        components_by_id = {component["id"]: component for component in components}
        current_position_dependencies = [
            dependency
            for dependency in config["dependencies"]
            if "clip-preview-video" in (dependency.get("js") or "")
        ]
        self.assertEqual(len(current_position_dependencies), 4)
        for dependency in current_position_dependencies:
            input_types = [
                components_by_id[component_id]["type"]
                for component_id in dependency["inputs"][:2]
            ]
            self.assertEqual(input_types, ["number", "slider"])
            origin_component = components_by_id[dependency["inputs"][0]]
            self.assertFalse(origin_component["props"]["visible"])
            output_types = [
                components_by_id[component_id]["type"]
                for component_id in dependency["outputs"]
            ]
            self.assertEqual(output_types, ["slider"])

        step_dependencies = [
            dependency
            for dependency in config["dependencies"]
            if (dependency.get("api_name") or "").startswith((
                "adjust_time_with_step", "adjust_exclusion_time_with_step",
            ))
        ]
        self.assertEqual(len(step_dependencies), 8)
        for dependency in step_dependencies:
            first_input = components_by_id[dependency["inputs"][0]]
            self.assertEqual(first_input["type"], "slider")

    def test_js_string_constants_have_no_stray_backslash_comment_lines(self):
        """Regression: a `//` JS comment that gets mangled into a lone `\`
        compiles fine as a Python raw string (Python doesn't care), but is
        invalid as the very first token of a JS statement -- it breaks the
        whole IIFE at browser parse time and silently disables every click
        handler in the editor. Plain `unittest`/`import app` cannot catch
        this because they never execute the string as JavaScript; this test
        performs a lightweight static check instead of pulling in a JS parser
        that isn't available in this venv.
        """
        js_constants = {
            "_QUIT_JS": _QUIT_JS,
            "_INTUITIVE_EDITOR_JS": _INTUITIVE_EDITOR_JS,
            "_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS": _INTUITIVE_COLLAPSE_VIDEO_PICKER_JS,
        }
        for name, source in js_constants.items():
            for lineno, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith("\\") and not stripped.startswith("\\n"),
                    f"{name} line {lineno} starts with a stray backslash "
                    f"(likely a mangled `//` comment): {line!r}",
                )
            # Minimal structural sanity: balanced braces/parens/brackets and
            # an even number of backticks (template literals).
            self.assertEqual(
                source.count("{"), source.count("}"), f"{name}: unbalanced {{}}"
            )
            self.assertEqual(
                source.count("("), source.count(")"), f"{name}: unbalanced ()"
            )
            self.assertEqual(
                source.count("["), source.count("]"), f"{name}: unbalanced []"
            )
            self.assertEqual(source.count("`") % 2, 0, f"{name}: odd backtick count")

        # Whole-file sweep so JS constants that aren't module-level exports
        # (e.g. one built inline inside the `with gr.Blocks()` body) are
        # covered too, without needing to import them individually.
        source_path = pathlib.Path(app_module.__file__)
        for lineno, line in enumerate(
            source_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("\\") and not stripped.startswith("\\n"),
                f"app.py line {lineno} starts with a stray backslash: {line!r}",
            )

    def test_intuitive_editor_is_a_connected_top_level_prototype(self):
        config = demo.get_config_file()
        components = config["components"]

        prototype_tabs = [
            component
            for component in components
            if component.get("type") == "tabitem"
            and component.get("props", {}).get("label") == "直感編集（試作）"
        ]
        self.assertEqual(len(prototype_tabs), 1)
        prototype_tab = prototype_tabs[0]
        self.assertEqual(
            prototype_tab["props"].get("elem_id"),
            "intuitive-editor-prototype-tab",
        )

        top_level_tabs = next(
            component
            for component in components
            if component.get("type") == "tabs"
            and component.get("id") == config["layout"]["children"][-1]["id"]
        )
        top_level_layout = next(
            child
            for child in config["layout"]["children"]
            if child["id"] == top_level_tabs["id"]
        )
        self.assertIn(
            prototype_tab["id"],
            [child["id"] for child in top_level_layout["children"]],
        )

        elem_ids = {
            component.get("props", {}).get("elem_id")
            for component in components
        }
        self.assertTrue({
            "intuitive-preview-video",
            "intuitive-video-select",
            "intuitive-load-video",
            "intuitive-reload-videos",
            "intuitive-video-info",
            "intuitive-transcript-panel",
            "intuitive-transcript-words",
            "intuitive-adjust-step",
            "intuitive-overview-timeline",
            "intuitive-zoom-timeline",
            "intuitive-toolbox",
            "intuitive-command-json",
            "intuitive-command-submit",
            "intuitive-search-query",
            "intuitive-search-target",
            "intuitive-search-button",
            "intuitive-search-results",
            "intuitive-preview-result",
            "intuitive-return-source",
        }.issubset(elem_ids))

        html_values = "\n".join(
            str(component.get("props", {}).get("value") or "")
            for component in components
            if component.get("type") == "html"
        )
        self.assertNotIn("data-intuitive-tool=\"normal\"", html_values)
        for label in ("全体開始", "全体終了", "除外開始", "除外終了"):
            self.assertIn(label, html_values)

        dependency_component_ids = {
            component_id
            for dependency in config["dependencies"]
            for component_id in dependency.get("inputs", []) + dependency.get("outputs", [])
        }
        components_by_elem_id = {
            component.get("props", {}).get("elem_id"): component
            for component in components
            if component.get("props", {}).get("elem_id")
        }
        self.assertIn(
            components_by_elem_id["intuitive-video-select"]["id"],
            dependency_component_ids,
        )
        self.assertIn(
            components_by_elem_id["intuitive-preview-video"]["id"],
            dependency_component_ids,
        )
        self.assertEqual(
            components_by_elem_id["intuitive-preview-video"]["props"]["height"],
            310,
        )
        self.assertEqual(
            components_by_elem_id["intuitive-transcript-panel"]["props"].get("min_width"),
            400,
        )
        self.assertEqual(
            components_by_elem_id["intuitive-adjust-step"]["props"]["min_width"],
            360,
        )
        self.assertFalse(
            components_by_elem_id["intuitive-adjust-step"]["props"]["container"]
        )
        self.assertIn(
            "intuitive-control-group-start",
            components_by_elem_id["intuitive-selected-time"]["props"].get(
                "elem_classes", []
            ),
        )
        self.assertIn(
            "intuitive-control-group-start",
            components_by_elem_id["intuitive-apply-current"]["props"].get(
                "elem_classes", []
            ),
        )
        parent_by_id = {}
        def collect_parents(node):
            for child in node.get("children", []):
                parent_by_id[child["id"]] = node["id"]
                collect_parents(child)

        collect_parents(config["layout"])
        transcript_panel_id = components_by_elem_id["intuitive-transcript-panel"]["id"]
        # toolbarと本文を同じflex columnの直下に置き、Gradio Columnの伸長を挟まない。
        self.assertEqual(
            parent_by_id[components_by_elem_id["intuitive-toolbox"]["id"]],
            transcript_panel_id,
        )
        self.assertEqual(
            parent_by_id[components_by_elem_id["intuitive-transcript-words"]["id"]],
            transcript_panel_id,
        )
        self.assertIn(
            components_by_elem_id["intuitive-command-json"]["id"],
            dependency_component_ids,
        )
        api_names = {dependency.get("api_name") for dependency in config["dependencies"]}
        self.assertTrue({
            "handle_intuitive_command",
            "preview_intuitive_editor",
            "return_intuitive_source",
            "save_intuitive_editor",
        }.issubset(api_names))
        self.assertNotIn("adjust_intuitive_boundary", api_names)
        self.assertNotIn("adjust_intuitive_boundary_1", api_names)
        self.assertIn(".intuitive-zoom-track", _APP_CSS)
        self.assertIn(".intuitive-cut-zone", _APP_CSS)
        self.assertIn(".intuitive-playhead", _APP_CSS)
        self.assertIn(".intuitive-tool-buttons", _APP_CSS)
        # A-1 regression: 前へ/後ろへ must look disabled (not just fail
        # server-side) once no boundary is selected, matching apply-time's
        # existing visual-disable convention -- otherwise the button looks
        # clickable but silently does nothing but pop a toast.
        self.assertIn(
            'body:has(#intuitive-toolbox [data-has-selected-boundary="false"]) '
            'button#intuitive-adjust-before',
            _APP_CSS,
        )
        self.assertIn(
            'body:has(#intuitive-toolbox [data-has-selected-boundary="false"]) '
            'button#intuitive-adjust-after',
            _APP_CSS,
        )
        # B: zoom timeline edit-mode toggle -- FIFO command + gated handlers.
        self.assertIn("data-intuitive-toggle-edit-mode", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'set_timeline_edit_mode'", _INTUITIVE_EDITOR_JS)
        self.assertIn("meta.timelineEditMode", _INTUITIVE_EDITOR_JS)
        self.assertIn(
            "root.dataset.timelineEditMode !== 'true'", _INTUITIVE_EDITOR_JS
        )
        self.assertIn(
            "zoom.dataset.timelineEditMode !== 'true'", _INTUITIVE_EDITOR_JS
        )
        self.assertIn(
            '[data-intuitive-zoom][data-timeline-edit-mode="false"] .intuitive-handle',
            _APP_CSS,
        )
        # C-2: visual polish -- tabular numerals for timestamps, unified card
        # look, and the timeline handle color folded into the 3-color palette
        # (primary/grey-hatch/red) instead of a 4th orange hue.
        self.assertIn("font-variant-numeric: tabular-nums", _APP_CSS)
        self.assertIn("text-transform: uppercase", _APP_CSS)
        self.assertNotIn("#ff8a00", _APP_CSS)
        # FIFO deadlock regression: awaitRevision must self-recover (and warn)
        # if the server-side revision never advances, instead of leaving
        # commandBusy stuck forever.
        self.assertIn("AWAIT_REVISION_TIMEOUT_MS", _INTUITIVE_EDITOR_JS)
        self.assertIn("console.warn(", _INTUITIVE_EDITOR_JS)
        self.assertIn("commandBusy = false", _INTUITIVE_EDITOR_JS)
        # "fit overall to viewport" button -- FIFO wiring in the timeline
        # toolbox, mirroring the tool buttons' auto-enable-edit-mode rule.
        self.assertIn("data-intuitive-fit-overall", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'fit_overall_to_viewport'", _INTUITIVE_EDITOR_JS)
        self.assertIn(".intuitive-fit-overall-button", _APP_CSS)
        self.assertIn("document.addEventListener('click'", _INTUITIVE_EDITOR_JS)
        self.assertIn("document.addEventListener('pointerup'", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'add_exclusion'", _INTUITIVE_EDITOR_JS)
        self.assertIn("Object.getOwnPropertyDescriptor", _INTUITIVE_EDITOR_JS)
        self.assertIn("composed: true", _INTUITIVE_EDITOR_JS)
        self.assertIn("pointercancel", _INTUITIVE_EDITOR_JS)
        self.assertIn("event.pointerId !== drag.pointerId", _INTUITIVE_EDITOR_JS)
        self.assertIn("drag.distance = Math.abs(event.clientX - drag.startX)", _INTUITIVE_EDITOR_JS)
        self.assertIn("drag.overallStart", _INTUITIVE_EDITOR_JS)
        self.assertIn("seekZoom(drag.root, event)", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'set_from_timeline'", _INTUITIVE_EDITOR_JS)
        self.assertNotIn("data-intuitive-preview-action", _INTUITIVE_EDITOR_JS)
        self.assertIn("activeTool: root.dataset.activeTool", _INTUITIVE_EDITOR_JS)
        self.assertIn("queued.nonce !== meta.nonce", _INTUITIVE_EDITOR_JS)
        self.assertIn("#intuitive-adjust-step input:checked", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'adjust_selected'", _INTUITIVE_EDITOR_JS)
        self.assertIn("updateViewportDrag(drag, event)", _INTUITIVE_EDITOR_JS)
        self.assertIn("root.dataset.viewportMaxSpan", _INTUITIVE_EDITOR_JS)
        self.assertIn("data-viewport-summary", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'set_transcript_focus'", _INTUITIVE_EDITOR_JS)
        self.assertIn("requestTranscriptFocus(absolute)", _INTUITIVE_EDITOR_JS)
        self.assertIn("meta.previewStart + Number(video.currentTime)", _INTUITIVE_EDITOR_JS)
        self.assertIn("meta.previewMode === 'result' || !meta.activeTool", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'set_current_position'", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'set_selected_time'", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'remove_exclusion'", _INTUITIVE_EDITOR_JS)
        self.assertIn("type: 'clear_exclusions'", _INTUITIVE_EDITOR_JS)
        self.assertIn("data-intuitive-remove-exclusion", _INTUITIVE_EDITOR_JS)
        self.assertIn("fallbackSwitchArmed = false", _INTUITIVE_EDITOR_JS)
        self.assertIn("root.dataset.editDirty === 'true'", _INTUITIVE_EDITOR_JS)
        self.assertIn("現在の編集内容はこの画面から失われます", _INTUITIVE_EDITOR_JS)
        self.assertIn("event.stopImmediatePropagation()", _INTUITIVE_EDITOR_JS)
        self.assertIn("#intuitive-video-gallery", _INTUITIVE_EDITOR_JS)
        self.assertIn("openIntuitivePicker()", _INTUITIVE_EDITOR_JS)
        self.assertIn("#intuitive-video-select", _INTUITIVE_EDITOR_JS)
        self.assertIn("intuitive-video-picker", _INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        self.assertIn("button.click()", _INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        self.assertIn("scrollIntoView", _INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        self.assertIn(".intuitive-transcript-window", _APP_CSS)
        self.assertIn("width: max(100%, 32px)", _APP_CSS)
        self.assertIn("flex: 1 1 0 !important", _APP_CSS)
        self.assertIn("overflow-y: scroll", _APP_CSS)
        self.assertIn(".intuitive-timeline-toolbox", _APP_CSS)
        self.assertIn(".intuitive-control-group-start", _APP_CSS)
        self.assertIn(".intuitive-control-group-start::before", _APP_CSS)
        self.assertIn("height: 1rem; overflow: visible", _APP_CSS)
        intuitive_fns = {
            "load_intuitive_editor", "do_intuitive_search",
            "handle_intuitive_command", "preview_intuitive_editor",
            "return_intuitive_source", "save_intuitive_editor",
        }
        routed = [fn for fn in demo.fns.values() if fn.name in intuitive_fns]
        self.assertTrue(routed)
        self.assertTrue(all(fn.concurrency_id == "intuitive-editor-state" for fn in routed))
        self.assertTrue(all(fn.concurrency_limit == 1 for fn in routed))
        video_select_id = components_by_elem_id["intuitive-video-select"]["id"]
        auto_preview = [
            dependency for dependency in config["dependencies"]
            if (video_select_id, "input") in dependency.get("targets", [])
        ]
        self.assertEqual(len(auto_preview), 1)
        self.assertEqual(auto_preview[0].get("trigger_mode"), "always_last")
        for elem_id in (
            "intuitive-edit-summary",
            "intuitive-exclusion-list",
            "intuitive-selected-time",
            "intuitive-apply-time",
            "intuitive-apply-current",
            "intuitive-folder-browse",
            "intuitive-video-picker",
            "intuitive-video-gallery",
            "intuitive-video-filter",
            "intuitive-video-filter-button",
            "intuitive-reload-videos",
            "intuitive-reselect-video",
        ):
            self.assertIn(elem_id, components_by_elem_id)
        gallery_id = components_by_elem_id["intuitive-video-gallery"]["id"]
        gallery_select = [
            dependency for dependency in config["dependencies"]
            if (gallery_id, "select") in dependency.get("targets", [])
        ]
        self.assertEqual(len(gallery_select), 1)
        self.assertEqual(gallery_select[0].get("trigger_mode"), "always_last")
        self.assertEqual(len(gallery_select[0].get("outputs", [])), 13)
        initial_cards = components_by_elem_id["intuitive-video-gallery"]["props"].get(
            "value"
        ) or []
        gallery_ids_state = next(
            component for component in components
            if component["id"] == gallery_select[0]["inputs"][0]
        )
        self.assertGreater(len(initial_cards), 0)
        self.assertEqual(gallery_ids_state["type"], "state")
        self.assertEqual(len(initial_cards), len(_INTUITIVE_INITIAL_VIDEO_IDS))
        self.assertNotIn("__all_videos__", _INTUITIVE_INITIAL_VIDEO_IDS)
        self.assertTrue({
            "build_intuitive_video_gallery",
            "refresh_intuitive_video_picker",
            "select_intuitive_video_from_gallery",
        }.issubset(api_names))
        collapse_dependencies = [
            dependency for dependency in config["dependencies"]
            if "intuitive-video-picker" in str(dependency.get("js") or "")
            and dependency.get("trigger_only_on_success")
        ]
        self.assertEqual(len(collapse_dependencies), 3)
        folder_id = components_by_elem_id["intuitive-folder-browse"]["id"]
        out_dir_components = [
            component for component in components
            if component.get("type") == "textbox"
            and component.get("props", {}).get("label") == "保存先"
        ]
        self.assertTrue(any(
            (folder_id, "click") in dependency.get("targets", [])
            and any(component["id"] in dependency.get("outputs", [])
                    for component in out_dir_components)
            for dependency in config["dependencies"]
        ))

    def test_search_selection_and_none_refresh_are_safe(self):
        class FakeConnection:
            def close(self):
                pass

        class FakeEmbedder:
            def encode(self, _queries):
                return SimpleNamespace(shape=(1, 3))

        result = {
            "video_id": "video-1",
            "start": 10.0,
            "end": 15.0,
            "match_type": "文字一致",
            "score": 1.0,
            "text": "命令を確認する場面",
        }
        video = {
            "video_id": "video-1",
            "path": r"F:\videos\sample.mp4",
            "duration": 120.0,
        }
        with (
            patch("app.config.TEXT_INDEX_PATH") as index_path,
            patch("app.db.get_conn", return_value=FakeConnection()),
            patch("app.db.init_db"),
            patch("app.db.list_videos", return_value=[video]),
            patch("app.db.get_video", return_value=video),
            patch("app.get_embedder", return_value=FakeEmbedder()),
            patch("app.VectorIndex.load", return_value=object()),
            patch("app.search_chunks", return_value=[result]),
            patch("app.expand_to_speech_boundary", return_value=(9.0, 16.0)),
            patch("app.region_transcript", return_value="transcript"),
            patch("app.get_region_sentences", return_value=[]),
            patch("app.make_preview", return_value="preview.mp4"),
        ):
            index_path.exists.return_value = True
            output = do_search("命令", "(すべての動画)", 5, 0.55)

        self.assertEqual(output[0][0][1], "sample.mp4 [video-1]")
        self.assertEqual(output[2]["value"], "preview.mp4")
        self.assertIn("sample.mp4", output[2]["label"])
        self.assertEqual(output[3:5], (9.0, 16.0))

        no_updates = always_refresh(None, output[4], output[7])
        self.assertEqual(len(no_updates), 4)
        self.assertTrue(all(value.get("__type__") == "update" for value in no_updates))

        empty_plan, table, message = reset_clip_plan(None, output[4])
        self.assertIsNone(empty_plan)
        self.assertEqual(table, [])
        self.assertIn("正しく指定", message)

    def test_cannot_exclude_entire_selection(self):
        plan, _, _ = reset_clip_plan(0.0, 60.0)

        with self.assertRaises(gr.Error):
            exclude_clip_range(0.0, 60.0, 0.0, 60.0, plan)


if __name__ == "__main__":
    unittest.main()
