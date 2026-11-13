"""Offline GitHub Contents API fixture; no real credentials or GitHub writes."""
import base64
import io
import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from github_auth_store import GitHubPasswordStore, PasswordStoreUnavailable, _Conflict, _PutRejected


class FakeGitHub:
    def __init__(self):
        self.private = True
        self.data = None
        self.revision = 0
        self.conflict_once = False
        self.reads = 0

    def request(self, method, url, payload=None):
        if method == 'GET' and '/contents/' not in url:
            return {'private': self.private}
        if method == 'GET':
            self.reads += 1
            if self.data is None:
                return None
            return {'sha': str(self.revision), 'content': base64.b64encode(json.dumps(self.data).encode()).decode()}
        if self.conflict_once:
            self.conflict_once = False
            self.data['passwords'].append({'id': self.data['next_id'], 'label': '外部',
                                           'hash': self.data['passwords'][0]['hash']})
            self.data['next_id'] += 1
            self.revision += 1
            raise _Conflict()
        if (self.data is not None and payload.get('sha') != str(self.revision)) or (self.data is None and 'sha' in payload):
            raise _Conflict()
        self.data = json.loads(base64.b64decode(payload['content']))
        self.revision += 1
        return {'content': {'sha': str(self.revision)}}


class GitHubAuthTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.store = self.make_store(bootstrap=True)

    def make_store(self, bootstrap=False):
        store = GitHubPasswordStore('test/private-repo', 'fake-token', 'main', 'auth/passwords.json',
                                    'initial-password', bootstrap=bootstrap)
        store._request = self.api.request
        return store

    def test_restart_deletion_and_hash_only(self):
        initial = self.store.authenticate('initial-password')
        self.store.add('second', 'another-password')
        other = self.store.authenticate('another-password')
        self.store.delete(initial)
        restarted = self.make_store()
        self.assertIsNone(restarted.authenticate('initial-password'))
        self.assertEqual(restarted.authenticate('another-password'), other)
        self.assertNotIn('another-password', json.dumps(self.api.data))
        self.assertNotIn('initial-password', json.dumps(self.api.data))
        with self.assertRaises(ValueError):
            restarted.delete(other)
        restarted.add('third', 'third-password')
        self.assertGreater(restarted.authenticate('third-password'), other)
        self.assertFalse(restarted.contains(initial))

    def test_cache_reduces_reads_and_own_update_revokes_immediately(self):
        ident = self.store.authenticate('initial-password')
        reads = self.api.reads
        self.assertTrue(self.store.contains(ident))
        self.assertEqual(self.api.reads, reads)
        self.store.add('second', 'another-password')
        self.store.delete(ident)
        self.assertFalse(self.store.contains(ident))

    def test_conflict_reloads_and_keeps_both_updates(self):
        self.store.initialize()
        self.api.conflict_once = True
        self.store.add('new', 'another-password')
        self.assertEqual({label for _, label in self.store.list()}, {'初期パスワード', '外部', 'new'})

    def test_bootstrap_reloads_if_another_writer_created_file(self):
        def concurrent_create(method, url, payload=None):
            if method == 'PUT' and self.api.data is None:
                self.api.request(method, url, payload)
                raise _PutRejected()
            return self.api.request(method, url, payload)
        self.store._request = concurrent_create
        self.assertIsNotNone(self.store.authenticate('initial-password'))

    def test_missing_file_without_bootstrap_and_public_repository_fail_closed(self):
        with self.assertRaises(PasswordStoreUnavailable):
            self.make_store().authenticate('initial-password')
        self.api.private = False
        with self.assertRaises(PasswordStoreUnavailable):
            self.store.authenticate('initial-password')
        self.assertIsNone(self.api.data)

    def test_api_errors_do_not_fall_back_to_initial_password(self):
        self.store.initialize()
        self.store.cached = None
        with patch.object(self.store, '_request', side_effect=PasswordStoreUnavailable('offline')):
            with self.assertRaises(PasswordStoreUnavailable):
                self.store.authenticate('initial-password')

    def test_unavailable_github_returns_503_at_login(self):
        os.environ.setdefault('APP_PASSWORD', 'test-password')
        import app
        with patch.object(app, 'password_store', self.store), \
             patch.object(self.store, '_request', side_effect=PasswordStoreUnavailable('offline')):
            response = app.app.test_client().post('/login', data={'password': 'initial-password'})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('fake-token', response.get_data(as_text=True))

    def test_http_requests_use_contents_api_and_never_send_plaintext_passwords(self):
        store = GitHubPasswordStore('test/private-repo', 'fake-token', 'main', 'auth/passwords.json',
                                    'initial-password', bootstrap=True)
        requests = []
        def fake_urlopen(req, timeout):
            self.assertEqual(timeout, 10)
            self.assertEqual(req.get_header('Authorization'), 'Bearer fake-token')
            body = json.loads(req.data) if req.data is not None else None
            requests.append((req.method, req.full_url, body))
            result = self.api.request(req.method, req.full_url, body)
            if result is None:
                raise HTTPError(req.full_url, 404, 'missing', {}, None)
            return io.BytesIO(json.dumps(result).encode())
        with patch('github_auth_store.request.urlopen', side_effect=fake_urlopen):
            self.assertIsNotNone(store.authenticate('initial-password'))
            store.add('second', 'another-password')
        puts = [body for method, _, body in requests if method == 'PUT']
        self.assertEqual(len(puts), 2)
        self.assertIn('sha', puts[1])
        self.assertIn('/contents/auth/passwords.json', requests[-1][1])
        self.assertNotIn('initial-password', json.dumps(puts))
        self.assertNotIn('another-password', json.dumps(puts))
        self.assertNotIn(b'another-password', base64.b64decode(puts[1]['content']))

    def test_flask_admin_uses_github_store(self):
        os.environ.setdefault('APP_PASSWORD', 'test-password')
        import app
        client = app.app.test_client()
        with patch.object(app, 'password_store', self.store), patch.dict(os.environ, {'ADMIN_PASSWORD': 'admin-test'}):
            self.assertEqual(client.post('/login', data={'password': 'initial-password'}).status_code, 302)
            client.get('/admin')
            with client.session_transaction() as session:
                csrf = session['admin_csrf']
            self.assertEqual(client.post('/admin', data={'action': 'unlock', 'csrf': csrf, 'passcode': 'admin-test'}).status_code, 302)
            with client.session_transaction() as session:
                csrf = session['admin_csrf']
            response = client.post('/admin', data={'action': 'add', 'csrf': csrf, 'label': 'second', 'password': 'another-password'})
            self.assertIn('追加しました', response.get_data(as_text=True))
            original = self.store.authenticate('initial-password')
            client.post('/admin', data={'action': 'delete', 'csrf': csrf, 'id': original})
            self.assertEqual(client.get('/api/state').status_code, 200)
            self.assertEqual(client.get('/console').status_code, 302)
            self.assertEqual(client.post('/login', data={'password': 'another-password'}).status_code, 302)


if __name__ == '__main__':
    unittest.main()
