You are curating the owner's morning digest for the backlog system. Repo: {{BACKLOG_ROOT}}.
Slice: {{SLICE_MIN}} minutes, but this should take far less. Be concise.

Runs already journal their own progress live into {{DIGEST_FILE}} as they finish (a
markdown bullet per run under `## Runs`, plus a mechanical cost/duration line the
harness appends). Your job is not to reconstruct the night from logs: it is to
reorganize what is already written into a readable digest, then rewrite the file
in place.

Procedure:
1. Read {{DIGEST_FILE}}. If it does not exist yet or its `## Runs` section is empty,
   nothing ran: skip straight to step 4 with every section but `## Quota` set to "-".
2. Run: `python3 {{ORCH_DIR}}/gate.py status` and capture the output.
3. Read NEEDS-HUMAN.md for open questions, and skim the journal bullets in
   `## Runs` for anything that needs the owner's judgment (work to validate,
   questions, tasks completed).
4. Rewrite {{DIGEST_FILE}} in place with exactly these sections, in this order:
   - `## To validate` - work finished by background runs awaiting the owner's approval,
     drawn from the `## Runs` journal bullets.
   - `## Questions` - every unchecked item from NEEDS-HUMAN.md, one bullet each.
   - `## Done` - tasks completed autonomously, drawn from the journal bullets.
   - `## Quota` - the gate.py status output, one block per account (it includes
     per-task cost lines from each account's ledger: runs, USD, tokens - keep them,
     they inform planning), plus one line per account whose promo_until is within
     3 days: "Promo expires <date> - config falls back to 1.0 automatically."
   - `## Runs` - the raw journal, UNCHANGED, moved to the bottom of the file as the
     source record the sections above were built from.
   Keep the whole file readable in under ten minutes. If a section is empty
   (besides `## Runs`), write "-".
5. Commit the digest with message "digest: YYYY-MM-DD". Never push.
