import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gradio as gr

from app import (
    _APP_CSS,
    _clip_plan_exclusions,
    _clip_plan_ranges,
    adjust_exclusion_time,
    adjust_exclusion_time_with_step,
    always_refresh,
    build_video_gallery,
    do_search,
    exclude_clip_range,
    list_video_choices,
    parse_video_choice,
    remove_clip_exclusion,
    render_clip_plan_timeline,
    reset_clip_plan,
    reset_clip_plan_after_range_change,
    select_clip_exclusion,
    select_video_from_gallery,
    sync_exclusion_controls,
    selected_video_info,
    demo,
)


class ClipPlanTest(unittest.TestCase):
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
