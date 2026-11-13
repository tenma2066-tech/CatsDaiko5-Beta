"""Unit tests for the read-only save structural auditor."""
import types
import unittest

from save_audit import audit_save


def make_cat(cat_id):
    upgrade = types.SimpleNamespace(base=0, plus=0)
    return types.SimpleNamespace(
        id=cat_id,
        unlocked=0,
        current_form=0,
        unlocked_forms=0,
        gatya_seen=0,
        fourth_form=0,
        catseyes_used=0,
        upgrade=types.SimpleNamespace(base=0, plus=0),
        max_upgrade_level=types.SimpleNamespace(base=0, plus=0),
        catguide_collected=False,
        talents=None,
    )


class SaveAuditTests(unittest.TestCase):
    def make_save(self, ids=(0, 1, 2)):
        cats = [None if value is None else make_cat(value) for value in ids]
        return types.SimpleNamespace(
            cats=types.SimpleNamespace(
                cats=cats, storage_items=[], favourites={}, chara_new_flags={}
            ),
            catfood=0, xp=0, np=0, normal_tickets=0, rare_tickets=0,
            platinum_tickets=0, legend_tickets=0, leadership=0,
            platinum_shards=0, catfruit=[], catseyes=[], catamins=[],
            lucky_tickets=[], event_capsules=[], event_capsules_2=[],
            labyrinth_medals=[],
            battle_items=types.SimpleNamespace(items=[]),
        )

    def codes(self, report):
        return [item.code for item in report.findings]

    def test_normal_save_structures_are_clean(self):
        report = audit_save(self.make_save())
        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.cats_checked, 3)
        self.assertEqual(report.findings, ())

    def test_missing_cat_container_is_critical(self):
        report = audit_save(types.SimpleNamespace())
        self.assertFalse(report.ok)
        self.assertIn("missing_cat_container", self.codes(report))

    def test_missing_cat_collection_is_critical(self):
        report = audit_save(types.SimpleNamespace(cats=types.SimpleNamespace()))
        self.assertIn("missing_cat_collection", self.codes(report))

    def test_invalid_collection_is_critical(self):
        report = audit_save(types.SimpleNamespace(cats=types.SimpleNamespace(cats=None)))
        self.assertFalse(report.ok)

    def test_null_record_is_critical(self):
        report = audit_save(self.make_save((0, None, 2)))
        self.assertIn("null_cat_record", self.codes(report))

    def test_unreadable_id_is_critical(self):
        save = self.make_save()
        save.cats.cats[1].id = "1"
        report = audit_save(save)
        self.assertIn("invalid_cat_id", self.codes(report))

    def test_duplicate_id_is_critical(self):
        report = audit_save(self.make_save((0, 1, 1)))
        self.assertIn("duplicate_cat_id", self.codes(report))

    def test_position_mismatch_is_critical(self):
        report = audit_save(self.make_save((0, 4, 2)))
        self.assertIn("cat_id_position_mismatch", self.codes(report))

    def test_out_of_range_id_is_critical(self):
        report = audit_save(self.make_save((-1,)))
        self.assertIn("cat_id_out_of_range", self.codes(report))

    def test_empty_roster_is_warning(self):
        report = audit_save(self.make_save(()))
        self.assertTrue(report.ok)
        self.assertEqual(report.warning_count, 1)

    def test_invalid_cat_field_is_critical(self):
        save = self.make_save()
        save.cats.cats[0].current_form = "third"
        self.assertIn("invalid_cat_field", self.codes(audit_save(save)))

    def test_owned_known_error_character_is_flagged_as_warning(self):
        save = self.make_save((0, 1))
        save.cats.cats[1].unlocked = 1
        report = audit_save(save, known_error_cat_ids={1})
        self.assertTrue(report.ok)
        self.assertIn("known_error_cat_unlocked", self.codes(report))

    def test_unowned_known_error_character_is_not_flagged(self):
        save = self.make_save((0, 1))
        report = audit_save(save, known_error_cat_ids={1})
        self.assertNotIn("known_error_cat_unlocked", self.codes(report))

    def test_unusual_form_is_warning_not_deleted(self):
        save = self.make_save()
        save.cats.cats[0].current_form = 8
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("unusual_current_form", self.codes(report))

    def test_invalid_upgrade_is_critical(self):
        save = self.make_save()
        save.cats.cats[0].upgrade.base = None
        self.assertIn("invalid_upgrade_value", self.codes(audit_save(save)))

    def test_invalid_talent_record_is_critical(self):
        save = self.make_save()
        save.cats.cats[0].talents = [types.SimpleNamespace(id=2, level=-1)]
        self.assertIn("negative_talent_value", self.codes(audit_save(save)))

    def test_invalid_storage_type_is_warning(self):
        save = self.make_save()
        save.cats.storage_items = [types.SimpleNamespace(item_id=0, item_type=9)]
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("unknown_storage_type", self.codes(report))

    def test_invalid_storage_cat_reference_is_warning(self):
        save = self.make_save()
        save.cats.storage_items = [types.SimpleNamespace(item_id=99, item_type=1)]
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("storage_cat_reference_invalid", self.codes(report))

    def test_invalid_favourite_value_is_critical(self):
        save = self.make_save()
        save.cats.favourites = {0: 1}
        self.assertIn("invalid_favourite_value", self.codes(audit_save(save)))

    def test_invalid_currency_and_negative_item_count(self):
        save = self.make_save()
        save.catfood = -1
        save.xp = "a lot"
        report = audit_save(save)
        self.assertIn("negative_currency", self.codes(report))
        self.assertIn("invalid_currency_field", self.codes(report))

    def test_negative_list_item_is_critical(self):
        save = self.make_save()
        save.catfruit = [0, -2]
        self.assertIn("negative_list_value", self.codes(audit_save(save)))

    def test_negative_battle_item_amount_is_critical(self):
        save = self.make_save()
        save.battle_items.items = [types.SimpleNamespace(amount=-1)]
        self.assertIn("negative_battle_item_amount", self.codes(audit_save(save)))

    def test_valid_lineups_are_clean(self):
        save = self.make_save((0, 1))
        empty_slots = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        save.lineups = types.SimpleNamespace(
            slots=[types.SimpleNamespace(slots=empty_slots)],
            selected_slot=0, unlocked_slots=1,
        )
        self.assertTrue(audit_save(save).ok)

    def test_lineup_reference_outside_roster_is_warning(self):
        save = self.make_save((0, 1))
        lineup_slots = [types.SimpleNamespace(cat_id=-1, form=0) for _ in range(10)]
        lineup_slots[0] = types.SimpleNamespace(cat_id=99)
        save.lineups = types.SimpleNamespace(
            slots=[types.SimpleNamespace(slots=lineup_slots)],
            selected_slot=0, unlocked_slots=1,
        )
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("lineup_cat_reference_out_of_range", self.codes(report))

    def test_malformed_lineup_collection_is_critical(self):
        save = self.make_save()
        save.lineups = types.SimpleNamespace(slots=None)
        self.assertIn("invalid_lineup_collection", self.codes(audit_save(save)))

    def test_id_wrapper_is_supported(self):
        save = self.make_save((0,))
        save.cats.cats[0].id = types.SimpleNamespace(value=0)
        self.assertTrue(audit_save(save).ok)

    def test_audit_does_not_mutate_save(self):
        save = self.make_save((0, 1, 1))
        before = list(save.cats.cats)
        audit_save(save)
        self.assertEqual(save.cats.cats, before)
        self.assertIs(save.cats.cats[0], before[0])


    def test_active_favourite_for_unowned_cat_is_warning(self):
        save = self.make_save((0, 1))
        save.cats.favourites = {1: True}
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("favourite_unowned_cat_reference", self.codes(report))

    def test_lineup_for_unowned_cat_is_warning(self):
        save = self.make_save((0, 1))
        entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        entries[0].cat_id = 1
        save.lineups = types.SimpleNamespace(
            slots=[types.SimpleNamespace(slots=entries)],
            selected_slot=0, unlocked_slots=1,
        )
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("lineup_unowned_cat_reference", self.codes(report))

    def test_locked_unused_lineup_does_not_warn_for_unowned_cat(self):
        save = self.make_save((0, 1))
        empty_entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        locked_entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        locked_entries[0].cat_id = 1
        save.lineups = types.SimpleNamespace(
            slots=[
                types.SimpleNamespace(slots=empty_entries),
                types.SimpleNamespace(slots=locked_entries),
            ],
            selected_slot=0, unlocked_slots=1,
        )
        report = audit_save(save)
        self.assertNotIn("lineup_unowned_cat_reference", self.codes(report))

    def test_all_unlocked_lineups_are_checked(self):
        save = self.make_save((0, 1))
        first_entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        second_entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        second_entries[0].cat_id = 1
        save.lineups = types.SimpleNamespace(
            slots=[
                types.SimpleNamespace(slots=first_entries),
                types.SimpleNamespace(slots=second_entries),
            ],
            selected_slot=0, unlocked_slots=2,
        )
        self.assertIn("lineup_unowned_cat_reference", self.codes(audit_save(save)))

    def test_owned_favourite_and_lineup_are_not_flagged(self):
        save = self.make_save((0, 1))
        save.cats.cats[1].unlocked = 1
        save.cats.favourites = {1: True}
        entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        entries[0].cat_id = 1
        save.lineups = types.SimpleNamespace(
            slots=[types.SimpleNamespace(slots=entries)],
            selected_slot=0, unlocked_slots=1,
        )
        report = audit_save(save)
        self.assertNotIn("favourite_unowned_cat_reference", self.codes(report))
        self.assertNotIn("lineup_unowned_cat_reference", self.codes(report))

    def test_known_error_favourite_reference_is_flagged(self):
        save = self.make_save()
        save.cats.favourites = {1: True}
        report = audit_save(save, known_error_cat_ids={1})
        self.assertIn("known_error_cat_referenced", self.codes(report))

    def test_known_error_storage_reference_is_flagged(self):
        save = self.make_save()
        save.cats.storage_items = [types.SimpleNamespace(item_id=1, item_type=1)]
        report = audit_save(save, known_error_cat_ids={1})
        self.assertIn("known_error_cat_referenced", self.codes(report))

    def test_known_error_lineup_reference_is_flagged(self):
        save = self.make_save()
        entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        entries[0].cat_id = 1
        save.lineups = types.SimpleNamespace(
            slots=[types.SimpleNamespace(slots=entries)],
            selected_slot=0, unlocked_slots=1,
        )
        report = audit_save(save, known_error_cat_ids={1})
        self.assertIn("known_error_cat_referenced", self.codes(report))

    def test_inactive_favourite_is_not_flagged_as_reference(self):
        save = self.make_save()
        save.cats.favourites = {1: False}
        report = audit_save(save, known_error_cat_ids={1})
        self.assertNotIn("known_error_cat_referenced", self.codes(report))



    def make_auxiliary_save(self):
        save = self.make_save()
        save.gamatoto = types.SimpleNamespace(
            remaining_seconds=0.0, return_flag=False, xp=0, dest_id=0,
            recon_length=0, unknown=0, notif_value=0,
            helpers=types.SimpleNamespace(helpers=[
                types.SimpleNamespace(id=-1)
            ]),
            is_ad_present=False, skin=0,
            collab_flags={}, collab_durations={},
        )
        save.ototo = types.SimpleNamespace(
            base_materials=types.SimpleNamespace(materials=[
                types.SimpleNamespace(amount=0)
            ]),
            remaining_seconds=0.0, return_flag=False,
            improve_id=0, engineers=0,
            cannons=types.SimpleNamespace(
                cannons={0: types.SimpleNamespace(development=0, levels=[0, 0])},
                selected_parts=[[0, 0, 0]],
            ),
        )
        save.missions = types.SimpleNamespace(
            clear_states={1: 0}, requirements={1: 0},
            progress_types={1: 0}, gamatoto_values={},
            nyancombo_values={}, user_rank_values={}, expiry_values={},
            preparing_values={1: False}, weekly_missions={1: True},
        )
        return save

    def test_valid_gamatoto_ototo_and_missions_are_clean(self):
        report = audit_save(self.make_auxiliary_save())
        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.findings, ())

    def test_invalid_gamatoto_helper_type_is_critical(self):
        save = self.make_auxiliary_save()
        save.gamatoto.helpers.helpers = [types.SimpleNamespace(id="helper")]
        self.assertIn("invalid_gamatoto_helper_id", self.codes(audit_save(save)))

    def test_invalid_gamatoto_timer_nan_is_critical(self):
        save = self.make_auxiliary_save()
        save.gamatoto.remaining_seconds = float("nan")
        self.assertIn("invalid_gamatoto_timer", self.codes(audit_save(save)))

    def test_invalid_ototo_cannon_row_is_critical(self):
        save = self.make_auxiliary_save()
        save.ototo.cannons.selected_parts = [[0, 1]]
        self.assertIn("invalid_ototo_selected_part_row", self.codes(audit_save(save)))

    def test_negative_ototo_material_is_critical(self):
        save = self.make_auxiliary_save()
        save.ototo.base_materials.materials[0].amount = -1
        self.assertIn("negative_ototo_material_amount", self.codes(audit_save(save)))

    def test_invalid_mission_map_is_critical(self):
        save = self.make_auxiliary_save()
        save.missions.clear_states = []
        self.assertIn("invalid_mission_map", self.codes(audit_save(save)))

    def test_negative_mission_requirement_is_critical(self):
        save = self.make_auxiliary_save()
        save.missions.requirements[1] = -1
        self.assertIn("negative_mission_requirement", self.codes(audit_save(save)))

    def test_mission_preparing_supports_bool_or_int(self):
        save = self.make_auxiliary_save()
        save.missions.preparing_values = {1: 2}
        self.assertNotIn("invalid_mission_entry", self.codes(audit_save(save)))



    def make_stage_save(self):
        save = self.make_save()
        story_chapters = []
        for _ in range(10):
            stages = [
                types.SimpleNamespace(clear_times=0, treasure=0, itf_timed_score=0)
                for _ in range(51)
            ]
            story_chapters.append(types.SimpleNamespace(
                selected_stage=0, progress=0, stages=stages,
                time_until_treasure_chance=0, treasure_chance_duration=0,
                treasure_chance_value=0, treasure_chance_stage_id=0,
                treasure_festival_type=0,
            ))
        save.story = types.SimpleNamespace(chapters=story_chapters)
        event_subchapter = types.SimpleNamespace(
            selected_stage=0, clear_progress=0, chapter_unlock_state=0,
            stages=[types.SimpleNamespace(clear_amount=0) for _ in range(4)],
        )
        event_star_group = types.SimpleNamespace(
            chapters=[event_subchapter], legend_restriction=0,
        )
        event_map_group = types.SimpleNamespace(chapters=[event_star_group])
        save.event_stages = types.SimpleNamespace(
            chapters=[event_map_group], chapter_completion_count={},
            displayed_cleared_limit_text={}, event_start_dates={},
            stages_reward_claimed=[],
        )
        return save

    def test_valid_story_and_event_stage_structures_are_clean(self):
        report = audit_save(self.make_stage_save())
        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.findings, ())

    def test_invalid_story_stage_value_is_critical(self):
        save = self.make_stage_save()
        save.story.chapters[0].stages[0].treasure = "gold"
        self.assertIn("invalid_story_stage_field", self.codes(audit_save(save)))

    def test_null_story_stage_is_critical(self):
        save = self.make_stage_save()
        save.story.chapters[0].stages[0] = None
        self.assertIn("null_story_stage", self.codes(audit_save(save)))

    def test_unusual_story_stage_count_is_warning(self):
        save = self.make_stage_save()
        save.story.chapters[0].stages.pop()
        report = audit_save(save)
        self.assertTrue(report.ok)
        self.assertIn("unusual_story_stage_count", self.codes(report))

    def test_invalid_event_clear_amount_is_critical(self):
        save = self.make_stage_save()
        save.event_stages.chapters[0].chapters[0].chapters[0].stages[0].clear_amount = "x"
        self.assertIn("invalid_event_stage_clear_amount", self.codes(audit_save(save)))

    def test_invalid_event_metadata_map_is_critical(self):
        save = self.make_stage_save()
        save.event_stages.chapter_completion_count = []
        self.assertIn("invalid_event_metadata_map", self.codes(audit_save(save)))


    def test_common_map_wrapper_structures_are_checked(self):
        save = self.make_save()
        chapter = types.SimpleNamespace(
            selected_stage=0, clear_progress=0, chapter_unlock_state=0,
            stages=[types.SimpleNamespace(clear_times=0)],
        )
        save.tower = types.SimpleNamespace(
            chapters=types.SimpleNamespace(chapters=[
                types.SimpleNamespace(chapters=[chapter])
            ]),
            item_obtain_states=[[False, True]],
        )
        save.challenge = types.SimpleNamespace(
            chapters=types.SimpleNamespace(chapters=[
                types.SimpleNamespace(chapters=[types.SimpleNamespace(
                    selected_stage=0, clear_progress=0, chapter_unlock_state=0,
                    stages=[types.SimpleNamespace(clear_times=0)],
                )])
            ]),
            scores=[0], shown_popup=False,
        )
        report = audit_save(save)
        self.assertTrue(report.ok, report.findings)

    def test_invalid_map_stage_value_is_critical(self):
        save = self.make_save()
        save.tower = types.SimpleNamespace(
            chapters=types.SimpleNamespace(chapters=[
                types.SimpleNamespace(chapters=[types.SimpleNamespace(
                    selected_stage=0, clear_progress=0, chapter_unlock_state=0,
                    stages=[types.SimpleNamespace(clear_times="bad")],
                )])
            ]),
            item_obtain_states=[],
        )
        self.assertIn("invalid_map_stage_value", self.codes(audit_save(save)))

    def test_invalid_ex_stage_clear_amount_is_critical(self):
        save = self.make_save()
        save.ex_stages = types.SimpleNamespace(chapters=[
            types.SimpleNamespace(stages=[types.SimpleNamespace(clear_amount=None)])
        ])
        self.assertIn("invalid_ex_stage_clear_amount", self.codes(audit_save(save)))

    def test_invalid_outbreak_stage_flag_is_critical(self):
        save = self.make_save()
        outbreak = types.SimpleNamespace(cleared=1)
        chapter = types.SimpleNamespace(id=0, outbreaks={1: outbreak})
        save.outbreaks = types.SimpleNamespace(
            chapters={0: chapter}, current_outbreaks={},
            zombie_event_remaining_time=0.0,
        )
        self.assertIn("invalid_outbreak_stage", self.codes(audit_save(save)))


    def test_aku_and_gauntlet_map_groups_are_checked(self):
        save = self.make_save()
        aku_chapter = types.SimpleNamespace(
            current_stage=0,
            stages=[types.SimpleNamespace(clear_times=0)],
        )
        save.aku = types.SimpleNamespace(chapters=[
            types.SimpleNamespace(chapters=[aku_chapter])
        ])
        gauntlet_chapter = types.SimpleNamespace(
            selected_stage=0, clear_progress=0, chapter_unlock_state=0,
            stages=[types.SimpleNamespace(clear_times=0)],
        )
        save.gauntlets = types.SimpleNamespace(
            chapters=[types.SimpleNamespace(chapters=[gauntlet_chapter])],
            unknown=[0],
        )
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_aku_stage_value_is_critical(self):
        save = self.make_save()
        save.aku = types.SimpleNamespace(chapters=[
            types.SimpleNamespace(chapters=[types.SimpleNamespace(
                current_stage=0, stages=[types.SimpleNamespace(clear_times="bad")]
            )])
        ])
        self.assertIn("invalid_star_group_stage_value", self.codes(audit_save(save)))


    def test_item_reward_and_timed_score_maps_are_checked(self):
        save = self.make_save()
        save.item_reward_stages = types.SimpleNamespace(
            sub_chapters=[types.SimpleNamespace(sub_chapters=[
                types.SimpleNamespace(stages=[types.SimpleNamespace(claimed=False)])
            ])],
            item_obtains=types.SimpleNamespace(
                item_obtain_sets={1: types.SimpleNamespace(item_obtains={2: types.SimpleNamespace(flag=True)})},
            ),
            unobtained_items=types.SimpleNamespace(
                unobtained_items={3: types.SimpleNamespace(unobtained=False)}
            ),
        )
        save.timed_score_stages = types.SimpleNamespace(
            sub_chapters=[types.SimpleNamespace(sub_chapters=[
                types.SimpleNamespace(stages=[types.SimpleNamespace(score=100)])
            ])]
        )
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_item_reward_claim_flag_is_critical(self):
        save = self.make_save()
        save.item_reward_stages = types.SimpleNamespace(
            sub_chapters=[types.SimpleNamespace(sub_chapters=[
                types.SimpleNamespace(stages=[types.SimpleNamespace(claimed=1)])
            ])],
            item_obtains=types.SimpleNamespace(item_obtain_sets={}),
            unobtained_items=types.SimpleNamespace(unobtained_items={}),
        )
        self.assertIn("invalid_subchapter_stage_value", self.codes(audit_save(save)))

    def test_legend_quest_and_zero_legend_structures_are_checked(self):
        save = self.make_save()
        lq_chapter = types.SimpleNamespace(selected_stage=0, clear_progress=1, chapter_unlock_state=3,
                                           stages=[types.SimpleNamespace(clear_times=1, tries=2)])
        save.legend_quest = types.SimpleNamespace(chapters=[types.SimpleNamespace(chapters=[lq_chapter])],
                                                  unknown=[0], ids=[1])
        zero_chapter = types.SimpleNamespace(selected_stage=0, clear_progress=1, unlock_state=3,
                                             stages=[types.SimpleNamespace(clear_times=1)])
        save.zero_legends = types.SimpleNamespace(chapters=[types.SimpleNamespace(unknown=0, chapters=[zero_chapter])])
        save.dojo_chapters = types.SimpleNamespace(chapters=[types.SimpleNamespace(unknown=0, chapters=[zero_chapter])])
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_legend_quest_tries_is_critical(self):
        save = self.make_save()
        save.legend_quest = types.SimpleNamespace(chapters=[types.SimpleNamespace(chapters=[
            types.SimpleNamespace(selected_stage=0, clear_progress=0, chapter_unlock_state=0,
                                  stages=[types.SimpleNamespace(clear_times=0, tries="x")])
        ])], unknown=[], ids=[])
        self.assertIn("invalid_legend_quest_stage", self.codes(audit_save(save)))

    def test_dojo_and_shrine_data_are_checked(self):
        save = self.make_save()
        save.dojo = types.SimpleNamespace(
            chapters=types.SimpleNamespace(chapters={1: types.SimpleNamespace(stages={2: types.SimpleNamespace(score=100)})}),
            item_lock_flags=False, item_locks=[False] * 6,
            ranking=types.SimpleNamespace(score=0, ranking=0, start_date=0, end_date=0, event_number=0,
                has_submitted=False, has_completed=False, has_seen_results=False,
                should_show_rank_description=False, should_show_start_message=False,
                submit_error_flag=False, did_win_rewards=False),
        )
        save.cat_shrine = types.SimpleNamespace(unknown=False, stamp_1=0.0, stamp_2=0.0,
            shrine_gone=False, flags=[0, 1], xp_offering=0, dialogs=0)
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_cat_shrine_timestamp_is_critical(self):
        save = self.make_save()
        save.cat_shrine = types.SimpleNamespace(unknown=False, stamp_1=float("nan"), stamp_2=0.0,
            shrine_gone=False, flags=[], xp_offering=0, dialogs=0)
        self.assertIn("invalid_cat_shrine_timestamp", self.codes(audit_save(save)))

    def test_invalid_behemoth_culling_group_is_critical(self):
        save = self.make_save()
        save.behemoth_culling = types.SimpleNamespace(chapters=[], unknown="bad")
        self.assertIn("invalid_behemoth_unknown", self.codes(audit_save(save)))


    def test_gacha_enigma_orbs_and_gambling_structures_are_clean(self):
        save = self.make_save()
        save.gatya = types.SimpleNamespace(
            rare_seed=1, normal_seed=2, event_seed=3, stepup_stage_3_cooldown=0,
            previous_normal_roll=0, previous_normal_roll_type=0, previous_rare_roll=0,
            previous_rare_roll_type=0, trade_progress=0, unknown1=False,
            roll_single=False, roll_multi=False, step_up_stages={1: 2},
            stepup_durations={1: 10.0},
        )
        save.enigma = types.SimpleNamespace(
            energy_since_1=0, energy_since_2=0, enigma_level=0, unknown_1=0,
            unknown_2=False, stages=[types.SimpleNamespace(level=1, stage_id=25000,
                decoding_satus=0, start_time=0.0)], extra=None,
        )
        save.talent_orbs = types.SimpleNamespace(orbs={1: types.SimpleNamespace(id=1, value=2)})
        gambling = types.SimpleNamespace(completed={1: True}, values={1: {2: 3}}, start_times={1: 0.0})
        save.wildcat_slots = gambling
        save.cat_scratcher = types.SimpleNamespace(completed={}, values={}, start_times={})
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_gacha_stepup_duration_is_critical(self):
        save = self.make_save()
        save.gatya = types.SimpleNamespace(
            rare_seed=0, normal_seed=0, event_seed=0, stepup_stage_3_cooldown=0,
            previous_normal_roll=0, previous_normal_roll_type=0, previous_rare_roll=0,
            previous_rare_roll_type=0, trade_progress=0, unknown1=False,
            roll_single=False, roll_multi=False, step_up_stages={},
            stepup_durations={1: float("nan")},
        )
        self.assertIn("invalid_gacha_stepup_duration", self.codes(audit_save(save)))

    def test_talent_orb_key_mismatch_is_critical(self):
        save = self.make_save()
        save.talent_orbs = types.SimpleNamespace(orbs={1: types.SimpleNamespace(id=2, value=0)})
        self.assertIn("talent_orb_key_mismatch", self.codes(audit_save(save)))

    def test_map_reset_timestamps_are_checked(self):
        save = self.make_save()
        save.map_resets = types.SimpleNamespace(data={
            1: [types.SimpleNamespace(yearly_end_timestamp=0.0, monthly_end_timestamp=0.0,
                weekly_end_timestamp=0.0, daily_end_timestamp=float("nan"))]
        })
        self.assertIn("invalid_map_reset_timestamp", self.codes(audit_save(save)))


    def test_top_level_typed_collections_are_checked(self):
        save = self.make_save()
        save.enemy_guide = [0, 1]
        save.achievements = [True, False]
        save.order_ids = ["order-a"]
        save.first_locks = {1: True}
        save.announcements = [(1, 2)]
        self.assertTrue(audit_save(save).ok, audit_save(save).findings)

    def test_invalid_top_level_boolean_list_is_critical(self):
        save = self.make_save()
        save.achievements = [True, 1]
        self.assertIn("invalid_save_boolean_list", self.codes(audit_save(save)))

    def test_invalid_first_lock_entry_is_critical(self):
        save = self.make_save()
        save.first_locks = {1: 1}
        self.assertIn("invalid_first_lock_entry", self.codes(audit_save(save)))

    def test_invalid_announcement_tuple_is_critical(self):
        save = self.make_save()
        save.announcements = [(1, "bad")]
        self.assertIn("invalid_announcement_entry", self.codes(audit_save(save)))

    def test_excessive_findings_are_bounded_and_fail_closed(self):
        save = self.make_save(tuple(None for _ in range(700)))
        report = audit_save(save)
        codes = self.codes(report)
        self.assertIn("audit_findings_limit_exceeded", codes)
        self.assertFalse(report.ok)
        self.assertLessEqual(len(report.findings), 501)


if __name__ == "__main__":
    unittest.main()
