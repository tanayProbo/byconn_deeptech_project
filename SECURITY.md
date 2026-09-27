# Security

## Reporting

Please report vulnerabilities privately to the maintainers rather than in a
public issue.

## Secrets

Credentials are read from the environment (or a local `.env`, which is
git-ignored) and never from code. The dashboard only ever shows the provider
and model name, never a key. CI runs [gitleaks](https://github.com/gitleaks/gitleaks)
on every push.

### Known exposure

Commit `700678e` hardcoded a Groq API key in `server.py` and `byconn/main.py`.
It was removed in `0b33370`, but it remains in the public git history, so
removing it from the code does not make it safe. **The key must be revoked in
the Groq console**; rewriting history would not help, because clones and forks
already have it. The CI secret scan starts after that commit so the old,
revoked key does not fail every build.
