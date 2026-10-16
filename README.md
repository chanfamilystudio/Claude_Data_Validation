# Claude Data Validation

Metadata-driven reconciliation framework for Workday ↔ Great Plains (and, later, other sources), backed by SQL Server. Design spec: [`docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md`](docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md).

## Claude Code mods

Claude Code configuration for this project. `.claude/settings.json` registers the marketplaces below and enables these mods, so Claude Code offers to install them the first time you open this folder on your machine (or run `/plugin` and pick them from the **Discover** tab).

| Mod | Marketplace | What it does |
|---|---|---|
| `blast-radius` | [anthropics/claude-code-playground](https://github.com/anthropics/claude-code-playground) | Holds a risky shell command (`rm -rf`, force push) and shows what it would change, with Proceed / Cancel |
| `replay-theater` | [anthropics/claude-code-playground](https://github.com/anthropics/claude-code-playground) | `/replay` steps through the file edits Claude made in the last turn |
| `cache-tax` | [karanb192/claude-code-mods](https://github.com/karanb192/claude-code-mods) | Warns once before a cold-cache send with its price; `/keepwarm 6h` keeps the prompt cache warm |
| `file-tree` | [Fraktalis/claude-mods](https://github.com/Fraktalis/claude-mods) | Side pane with the project tree; files Claude reads, edits or creates light up, green once committed |
| `savvy-progress` | [JohnnyVizz/claude-kit](https://github.com/JohnnyVizz/claude-kit) | Progress bar above the prompt and a live subagents pane |
| `reflect-mod` | [BayramAnnakov/claude-reflect](https://github.com/BayramAnnakov/claude-reflect) | Checks each prompt for a correction and offers to save it as a rule in CLAUDE.md |
| `terminal-browser` | [zenbu-labs/terminal-browser](https://github.com/zenbu-labs/terminal-browser) | A browser pane beside the conversation. Needs the binary: `brew install terminal-browser` |
| `skins` | [hellosverre/claude-skins](https://github.com/hellosverre/claude-skins) | 15 transcript themes, card rendering, `/skin <name>`; ask Claude to design one (e.g. an Obsidian-style palette) |
| `cc-plugin-you-should-know` | built in | Side agent that watches long tasks and surfaces notes above the prompt |

To turn one off just for yourself, set it to `false` in `.claude/settings.local.json` (not committed).
