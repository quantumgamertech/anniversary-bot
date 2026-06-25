# Legacy Bot

Legacy Bot is a Discord growth and analytics bot built for server owners who want clear visibility into community growth, milestone progress, Top.gg vote rewards, and premium growth reporting.

## Features

### Free features

- Daily growth tracking for joins, leaves, and net growth
- Free 7-day growth snapshot with `/analytics`
- Growth leaderboard and best growth day tracking
- Server milestone role configuration
- Top.gg vote link and vote reward tracking
- Temporary vote premium rewards
- Basic server status and bot statistics
- New-server setup guide with `/start` and `/setup`

### Premium features

- Growth dashboard with charted analytics
- Weekly growth analytics
- Daily growth report automation
- Live growth and drop alerts
- Custom alert thresholds
- Premium billing status and Lemon Squeezy checkout support

## Commands

### Public slash commands

- `/help` - View the help menu
- `/start` - View the quick-start setup guide
- `/analytics` - View a free 7-day growth snapshot
- `/growthtoday` - View today's growth stats
- `/growthleaderboard` - View top growth days
- `/premium` - Compare free and premium features
- `/premiumstatus` - View this server's premium/billing status
- `/vote` - Get the Top.gg vote link
- `/votestatus` - Check vote reward status
- `/ping` - Check bot latency

### Admin slash commands

- `/setup` - View setup guidance for server admins
- `/buypremium` - Generate a secure premium checkout link

### Premium slash commands

- `/dashboard` - View the premium analytics dashboard

Legacy Bot also supports prefix commands using `!`.

## Setup

1. Invite Legacy Bot to your Discord server.
2. Run `/start` or `/setup`.
3. Use `/growthtoday` or `/analytics` to confirm growth tracking is available.
4. Optional: configure milestone roles and vote reward roles.
5. Optional: upgrade with `/buypremium` to unlock premium analytics and alerts.

## Required Discord permissions

Legacy Bot may need the following permissions depending on which features you use:

- View Channels
- Send Messages
- Embed Links
- Attach Files
- Manage Roles, only if using milestone roles or vote reward roles

The bot role must be higher than any role it needs to assign.

## Webhooks

Legacy Bot supports:

- Top.gg vote webhook at `/topgg`
- Lemon Squeezy billing webhook at `/lemonsqueezy/webhook`
- Railway health route at `/health`

Webhook requests are authenticated before vote rewards or premium changes are processed.

## Premium billing

Premium checkout is handled through Lemon Squeezy. When a server admin purchases premium, the checkout includes server-specific metadata so premium can unlock automatically after payment.

Premium status may be managed through the server's premium status command when billing data is available.

## Privacy and terms

Please review:

- [Privacy Policy](PRIVACY.md)
- [Terms of Service](TERMS.md)

## Support

For support, billing questions, setup help, or data requests, join the official Legacy Bot support server:

https://discord.gg/7htnU8d2bm
