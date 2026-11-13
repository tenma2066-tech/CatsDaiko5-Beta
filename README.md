# CatsDaiko5-Beta runtime data

This branch is reserved for encrypted runtime-state files used by the beta Render service.

- Keep this branch separate from the Render deployment branch.
- Store only encrypted database snapshots and encrypted PayPay session data.
- Never commit plaintext credentials, payment links, transfer codes, or customer information.
- The beta service must use its own encryption key and a repository-scoped GitHub token with Contents read/write access.
- Do not merge this branch into `main` or the stable `CatsDaiko5` repository.
