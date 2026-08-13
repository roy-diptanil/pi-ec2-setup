# Agent execution instructions

Execute these steps on the target Linux EC2 instance.

1. Read `README.md` and `bootstrap.sh` before executing anything.
2. Confirm the repository remote is `roy-diptanil/pi-ec2-setup` and the repository checkout contains no `auth.json`, session JSONL files, OAuth logs, or token caches.
3. Run:

   ```bash
   ./scripts/check-no-personal-tokens.sh
   ./bootstrap.sh
   ```

4. Verify that `node --version` is `v22.23.2`, `pi --version` is `0.84.1`, and `pi list` shows all three configured packages.
5. Do **not** request, read, echo, commit, or copy credentials. Tell the user to configure `OPENAI_API_KEY` through their existing secret-management mechanism, or use Pi's `/login` interactively.
6. Browser authentication cannot be completed unattended. Ask the user to start Pi and run, in order:

   ```text
   /mcp-auth glean
   /google-workspace-setup
   /google-workspace-doctor
   ```

7. For a remote EC2 callback, follow the URL/callback-copy instructions displayed by the relevant Pi command. Never paste an authorization URL, callback code, access token, or refresh token into this repository or an agent transcript unnecessarily.
8. Report the installed versions and which interactive authentication steps remain.

The bootstrap may install ordinary OS prerequisites with `sudo`, but it must not weaken firewall, IAM, SSH, credential-store, or file-permission settings.
