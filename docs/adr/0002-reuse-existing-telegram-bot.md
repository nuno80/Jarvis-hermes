# ADR 0002: Reuse the owner's Telegram bot

Status: accepted for planning; runtime connection not verified.

The owner supplied a screenshot showing Hermes_bot, username @nuno_agent_bot.
Do not create a second bot. The screenshot establishes the bot identity only, not gateway health, its host or authorization configuration.
On return to the PC, inspect the existing Hermes installation and gateway, preserve its configuration, identify its actual profile, and integrate incrementally.
Token and authorized numeric user ID are configured locally. They are not requested in issue bodies or committed here.
A second competing long-polling consumer must not be started during setup. Test connectivity with the owner after inspecting the existing gateway.
