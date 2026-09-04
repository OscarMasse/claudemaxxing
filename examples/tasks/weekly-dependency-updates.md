---
title: Weekly dependency update pass across active side projects
project: side-projects
status: ready
priority: low
autonomy: private
model: sonnet
effort: low
recurring: true
token_budget: 250k
verification: Each bumped project builds and its test suite passes locally; the pass summary in Notes lists every bump with its test result.
created: 2026-08-15
---

## Context

A standing routine: once a week, walk the active side projects, bump patch and minor dependency versions, and run each project's tests.
Major version bumps are out of scope for the background pass: list them in Notes for the owner instead.

## Definition of done

Never `done` (recurring: true).
One pass is complete when every active project has either a committed bump with green tests, or a dated note explaining why it was skipped.

## Notes

Agent updates go here.
Each pass appends a dated summary: projects touched, versions bumped, test results, majors deferred to the owner.
