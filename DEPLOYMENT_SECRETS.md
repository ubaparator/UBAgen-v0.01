# UBAgen secrets for Google AI Studio

Do not commit a real `.env` file or live credentials. This repository is run from Google AI Studio Build mode, and the public `*.ai.studio` URL is published separately from GitHub Actions.

## Required for the live Discord bot

- `DISCORD_BOT_TOKEN` — the bot token from Discord Developer Portal → Applications → your application → Bot. Add it as a server-side secret in the **same Google AI Studio app's Secrets panel**. Do not put it in browser/frontend code, a commit, the README, or a public issue. The Node server spawns `python3 main.py` and passes its environment to the worker, so the worker can read this variable.
- If using the local development workflow, put the token in a local `.env` file copied from `.env.example`; keep it untracked.

## Optional runtime configuration

- `DISCORD_GUILD_ID` — numeric ID of your test Discord server. Add it to the same AI Studio app's server-side secrets/environment settings if you want faster guild-scoped slash-command sync.
- `PORT` — provided by the hosting environment in production; Express uses port 3000 in development mode.
- `HOST` — Python backend defaults to `0.0.0.0`.
- `BACKEND_URL` — only needed if the Node server should query a separate Python backend. In the normal AI Studio Run path, `server.ts` starts its own Python worker on port 5050, so leave this unset.

## Which system to use

- **Google AI Studio → your app → Secrets:** put `DISCORD_BOT_TOKEN` here for the Run/published app's server-side environment.
- **GitHub → Settings → Secrets and variables → Actions:** no deploy webhook is required for the normal AI Studio workflow. GitHub Actions validates source code; it does not restart AI Studio Run or publish the public app.
- **GitHub repository:** source code only; never commit actual credentials.
- **AI Studio Run:** tests the current code in the Build workspace.
- **AI Studio Publish/Republish:** updates the public `*.ai.studio` app after you sync the intended code into AI Studio and test it.

## Correct update flow after a GitHub commit

1. Merge the reviewed pull request into the intended branch.
2. In the same AI Studio Build project, open Settings → GitHub and sync/pull the latest repository changes into the workspace. Review conflicts if prompted.
3. Run the app in AI Studio and confirm the Discord bot connects.
4. Use Publish/Republish to update the public `*.ai.studio` deployment.

A GitHub commit alone does not guarantee that an already-running Run session or the public deployment has restarted.

## Video upload limitation

The bot's 1.5 GB limit is an application-side validation limit. It does not override Discord's own upload cap for interaction attachments. For larger videos, use the command's `magnet_uri` input or implement a separate upload/storage flow. The successful result message is configured to disappear after six minutes; a command attachment is not a bot-owned Discord message that the bot can delete from the UI.
