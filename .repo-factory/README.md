# Assistant Repository Factory

This private repository hosts a small GitHub Actions workflow that lets the connected assistant request new repositories without exposing a token in chat.

## One-time setup

1. In GitHub, open **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Generate a token for the account `tenma2066-tech`. Set an expiration date.
3. Grant only **Repository creation: Read and write**. Do not grant broad repository administration unless GitHub requires it.
4. In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.
5. Set the secret name to `REPO_FACTORY_TOKEN` and paste the token value there. Never paste the token into a chat or commit it to a file.
6. Ensure GitHub Actions are enabled for this repository.

The workflow checks for requests every 15 minutes (and can also be run manually from the Actions tab). New repositories are private by default and initialized with a README.

## Request format

Create a uniquely named JSON file under `.repo-factory/requests/pending/`:

```json
{
  "name": "Example-Repository",
  "description": "A short description",
  "private": true
}
```

- `name` is required and must be a valid GitHub repository name.
- `description` is optional (maximum 350 characters).
- `private` is optional and defaults to `true`.

After successful creation, the workflow moves the request record to `.repo-factory/requests/completed/` and records the new repository URL. Failed requests remain pending and appear in the Actions run log.

## Security notes

- The token is stored only as a GitHub Actions secret.
- The token should have only the Repository creation permission.
- Requests are processed from the default branch; do not accept requests from pull requests or untrusted branches.
- Do not store credentials or personal data in request JSON files.
