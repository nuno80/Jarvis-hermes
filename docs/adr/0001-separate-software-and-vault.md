# ADR 0001: Separate software and personal vault

Status: accepted by the owner's creation of Jarvis-hermes and authorization to proceed.

Software, tests, specification and issues live in nuno80/Jarvis-hermes. Personal Markdown notes and preferences live in nuno80/jarvis-vault.
The software uses a configured local vault path; no nested clone, submodule or copied personal data is required.
A project note in Obsidian can link to software docs and issues. Technical specification remains canonical in this repository.
Runtime databases, secrets, logs, screenshots and checkpoints are kept outside both tracked repositories.
