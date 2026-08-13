# Pi EC2 setup

Private, token-free bundle that reproduces Diptanil's Pi setup on a Linux EC2 instance.

## What it installs

- Node.js `22.23.2`
- Pi `0.84.1`
- Default model setting: `openai/gpt-5.6-sol`
- `@tintinweb/pi-subagents` `0.15.0`
- `pi-mcp-adapter` `2.24.0`
- Upstart Glean MCP configuration and Glean skill
- Upstart Google Workspace Pi package and its safety hooks

## Fast path

```bash
git clone git@github.com:roy-diptanil/pi-ec2-setup.git
cd pi-ec2-setup
./bootstrap.sh
```

HTTPS also works:

```bash
gh repo clone roy-diptanil/pi-ec2-setup
cd pi-ec2-setup
./bootstrap.sh
```

The bootstrap is idempotent and does not install or copy personal credentials.

## Give this to another Pi agent

After cloning, tell it:

> Read `SETUP.md` completely and execute it. Do not ask me for tokens or print credentials. Pause only when interactive browser authentication is required.

## Security boundary

This repository intentionally excludes:

- `~/.pi/agent/auth.json`
- OpenAI/Anthropic API keys and login tokens
- Glean OAuth tokens
- Google user OAuth tokens
- Pi sessions and OAuth logs
- generated model caches

The Google Workspace package includes UpstartClaw's desktop OAuth **client registration** file. It is not a user's access or refresh token; the package documentation records it as security-approved for repository inclusion under SSD-4711. User authorization is still required on each machine.

## Updating the snapshot

Update versions in `bootstrap.sh` and `config/settings.json`, replace the copied token-free resources, then review with:

```bash
git diff
./scripts/check-no-personal-tokens.sh
git commit -am "Update Pi setup snapshot"
git push
```
