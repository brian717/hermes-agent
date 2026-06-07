---
name: proactive-monitor
description: Poll a source, LLM-classify urgency, surface only what matters.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [cron, monitoring, classifier, urgency, inbox, proactive, automation]
    category: productivity
    requires_toolsets: [terminal]
    related_skills: [watchers]
---

# Proactive Monitor

Turn a noisy stream into the handful of things worth interrupting the user for. Poll a source on an interval, score each candidate item with a cheap LLM, and deliver ONLY the items above an importance threshold. Quiet intervals stay silent.

This is the Poke "email monitor" pattern generalized: fetch → classify urgency → surface only above-threshold. Where the `watchers` skill answers "what's new?", this skill answers "what's new AND worth your attention right now?".

## When to Use

- "Text me only when an email actually needs my attention today"
- "Watch this feed but only ping me about the important stuff"
- "Monitor my alerts/PRs/mentions and filter out the noise"
- Any "notify me, but only if it matters" request

## Mental model

```
  [ fetch step ]          [ classify step ]              [ deliver step ]
  watcher / inbox dump  →  classify_items.py  →  above threshold? → deliver
  / API → JSON list        (scores 0-10)         below / none     → SILENT
```

The classifier (`scripts/classify_items.py`) reads a JSON list of candidate items on stdin, scores each against the user's plain-language criteria with a cheap model, and prints ONLY items at or above `--threshold`. Empty/below-threshold runs print nothing, so a wrapping cron job stays silent (no spam).

## The classifier model is configurable and cheap

Classification uses the `monitor` auxiliary task, so the model is set once in `config.yaml` and is independent of the main chat model:

```yaml
auxiliary:
  monitor:
    provider: openrouter          # or auto (uses main model)
    model: google/gemini-3-flash-preview   # cheap + fast; per-item scoring is high-volume
```

Leave it `auto` to use the main model. Set a small fast model for cost — per-item urgency scoring does not need a frontier model. Override interactively via `hermes model` → auxiliary → Monitor.

## Usage

### Standalone (test your criteria)

```bash
cat items.json | python $HERMES_HOME/skills/productivity/proactive-monitor/scripts/classify_items.py \
  --threshold 7 \
  --criteria "Urgent if it needs a reply today, is from my manager or family, or mentions a deadline"
```

`items.json` is a JSON list of objects (or `{"items": [...]}`). Helpful fields per item: `title`/`subject`/`summary`/`text`/`from` for judging, and any of `id`/`guid`/`url` for dedup. Output is one block per surfaced item; nothing is printed when nothing clears the bar.

`--format json` emits structured `{id, score, reason, item}` objects instead of text, for chaining.

### Wired to a fetch source via cron (the real use)

Pair it with a fetch step. The `watchers` skill provides ready fetch scripts that emit JSON. Ask the agent to schedule it:

> Every 10 minutes, run `watch_http_json.py --name inbox --url <feed> --id-field id` to fetch new items, pipe the JSON into `classify_items.py --threshold 7 --criteria "needs a reply today or is from my manager"`, and deliver whatever it prints. If it prints nothing, stay silent.

The agent composes the two scripts in its cron-job turn via the terminal tool. No new cron mode is needed — cron's existing empty-stdout / `[SILENT]` suppression handles the "stay quiet" half.

### Cheapest path (no agent turn): a wrapper script + no_agent cron

For zero per-tick token cost, write a small wrapper in `$HERMES_HOME/scripts/` that fetches AND pipes into `classify_items.py`, then schedule it as a `no_agent` cron job. The classifier still makes one cheap `monitor` call per tick; the cron harness delivers its stdout verbatim and stays silent on empty stdout.

## Thresholds

| Threshold | Behavior |
|---|---|
| 9-10 | Only true emergencies surface |
| 7-8 (default 7) | Important, time-sensitive items |
| 4-6 | Moderately noteworthy — expect more pings |
| 0-3 | Almost everything surfaces (defeats the purpose) |

Start at 7 and adjust. If the user gets too many or too few pings, change `--threshold`, not the criteria.

## Why classification, not just dedup

`watchers` dedups by ID — it tells you what changed. It cannot tell you whether a change matters. This skill adds the judgment layer: an LLM reads each item against the user's own words for "important" and gates delivery on the score. That judgment is the whole point of a *proactive* assistant.

## Pitfalls

- **Classifier failure is loud, not silent.** If the `monitor` LLM call fails, `classify_items.py` exits non-zero and prints the error to stderr — so a broken monitor surfaces (cron sends a watchdog alert) rather than silently swallowing urgent items.
- **Dedup is the fetch step's job.** This skill scores; it does not remember what it already surfaced. Pair with a `watchers` fetch script (which watermarks seen IDs) so the same item isn't re-scored and re-delivered every tick.
- **Keep criteria concrete.** "Important" is too vague. "From my manager, mentions a deadline, or asks a direct question" scores far more reliably.
- **Don't set the threshold below 4** unless you actually want most items through — at that point skip classification and use `watchers` directly.
