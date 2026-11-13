"""Store only salted password hashes in a private GitHub repository."""
import base64
import binascii
import json
import re
import threading
import time
from urllib import error, parse, request

from werkzeug.security import check_password_hash, generate_password_hash


class PasswordStoreUnavailable(RuntimeError):
    pass


class _Conflict(Exception):
    pass


class _PutRejected(Exception):
    pass


class GitHubPasswordStore:
    CACHE_SECONDS = 15
    MAX_PASSWORDS = 100

    def __init__(self, repository, token, branch, path, initial_password, bootstrap=False):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
            raise ValueError('AUTH_GITHUB_REPO は owner/repository 形式にしてください。')
        if not token or not branch or not path or any(p in ('', '.', '..') for p in path.split('/')):
            raise ValueError('GitHub認証情報または保存先の設定を確認してください。')
        self.repository, self.token, self.branch, self.path = repository, token, branch, path
        self.initial_password, self.bootstrap = initial_password, bootstrap
        self.lock = threading.RLock()
        self.cached = None
        self.cached_at = 0.0
        self.base_url = 'https://api.github.com/repos/' + repository
        self.content_url = self.base_url + '/contents/' + parse.quote(path, safe='/')

    def _request(self, method, url, payload=None):
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        req = request.Request(url, data=body, method=method, headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': 'Bearer ' + self.token,
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'catsdaiko-password-store',
            'Content-Type': 'application/json',
        })
        try:
            with request.urlopen(req, timeout=10) as response:
                return json.loads(response.read(2_000_001))
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            if method == 'PUT' and exc.code == 409:
                raise _Conflict from exc
            if method == 'PUT' and exc.code == 422:
                raise _PutRejected from exc
            raise PasswordStoreUnavailable('GitHubへの接続または権限を確認してください。') from exc
        except (OSError, ValueError) as exc:
            raise PasswordStoreUnavailable('GitHubからパスワード情報を取得できません。') from exc

    def _validate(self, data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise PasswordStoreUnavailable('パスワード保存データの形式が正しくありません。')
        rows, next_id = data.get('passwords'), data.get('next_id')
        if not isinstance(rows, list) or not rows or len(rows) > self.MAX_PASSWORDS or type(next_id) is not int:
            raise PasswordStoreUnavailable('パスワード保存データの形式が正しくありません。')
        ids = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1
                    or not isinstance(row.get('label'), str) or not 0 < len(row['label']) <= 80
                    or not isinstance(row.get('hash'), str) or not row['hash'].startswith('pbkdf2:sha256:')
                    or len(row['hash']) > 256 or row['id'] in ids):
                raise PasswordStoreUnavailable('パスワード保存データの形式が正しくありません。')
            ids.add(row['id'])
        if next_id <= max(ids):
            raise PasswordStoreUnavailable('パスワード保存データの形式が正しくありません。')
        return data

    def _save(self, data, sha):
        payload = {'message': 'Update login password hashes', 'branch': self.branch,
                   'content': base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()}
        if sha is not None:
            payload['sha'] = sha
        if self._request('PUT', self.content_url, payload) is None:
            raise PasswordStoreUnavailable('GitHubの保存先または権限を確認してください。')

    def _load(self, force=False):
        with self.lock:
            if not force and self.cached is not None and time.monotonic() - self.cached_at < self.CACHE_SECONDS:
                return self.cached
            repo = self._request('GET', self.base_url)
            if not isinstance(repo, dict) or repo.get('private') is not True:
                raise PasswordStoreUnavailable('保存先の非公開GitHubリポジトリと権限を確認してください。')
            response = self._request('GET', self.content_url + '?' + parse.urlencode({'ref': self.branch}))
            if response is None:
                if not self.bootstrap:
                    raise PasswordStoreUnavailable('GitHubのパスワード保存ファイルがありません。初期設定を確認してください。')
                data = {'version': 1, 'next_id': 2, 'passwords': [{
                    'id': 1, 'label': '初期パスワード',
                    'hash': generate_password_hash(self.initial_password, method='pbkdf2:sha256:600000'),
                }]}
                try:
                    self._save(data, None)
                except (_Conflict, _PutRejected):
                    pass
                # Read the SHA from GitHub even after a successful create.
                response = self._request('GET', self.content_url + '?' + parse.urlencode({'ref': self.branch}))
            try:
                raw = base64.b64decode(response['content'], validate=False)
                if len(raw) > 1_000_000:
                    raise ValueError('oversized')
                data = self._validate(json.loads(raw))
                sha = response['sha']
                if not isinstance(sha, str) or not sha:
                    raise ValueError('missing sha')
            except (TypeError, KeyError, ValueError, binascii.Error) as exc:
                raise PasswordStoreUnavailable('GitHubの保存データを読み取れません。') from exc
            self.cached = (data, sha)
            self.cached_at = time.monotonic()
            return self.cached

    def initialize(self):
        self._load()

    def list(self):
        data, _ = self._load()
        return [(row['id'], row['label']) for row in data['passwords']]

    def authenticate(self, password):
        if len(password) > 256:
            return None
        data, _ = self._load()
        for row in data['passwords']:
            if check_password_hash(row['hash'], password):
                return row['id']
        return None

    def contains(self, ident):
        data, _ = self._load()
        return any(row['id'] == ident for row in data['passwords'])

    def _update(self, change):
        with self.lock:
            for _ in range(3):
                data, sha = self._load(force=True)
                data = json.loads(json.dumps(data))
                change(data)
                try:
                    self._save(data, sha)
                except _Conflict:
                    self.cached = None
                    continue
                except _PutRejected as exc:
                    raise PasswordStoreUnavailable('GitHubが保存を拒否しました。設定と権限を確認してください。') from exc
                self.cached = None
                return
            raise PasswordStoreUnavailable('更新が競合しました。もう一度お試しください。')

    def add(self, label, password):
        if not label or len(label) > 80 or len(password) < 8 or len(password) > 256:
            raise ValueError('名前は80文字以内、パスワードは8〜256文字にしてください。')
        password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')
        def change(data):
            if len(data['passwords']) >= self.MAX_PASSWORDS:
                raise ValueError('登録できるパスワード数の上限に達しました。')
            if any(check_password_hash(row['hash'], password) for row in data['passwords']):
                raise ValueError('同じパスワードは既に登録されています。')
            data['passwords'].append({'id': data['next_id'], 'label': label, 'hash': password_hash})
            data['next_id'] += 1
        self._update(change)

    def delete(self, ident):
        def change(data):
            if len(data['passwords']) <= 1:
                raise ValueError('最後のパスワードは削除できません。先に追加してください。')
            rows = [row for row in data['passwords'] if row['id'] != ident]
            if len(rows) == len(data['passwords']):
                raise ValueError('パスワードが見つかりません。')
            data['passwords'] = rows
        self._update(change)
