# Claude Data Validation

Metadata-driven reconciliation framework for Workday ↔ Great Plains (and, later, other sources), backed by SQL Server. Design spec: [`docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md`](docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md).

## Quick start

Requires Python 3.11+ and the Microsoft ODBC Driver 18 for SQL Server.

1. **Install**

   ```bash
   python -m venv .venv
   .venv/bin/pip install .          # Windows: .venv\Scripts\pip install .
   ```

2. **Configure** the YAML files under `config/` (a few lines each; the files carry commented examples):
   `connection.yaml` (server and database, Windows authentication by default; or set the `RECON_DB` environment
   variable to a full ODBC connection string), `datasets.yaml`, `mappings.yaml`, `settings.yaml` (export, evidence
   and log folders) and `signers.yaml` (who may sign off which domain). Add rules as one YAML file each under
   `rules/<domain>/`.

3. **Create the schema** and check the rules:

   ```bash
   recon db migrate               # creates schema `recon` (re-run any time: prints "Up to date")
   recon rules validate           # validates config/ and rules/ without touching a database
   ```

4. **Run** a period, review the workbook, import the edits, sign off:

   ```bash
   recon run --all --period 2026-09        # or --domain payroll, --rule <id>; --period current | previous
   recon import exports/recon_all_2026-09_<run>.xlsx
   recon signoff --domain payroll --period 2026-09 --comment "September reviewed"
   ```

   Exit codes: `0` all checks passed, `1` breaks found, `2` error or refused command.

See [`docs/operations.md`](docs/operations.md) for installation on Windows, database grants, scheduling, locks,
sign-off evidence and local development, and [`docs/connectors.md`](docs/connectors.md) for adding a data source.
Tests: `pytest -m "not integration"` needs nothing; `pytest -m integration` needs `RECON_TEST_DSN`
(see the development section of the operations guide).

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
