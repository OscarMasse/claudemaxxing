---
title: Refactor the image thumbnail pipeline into a single resize module
project: side-projects
status: ready
priority: medium
autonomy: private
model: sonnet
effort: low
token_budget: 200k
verification: All existing tests pass (npm test), plus a new unit test covering the three thumbnail sizes; visual spot-check of one generated thumbnail per size recorded in Notes.
created: 2026-08-20
prerequisites: research-static-site-generators
---

## Context

The gallery app generates thumbnails in three places (upload handler, backfill script, admin re-crop), each with its own resize code and slightly different quality settings.
Bugs get fixed in one copy and not the others.
The goal is one shared module with an explicit size/quality table, used by all three call sites.
This waits on `research-static-site-generators` (the demo `prerequisites:` key): the blog rebuild's chosen generator decides the image pipeline this module must stay compatible with, so touching the shared module first would mean redoing it once that decision lands.

## Definition of done

- A single `lib/thumbnails` module owns resize logic and the size/quality table.
- All three call sites import it; no duplicated resize code remains (grep proves it).
- Existing tests pass and a new test covers the three sizes.
- No behavior change: output dimensions and formats are identical to before.

## Notes

Agent updates go here (progress, resume points, questions for the owner).
