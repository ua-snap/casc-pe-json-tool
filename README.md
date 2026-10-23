# CASC PE JSON Tool

`casc_json_tool.py` is a command-line helper for hand-editing the CASC project records in `json/`. It lets you find, view, create, change, and delete items safely, keeps the companion `*_search.json` and `*_stub.json` files in step, and journals every change so it can be replayed after the data is regenerated.

The script uses only the Python standard library, so there are no packages to install beyond Python 3.9 or newer.

## Contents

- [Install Micromamba](#1-install-micromamba)
- [Create the environment](#2-create-the-environment)
- [How the data is organized](#how-the-data-is-organized)
- [Quick start](#quick-start)
- [Command reference](#command-reference)
- [Common workflows](#common-workflows)
- [Safety features](#safety-features)
- [Troubleshooting](#troubleshooting)

## 1. Install Micromamba

### macOS / Linux

With Homebrew (macOS):

```bash
brew install micromamba
```

Or with the official install script:

```bash
"${SHELL}" <(curl -L micro.mamba.pm/install.sh)
```

### Windows (PowerShell)

```powershell
Invoke-Expression ((Invoke-WebRequest -Uri https://micro.mamba.pm/install.ps1 -UseBasicParsing).Content)
```

### Enable shell integration

Restart your terminal, or initialize your shell once:

```bash
micromamba shell init --shell zsh --root-prefix ~/micromamba   # use bash instead of zsh if needed
```

Restart the terminal, then verify:

```bash
micromamba --version
```

## 2. Create the environment

From the repository root:

```bash
micromamba create -n casc-pe-json-tool -c conda-forge python=3.12 pip
micromamba activate casc-pe-json-tool
```

To remove the environment later:

```bash
micromamba deactivate
micromamba env remove -n casc-pe-json-tool
```

## How the data is organized

Each of the ten CASCs has three files in `json/`, named after its slug (for example `alaska_casc`):

| File                        | Purpose                                                                                |
| --------------------------- | -------------------------------------------------------------------------------------- |
| `<slug>_full_migrated.json` | The source of truth: a map of item id to full record. This is the file the tool edits. |
| `<slug>_search.json`        | Slimmed-down entries used by site search. Rebuilt from the migrated file.              |
| `<slug>_stub.json`          | Minimal id, title, body, and type entries. Rebuilt from the migrated file.             |

The `*_full_migrated.json` files are generated output (`casc_sb_cache.py` -> `*_full.json` -> `download_to_s3.py` -> `*_full_migrated.json`). Regenerating them discards manual edits, so every change made with this tool is also recorded in `manual_edits/overrides.json` and can be replayed with `apply-overrides`.

The tool creates the `manual_edits/` folder the first time it writes.

### Naming a CASC

Every command that takes a `casc` argument accepts any of:

- The slug: `alaska_casc`
- The short name: `alaska`, `north_central`, `pacific_islands`
- The full name: `"North Central CASC"`
- The ScienceBase id
- An unambiguous prefix: `nor` is ambiguous (North Central, Northeast, Northwest), `northe` is not

The valid CASCs are `national`, `alaska`, `midwest`, `north_central`, `northeast`, `northwest`, `pacific_islands`, `south_central`, `southeast`, and `southwest`.

### Naming an item

Commands that take an `id` accept the full 24-character id or any unique prefix of it. Use `list` or `find` to look ids up.

## Quick start

Run everything from the repository root with the environment active.

```bash
python casc_json_tool.py --help                      # all commands
python casc_json_tool.py fields                      # describe every record field
python casc_json_tool.py list alaska --limit 10      # see some items
python casc_json_tool.py find "permafrost"           # search all CASCs
python casc_json_tool.py show alaska 4f83 --field title
python casc_json_tool.py validate                     # check every file
```

Before making a real change, add `--dry-run` to see what would happen without writing anything.

## Command reference

The general form is:

```bash
python casc_json_tool.py [--json-dir DIR] <command> [arguments]
```

`--json-dir` points the tool at a different folder of JSON files (default: `json/`). It goes before the command.

### Read-only commands

#### `fields`

Prints every record field, its type, and what it does.

#### `list <casc>`

Lists items in one CASC, sorted by title.

| Option            | Meaning                                          |
| ----------------- | ------------------------------------------------ |
| `--contains TEXT` | Only titles containing `TEXT` (case-insensitive) |
| `--projects-only` | Only items whose `types` include `Project`       |
| `--limit N`       | Rows to show (default 50)                        |

```bash
python casc_json_tool.py list southeast --projects-only --contains fire --limit 20
```

#### `find <text>`

Searches the title, body, summary, DOI, and keywords of every CASC and prints matching ids grouped by CASC.

```bash
python casc_json_tool.py find "10.5066"
```

#### `show <casc> <id>`

Prints the full record as JSON. Use `--field` with a dot path to print one piece; numbers index into lists.

```bash
python casc_json_tool.py show midwest 5e2f --field dates.start_date
python casc_json_tool.py show midwest 5e2f --field images.0.url
```

#### `validate [casc]`

Checks every record in one CASC, or in all of them if no CASC is given. Errors are printed by default; add `--show-warnings` for advisory issues. It also reports items that are missing from the search file. The exit code is 1 if any errors are found, so it can be used in scripts.

```bash
python casc_json_tool.py validate northwest --show-warnings
```

#### `overrides`

Lists every journaled manual edit by CASC, with the action, time, user, and changed fields.

### Commands that change data

All of these accept:

| Option        | Meaning                                      |
| ------------- | -------------------------------------------- |
| `--dry-run`   | Validate and show the result without writing |
| `--note TEXT` | A short reason, saved in the journal         |

Each write updates `dates.updated`, saves the migrated file, rebuilds the item's entries in the search and stub files, and records the change in `manual_edits/overrides.json`.

#### `new <casc>`

Creates an item. With no options it prompts for title (required), description, status, fiscal year, dates, investigators, organizations, keywords, topics, subtopics, types (default `Project`), and parent id. It then offers to open the full record in your editor before saving. A 24-character id is generated for you.

To create an item from a prepared JSON file instead of prompting:

```bash
python casc_json_tool.py new alaska --from-file my_project.json --note "Added missing project"
```

The file must hold one record (see `fields`). If it has no `id`, one is generated. If it has no `csc`, the target CASC is filled in.

#### `edit <casc> <id>`

Opens the record in your editor as JSON. When you save and close, the tool checks that the JSON parses and that the record is valid. If something is wrong it lists the problems and offers to reopen the editor. Problems that already existed in the record do not block your edit. If nothing changed, nothing is written.

The editor is chosen from `VISUAL`, then `EDITOR`, then `code --wait`, `nano`, or `vi`, whichever is found first. The editor must block until the file is closed; for VS Code use:

```bash
export EDITOR="code --wait"
```

#### `set <casc> <id> --field PATH --value VALUE`

Changes one field without opening an editor. `PATH` is a dot path; numbers index into lists. Missing parent objects are created.

Plain values are stored as text, except `true`, `false`, and `null`, which become JSON values. Add `--json` to parse `--value` as JSON for lists and objects.

```bash
python casc_json_tool.py set alaska 4f83 --field status --value Completed
python casc_json_tool.py set alaska 4f83 --field dates.end_date --value 2025-09-30
python casc_json_tool.py set alaska 4f83 --field hasMaps --value false
python casc_json_tool.py set alaska 4f83 --field keywords --json --value '["permafrost", "tundra"]'
python casc_json_tool.py set alaska 4f83 --field contacts.principal_investigators.0.organization --value "USGS"
```

#### `delete <casc> <id>`

Shows the item and asks for confirmation, then removes it from the migrated, search, and stub files. Add `--yes` to skip the prompt.

#### `sync <casc>`

Rebuilds the search and stub entries from the migrated file. Use it after editing a migrated file by hand, or when `validate` reports items missing from the search file. Use `--id` to sync a single item.

#### `apply-overrides`

Replays every journaled edit onto freshly generated files, for all CASCs. Creates are re-added, patches are applied to the existing record, and deletions are removed again. Records that no longer exist or that would fail validation are skipped and reported. Use `--dry-run` first to preview.

## Common workflows

### Fix a typo or update a status

```bash
python casc_json_tool.py find "project title words"
python casc_json_tool.py set southwest 4f8c --field status --value Completed --note "Project closed" --dry-run
python casc_json_tool.py set southwest 4f8c --field status --value Completed --note "Project closed"
```

### Make several changes to one record

```bash
python casc_json_tool.py edit southwest 4f8c --note "Added co-investigators and DOI"
```

### Add a new project

```bash
python casc_json_tool.py new national
python casc_json_tool.py validate national --show-warnings
```

### After the data is regenerated

The generator scripts overwrite the migrated files, which discards manual edits. Replay them:

```bash
python casc_json_tool.py apply-overrides --dry-run
python casc_json_tool.py apply-overrides
python casc_json_tool.py validate
```

## Safety features

- **Backups:** before overwriting a file, a timestamped copy is saved in `manual_edits/backups/`. The 10 most recent copies of each file are kept. To undo a change, copy the backup over the file in `json/`.
- **Atomic writes:** files are written to a temporary file and then moved into place, so an interrupted run cannot leave a half-written file.
- **Validation first:** nothing is written if the edit introduces an error. Warnings are shown but do not block.
- **Journal:** `manual_edits/overrides.json` records who changed what and when. Commit it so edits are not lost.
- **Dry runs:** `--dry-run` is available on every command that writes.

## Troubleshooting

| Message                                               | Cause and fix                                                                                          |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `Unknown CASC 'x'`                                    | Use one of the CASC names listed above.                                                                |
| `'x' matches several CASCs` or `matches N ids`        | Type more of the name or id.                                                                           |
| `File not found: ...`                                 | The expected JSON file is missing from `json/`. Check the name, or pass `--json-dir`.                  |
| `... is not valid JSON`                               | A data file was damaged. Restore the latest copy from `manual_edits/backups/`.                         |
| `N error(s) found; nothing was written.`              | Fix the listed errors. Run `fields` to see the expected shape of each field.                           |
| `Item ... already exists`                             | Use `edit` or `set` instead of `new`.                                                                  |
| The editor closes immediately and nothing changes     | The editor returned before you finished. Use a blocking command such as `export EDITOR="code --wait"`. |
| `validate` reports items missing from the search file | Run `sync <casc>`.                                                                                     |
