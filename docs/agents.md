# Use Pocket with an agent

Pocket was built to be a good instrument for an AI agent to play. Connect your
agent to it, and the agent can capture passages, place cues, render
alternatives, inspect MIDI and read saved sets on your machine. It hands back
receipts you can check. You stay in charge of listening and of the musical
decisions.

This page assumes you've run [your first experiment](getting-started.md). If a
term is unfamiliar, see [key ideas](concepts.md).

## 1. Install the agent interface

From your Pocket checkout, with its virtual environment active:

```sh
python -m pip install -e '.[agent]'        # adds the pocket-mcp server
python -m pip install -e '.[agent,midi]'   # also reads and writes MIDI files
```

## 2. Connect your agent

Pocket speaks [MCP](concepts.md#one-implementation-three-ways-in), the open
standard many AI assistants use to call local tools. The server runs on your own
machine and communicates over standard input and output. Nothing is hosted, and
no audio is uploaded.

Add Pocket to your MCP client's server list. Use the absolute path to the
executable inside your virtual environment:

```json
{
  "mcpServers": {
    "pocket": {
      "command": "/absolute/path/to/pocket/.venv/bin/pocket-mcp",
      "env": { "POCKET_ERROR_FORMAT": "v3" }
    }
  }
}
```

The `env` line is optional. It switches on machine-readable errors, which are
easier for an agent to act on. With `v3`, each error can also carry a `hint` that
names the argument to supply or the call to make first. `v2` still works and stays
unchanged (see [contracts and errors](contracts.md)). Restart your client after
you change its configuration, and again after you update Pocket, because a running
server doesn't pick up new tools.

To check that the server starts, run `.venv/bin/pocket-mcp` in a terminal. It
waits silently for a client to talk to it. Press Ctrl-C to stop it.

## 3. Give your agent the playbook

The [Pocket skill](../skills/pocket/SKILL.md) is a working guide written for
agents. It covers which tool to reach for, the order to call them in and the
distinctions to respect. If your agent supports skills stored as folders with a
`SKILL.md` file, copy or link `skills/pocket` into its skills folder. Otherwise,
share the file with it at the start of a session.

## 4. Try a first conversation

Point your agent at the store from your first experiment and ask it something
small:

> List Pocket's capabilities and tell me which ones work without Ableton Live.

> In `private/first-practice/store`, resolve the internal cue during the repeat
> and explain what the result means.

> Look at seconds 30 to 45 of `private/my-recording.wav` with Peek. Tell me
> which pulse rates are plausible, and don't decide where bar one is.

> Prepare a practice comparison of these two passages. Don't record any
> listening report; I'll listen myself.

Good requests name the exact files, say what should stay unchanged and ask one
musical question at a time.

## What your agent will and won't do

- **It works with your file access.** The server can read and write whatever
  your user account can. Give it a dedicated working folder, such as the
  ignored `private/` folder in the checkout, and keep your originals elsewhere.
- **It doesn't reach out on its own.** Downloading recordings, loading models,
  writing Spotify playlists and working inside Ableton Live each need explicit
  setup and a request from you. Apart from the optional acquisition and Spotify
  tools, Pocket makes no network requests.
- **Its reports are labeled as its own.** An agent's analysis is recorded with
  `actor_kind: "agent"`. Only a person can record human listening, and the
  [practice review page](practice-review.md) makes that an explicit act.
- **Pocket's tools don't overwrite your work.** They write new artifacts and
  refuse existing destinations, so a confused request leaves the files you
  already have exactly as they were.

## When something refuses

Each refusal names a code. With `v3` errors, a `hint` gives advice like the last
column below, using the tool's own argument names.

| You see | What it means | What to do |
|---|---|---|
| `invalid_arguments` | An argument is misspelled, missing or the wrong type | Check the tool's input schema. Pocket doesn't convert values, so `"5"` isn't a number |
| `idempotency_conflict` | That request ID was already used with different inputs | Use a new request ID, or inspect the earlier request with `request_status` |
| `request_not_complete` | An earlier call with this ID was interrupted or failed | Inspect it with `request_status`, and don't remove its lock. A new attempt needs a new request ID |
| `unsupported_profile` | The input falls outside what this operation supports | Check `capabilities_list` and the guide's limits. Don't force the input to fit |
| `ambiguous_mapping` | A cue sits inside a passage that plays more than once | Name the occurrence with `occurrence_id`. `context_query` lists them |
| `stale_revision` | The evidence changed after you read it, or two revisions got mixed | Read the current revision and choose again from it |
| `locked_field` | The edit would change something that was locked | Change the edit so the locked field stays as it is. Removing a lock is your decision, not a fix |
| `source_mismatch` | Two inputs come from different recordings or renders | Pass inputs made from the same exact source |
| `evidence_mismatch` | Saved evidence no longer matches its record | Point at the complete store. Don't edit or move artifacts one at a time |
| A tool is missing | The client started before Pocket was installed or updated | Restart the MCP client |

When a write fails after it has started, Pocket keeps a record of that request ID,
so send the corrected call with a new one.

More recovery guidance is in [contracts and errors](contracts.md#recovery).
