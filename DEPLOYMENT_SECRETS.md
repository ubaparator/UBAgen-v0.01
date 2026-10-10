# UBAgen deployment secrets and runtime configuration

Do not commit a real `.env` file or any live credentials. The repository's GitHub Actions workflow only uses the deploy hook; the Discord bot token belongs in the **actual long-running host's runtime environment**.

## Required for a live Discord bot

- `DISCORD_BOT_TOKEN` — the bot token from Discord Developer Portal → Applications → your application → Bot. Set it as a secret/environment variable in the service that actually runs `python main.py`. Do not put the token in source code, a commit, Google AI Studio frontend code, or this document.
- `DEPLOY_WEBHOOK_URL` — the private deploy/restart hook URL supplied by the hosting provider. Add it under GitHub repository **Settings → Secrets and variables → Actions → Secrets**. The workflow now fails the deploy job if this secret is missing, rather than claiming deployment succeeded.
- `DEPLOY_WEBHOOK_METHOD` — a GitHub Actions **Variable** (not a secret), normally `POST`; set under Settings → Secrets and variables → Actions → Variables. Use `GET` only if the host's documented hook requires it.

## Optional runtime variables

- `DISCORD_GUILD_ID` — the numeric ID of the test Discord server; speeds up slash-command propagation during development. Set in the bot host's runtime environment, not in GitHub Actions unless the workflow explicitly passes it to the service.
- `PORT` — normally provided by the host; the app defaults to `8080`.
- `HOST` — defaults to `0.0.0.0`.
- `ENVIRONMENT` — informational label, e.g. `production`.

## Where each value goes

- **GitHub:** only `DEPLOY_WEBHOOK_URL` as a repository Actions secret and `DEPLOY_WEBHOOK_METHOD=POST` as an Actions variable.
- **Bot host (Render/Railway/Cloud Run/other actual runtime):** `DISCORD_BOT_TOKEN` as a protected runtime secret, plus optional runtime variables.
- **Google AI Studio:** do not paste the bot token into browser/frontend code. If AI Studio is only your editor, it is not the bot's runtime secret store.
- **Local development:** copy `.env.example` to `.env`, enter the token locally, and keep `.env` untracked.

## Important limitations

A Discord interaction attachment must first be accepted by Discord, so increasing the bot-side validation cap to 1.5 GB does not override Discord's account/server upload limit. For files larger than Discord accepts, use the command's `magnet_uri` option or a controlled external upload/storage flow.

The `/altyazi_ayikla` command processes files in a temporary directory, which is cleaned automatically when processing ends. The bot's successful result message is configured to disappear after six minutes; a slash-command attachment itself is not a bot-owned message that the bot can delete from Discord's UI.
