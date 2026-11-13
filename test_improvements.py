"""Local tests of password management, reporting and game-data preparation."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('APP_PASSWORD', 'test-login-password')
import app as A
import engine
from auth_store import PasswordStore


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PasswordStore(Path(self.tmp.name) / 'auth.db', 'original-password')
        self.store_patch = patch.object(A, 'password_store', self.store)
        self.store_patch.start()
        self.client = A.app.test_client()

    def tearDown(self):
        self.store_patch.stop()
        self.tmp.cleanup()

    def login(self, password='original-password'):
        return self.client.post('/login', data={'password': password})

    def test_admin_password_management_persists_and_revokes_sessions(self):
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get('/admin').headers['Cache-Control'], 'no-store')
        with patch.dict(os.environ, {'ADMIN_PASSWORD': 'admin-test-secret'}):
            self.assertEqual(self.client.post('/admin', data={'action': 'add'}).status_code, 403)
            self.client.get('/admin')
            with self.client.session_transaction() as session:
                csrf = session['admin_csrf']
            response = self.client.post('/admin', data={'action': 'unlock', 'csrf': csrf, 'passcode': 'admin-test-secret'})
            self.assertEqual(response.status_code, 302)
            with self.client.session_transaction() as session:
                csrf = session['admin_csrf']
            response = self.client.post('/admin', data={'action': 'add', 'csrf': csrf, 'label': '予備', 'password': 'second-password'})
            self.assertIn('追加しました', response.get_data(as_text=True))
            ident = self.store.authenticate('original-password')
            self.client.post('/admin', data={'action': 'delete', 'csrf': csrf, 'id': ident})
            self.assertEqual(self.client.get('/api/state').status_code, 200)
            self.assertEqual(self.client.get('/console').status_code, 302)
            self.assertEqual(self.login('original-password').status_code, 200)
            self.assertEqual(self.login('second-password').status_code, 302)
            reopened = PasswordStore(self.store.path, 'original-password')
            self.assertIsNone(reopened.authenticate('original-password'))
            self.assertIsNotNone(reopened.authenticate('second-password'))
            with self.assertRaises(ValueError):
                reopened.delete(reopened.authenticate('second-password'))
            self.assertNotIn(b'second-password', self.store.path.read_bytes())

    def test_save_audit_hides_advisory_findings_outside_integrity_mode(self):
        from types import SimpleNamespace
        advisory = SimpleNamespace(severity='warning', path='save.lineups[0]',
                                   message='advisory-only')
        critical = SimpleNamespace(severity='critical', path='save.cats[1]',
                                   message='critical-corruption')
        audit = SimpleNamespace(cats_checked=10, critical_count=1, warning_count=1,
                                findings=(advisory, critical))
        ordinary = {'warn': [], 'log': []}
        A._report_save_audit(ordinary, audit, detailed=False)
        ordinary_text = '\\n'.join(ordinary['log'])
        self.assertIn('critical-corruption', ordinary_text)
        self.assertNotIn('advisory-only', ordinary_text)
        self.assertNotIn('注意 1 件', ordinary_text)
        self.assertEqual(ordinary['warn'], [])

        detailed = {'warn': [], 'log': []}
        A._report_save_audit(detailed, audit, detailed=True)
        detailed_text = '\\n'.join(detailed['log'])
        self.assertIn('critical-corruption', detailed_text)
        self.assertNotIn('advisory-only', detailed_text)
        self.assertIn('注意 1 件', detailed_text)
        self.assertEqual(detailed['warn'], ['セーブ構造検査: advisory-only'])

    def test_job_log_deduplicates_and_keeps_a_bounded_tail(self):
        job = {'log': []}
        A._log(job, '同じ通知')
        first = list(job['log'])
        A._log(job, '同じ通知')
        self.assertEqual(job['log'], first)
        for index in range(100):
            A._log(job, f'通知 {index}')
        self.assertLessEqual(len(job['log']), 60)
        self.assertTrue(job['log'][-1].endswith('通知 99'))

    def test_save_audit_emits_nothing_for_clean_or_advisory_only_ordinary_run(self):
        advisory = SimpleNamespace(severity='warning', path='save.lineups[0]',
                                   message='advisory-only')
        audit = SimpleNamespace(cats_checked=10, critical_count=0, warning_count=1,
                                findings=(advisory,))
        job = {'warn': [], 'log': []}
        A._report_save_audit(job, audit, detailed=False)
        self.assertEqual(job['log'], [])
        self.assertEqual(job['warn'], [])

    def test_snapshot_reports_automatic_changes_and_failed_items(self):
        class Save:
            cats = SimpleNamespace(cats=[SimpleNamespace(unlocked=0, gatya_seen=0, catguide_collected=False)])
            scheme_items = SimpleNamespace(to_obtain=[1, 2])
            unlock_popups = SimpleNamespace(popups={1: SimpleNamespace(seen=False)})
            @property
            def xp(self): return 1
            @xp.setter
            def xp(self, value): raise ValueError('invalid')
        save = Save()
        before = engine.snapshot(save)
        job = {'warn': [], 'log': []}
        A._apply_edits(job, save, ['XP'], [], [], {}, [])
        after = engine.snapshot(save)
        # Unrelated menu operations must not mark every character as seen in gacha/catalog.
        self.assertEqual((before['ガチャ表示済みキャラ'], after['ガチャ表示済みキャラ']), (0, 0))
        self.assertEqual((before['図鑑登録済みキャラ'], after['図鑑登録済みキャラ']), (0, 0))
        self.assertEqual((before['表示済みポップアップ'], after['表示済みポップアップ']), (0, 1))
        self.assertEqual((before['未取得の設定アイテム'], after['未取得の設定アイテム']), (2, 0))
        self.assertTrue(any('XP' in warning for warning in job['warn']))

    def test_catalog_does_not_mislabel_nearest_available_version(self):
        fake_core = SimpleNamespace(GameDataGetter=lambda *args, **kwargs: SimpleNamespace(version='15.7.2'))
        with patch.object(engine, 'core', fake_core), patch.object(A, '_gv_for', return_value='target'):
            with self.assertRaises(RuntimeError):
                A._build_names('jp', 'default', '15.8.0')

    def test_warmup_copies_completed_data_to_free_slots(self):
        root = Path(self.tmp.name)
        source = root / 'source' / 'jp' / '15.7.2'
        source.mkdir(parents=True)
        (source / 'downloaded').touch()
        (source / 'file.csv').write_text('fixture')
        getter = SimpleNamespace(get_game_data_dir=lambda: SimpleNamespace(to_str=lambda: str(root / 'source')))
        with patch.dict(os.environ, {'BCSFE_PARALLEL_ROOT': str(root / 'slots')}), \
             patch.object(A, '_get_bcsfe', return_value=('jp', 'gv')), \
             patch.object(A, '_build_names', return_value=({0: ['fixture']}, 'fixture')), \
             patch.object(engine, 'core', SimpleNamespace(GameDataGetter=getter)), \
             patch.object(A, 'MAX_CONCURRENT_JOBS', 2), patch.object(A, '_slots', [None, None]), \
             patch.object(A, '_effective_version', return_value='15.7.2'):
            self.assertTrue(A._warm_cache_lock.acquire(blocking=False))
            A._warmup_thread()
            self.assertEqual(A._warm_cache['prepared'], 2)
            for slot in range(2):
                self.assertEqual((root / 'slots' / str(slot) / 'data/game_data/jp/15.7.2/file.csv').read_text(), 'fixture')


    def test_cat_name_extraction_continues_after_missing_early_entries(self):
        cats = []
        for cat_id in range(5):
            cats.append(SimpleNamespace(id=cat_id, get_names_cls=lambda save: None))
        cats.append(SimpleNamespace(
            id=873,
            get_names_cls=lambda save: ["ロックこねこ", "ロックスターこねこ"],
        ))
        save = SimpleNamespace(cats=SimpleNamespace(cats=cats))

        names, method = engine.get_cat_names(save)

        self.assertEqual(names[873], ["ロックこねこ", "ロックスターこねこ"])
        self.assertEqual(method, "Cat.get_names_cls")
        self.assertEqual(len(names), 1)


    def test_individual_main_story_chapter_clear_changes_only_selected_chapter(self):
        class FakeChapter:
            def __init__(self):
                self.progress = 0
                self.stages = [
                    SimpleNamespace(clear_times=0, treasure=SimpleNamespace(value=0))
                    for _ in range(51)
                ]
            def clear_chapter(self):
                self.progress = 48
                for stage in self.stages[:48]:
                    stage.clear_times = 1
            def get_valid_treasure_stages(self):
                return self.stages[:48]

        options = {
            "日本編 第1章クリア": 0, "日本編 第2章クリア": 1, "日本編 第3章クリア": 2,
            "未来編 第1章クリア": 3, "未来編 第2章クリア": 4, "未来編 第3章クリア": 5,
            "宇宙編 第1章クリア": 6, "宇宙編 第2章クリア": 7, "宇宙編 第3章クリア": 8,
        }
        self.assertTrue(set(options).issubset(set(A.SUBS)))
        self.assertEqual(engine.MAIN_STORY_CHAPTER_OPTIONS, options)

        for option, selected_index in options.items():
            chapters = [FakeChapter() for _ in range(9)]
            save = SimpleNamespace(story=SimpleNamespace(get_real_chapters=lambda: chapters))
            engine.apply_subs_and_others(save, [option], [])
            for index, chapter in enumerate(chapters):
                expected = index == selected_index
                self.assertEqual(chapter.progress, 48 if expected else 0, option)
                self.assertEqual(chapter.stages[0].clear_times, 1 if expected else 0, option)
                self.assertEqual(chapter.stages[47].clear_times, 1 if expected else 0, option)
                self.assertEqual(chapter.stages[48].clear_times, 0, option)
                self.assertEqual(
                    [stage.treasure.value for stage in chapter.stages[:48]],
                    [3 if expected else 0] * 48,
                    option,
                )
                self.assertEqual(chapter.stages[48].treasure.value, 0, option)

class DeliveryTests(unittest.TestCase):
    setUp = ImprovementTests.setUp
    tearDown = ImprovementTests.tearDown
    login = ImprovementTests.login
    def test_sessions_separate_even_with_same_password_and_ip(self):
        from collections import deque
        self.login()
        other = A.app.test_client()
        other.post('/login', data={'password': 'original-password'})
        with patch.object(A, '_slots', ['busy']), patch.object(A, '_jobs', {}), \
             patch.object(A, '_pending_jobs', deque()), patch.object(A, '_active_transfers', set()):
            jid = self.client.post('/run', json={'items': ['XP']}).get_json()['job']
            self.assertEqual(self.client.get('/status/' + jid).status_code, 200)
            self.assertEqual(other.get('/status/' + jid).status_code, 404)
            self.assertEqual(other.post('/cancel/' + jid).status_code, 404)
            self.assertEqual(len(A._pending_jobs), 1)
            self.assertEqual(self.client.post('/cancel/' + jid).status_code, 200)
            self.client.get('/logout')
            self.login()
            self.assertEqual(self.client.get('/status/' + jid).status_code, 404)

    def test_error_log_omits_all_untrusted_fields(self):
        from unittest.mock import Mock
        logger = Mock()
        result = SimpleNamespace(response=SimpleNamespace(status_code=403,
            headers={'secret': 'RESPONSE_SECRET'}, content=b'BODY_SECRET', reason='REASON_SECRET'),
            url='https://example.test/URL_SECRET', headers={'authorization': 'AUTH_SECRET'}, data='SAVE_SECRET')
        with patch.object(engine.core, 'core_data', SimpleNamespace(logger=logger)):
            engine.core.ServerHandler.log_error('upload_fail_transfers', result)
            engine.core.ServerHandler.log_error('SECRET_KEY', result)
        text = str(logger.mock_calls)
        self.assertIn('403', text)
        self.assertIn('upload_fail_transfers', text)
        for secret in ('RESPONSE_SECRET', 'BODY_SECRET', 'REASON_SECRET', 'URL_SECRET', 'AUTH_SECRET', 'SAVE_SECRET', 'SECRET_KEY'):
            self.assertNotIn(secret, text)

    def test_issue_once_and_preserve_partial_result(self):
        from unittest.mock import Mock
        for outcome in (None, RuntimeError('SECRET_TRANSPORT')):
            handler = Mock()
            if isinstance(outcome, Exception):
                handler.get_codes.side_effect = outcome
            else:
                handler.get_codes.return_value = outcome
            job = {'result': {'original': {'transfer': 'synthetic'}}}
            with self.assertRaises(RuntimeError) as error:
                A._issue_codes_once(job, handler, '複製')
            handler.get_codes.assert_called_once_with(upload_managed_items=False, tries=1)
            self.assertNotIn('SECRET_TRANSPORT', str(error.exception))
            self.assertTrue(job['outcome_unknown'])
            self.assertEqual(job['result']['original']['transfer'], 'synthetic')

    def test_actual_sdk_does_not_retry_failed_upload(self):
        handler = engine.core.ServerHandler(SimpleNamespace(show_ban_message=False), print=False)
        with patch.object(handler, 'get_auth_token', return_value='synthetic'), \
             patch.object(handler, 'get_save_key', return_value={'key': 'synthetic'}), \
             patch.object(handler, 'upload_save_data', return_value=False) as upload:
            with self.assertRaises(RuntimeError):
                A._issue_codes_once({}, handler, 'fixture')
            self.assertEqual(upload.call_count, 1)

    def test_latest_catalog_success_and_failure_preserves_previous(self):
        from unittest.mock import Mock
        old_names = {'state': 'ready', 'version': '15.7.2', 'items': [{'n': 1, 'name': 'old'}]}
        response = Mock(status_code=200)
        response.json.return_value = {'versions': {'jp': {'15.9.0': 'a', '15.10.0': 'b'}}}
        with patch.object(A, '_names', old_names.copy()), patch.object(A, '_learned', {'version': None}), \
             patch.object(A, '_NAMES_FILE', str(Path(self.tmp.name)/'names.json')), \
             patch.object(A, '_VERSION_FILE', str(Path(self.tmp.name)/'version.json')), \
             patch.object(A, '_catalog_update', {}), \
             patch.object(A, '_get_bcsfe', return_value=(engine.core.CountryCode.from_code('jp'), None)), \
             patch.object(engine.core.RequestHandler, 'get', return_value=response), \
             patch.object(engine.core.GameDataGetter, 'repo_url', return_value='https://example.test/metadata'), \
             patch.object(A, '_build_names', return_value=({0: ['new', 'form'], 1: ['added']}, 'fixture')) as build:
            A._run_lock.acquire()
            A._latest_catalog_thread()
            self.assertEqual(build.call_args.args[2], '15.10.0')
            self.assertEqual(A._catalog_update['status'], 'ready')
            self.assertEqual(A._names['version'], '15.10.0')
            self.assertEqual(len(A._names['items']), 2)
            before = A._names.copy()
            build.side_effect = ValueError('SECRET_RESPONSE')
            A._run_lock.acquire()
            A._latest_catalog_thread()
            self.assertEqual(A._catalog_update['status'], 'error')
            self.assertEqual(A._names, before)
            self.assertNotIn('SECRET_RESPONSE', A._catalog_update['error'])
            self.assertFalse(A._run_lock.locked())

    def test_latest_endpoint_is_public_but_respects_busy_lock(self):
        with patch.object(A, '_latest_catalog_thread', lambda: A._run_lock.release()):
            response = self.client.post('/api/cats/latest')
            self.assertEqual(response.status_code, 202)
        A._run_lock.acquire()
        try:
            self.assertEqual(self.client.post('/api/cats/latest').status_code, 409)
        finally:
            A._run_lock.release()


if __name__ == '__main__':
    unittest.main()
