# P8/P9 design notes: the agent loop

Findings from the tool-calling bake-off that belong in the runtime seam and the
builder, **not built yet**. Each is a real failure observed in a real run, with
the evidence, so the design is answering something that happened rather than
something imagined.

Background: `docs/decisions/0009-tool-calling-substrate.md` and
`docs/decisions/0010-difficulty-not-context.md`.

---

## 1. A stall is not a malformed call, and needs its own recovery path

**Observed.** `zai-org/glm-5.2` on T1 read three files and then returned an
assistant message with **no tool call and empty content**. Reproduced in three
separate runs. Given an explicit nudge — "you returned an empty response with no
tool call; continue or say you are done" — it returned an empty response again.
Nudge-resistant.

**Why it matters.** The bake-off harness originally treated "no tool call" as
"the task is finished", and recorded this as the model having decided no work
was needed. It had returned nothing at all. Those two point at opposite
conclusions, and a loop that cannot tell them apart will mark a stalled run
successful.

Reprompting with a schema error is also meaningless here. There is no schema
violation to report: the model emitted nothing to violate one.

**Recovery, in order, bounded:**

1. **One nudge.** Some models recover; this one did not.
2. **Fail over to the fallback model.** The routing table already carries one
   per role, and a stall is precisely the kind of failure a different model
   might not share — unlike a refusal, which it would.
3. **Fail the task.** Record the stall in the event log as a distinct outcome,
   not as a generic agent failure.

**Never unbounded retry.** A model that stalls twice will stall again, and each
attempt costs tokens for no output. The cap is the point.

**Note for the models seam.** `_FALLBACK_WORTHY` in `app/models/router.py`
currently covers `timeout`, `rate_limited` and `provider_error`. A stall is none
of those: it is a *successful* completion with empty content, so it never
reaches the fallback today. The runtime, not the router, is the right place to
handle it — the router sees one call, the runtime sees the loop.

---

## 2. A turn cap is not a no-progress detector

**Observed.** `zai-org/glm-5.3` on T2 — a one-line missing-import fix that every
other candidate completed in 4 to 5 calls — spent **25 turns making 24 `read`
calls and one `bash`**, changed nothing, and hit the turn cap. Zero malformed
calls throughout.

**Why a turn cap is not enough.** It fired, eventually, after paying for 25
turns of a loop that was visibly going nowhere by turn 5. A cap bounds the
damage; it does not detect the condition. It also cannot distinguish a model
working hard on something difficult from a model reading the same file for the
fourth time.

**What to detect.** Consecutive turns with **no `write` and no `edit`**. A run
that has not modified the workspace in N turns is not making progress on a task
whose definition of done is a modified workspace. Reading is legitimate — every
model reads before it writes — so the threshold has to allow a genuine
exploration phase; something like 8 to 10 turns, tuned against the bake-off
transcripts, which are the data for it.

Worth pairing with a cheaper signal: **repeated identical tool calls**. Reading
the same path twice is normal; four times is a loop.

On detection, treat it as a stall (path above) rather than a failure: the model
has not errored, it has stopped making progress, and a different model may not.

---

## 3. Out-of-scope edits are a hard failure in the builder, not a metric

**Observed.** `zai-org/glm-4.7-flash` on T4 edited `src/acme/store.py`, a file
no step of the task mentioned. It was the only candidate to do so, and it still
scored 5/6.

**Why it must fail the build, not be counted.** A builder works on one task in a
repository that other tasks depend on. A file changed outside the task's scope
is how one task quietly breaks another — and it does so *without failing its own
checks*, because its own checks do not exercise the file it broke. It surfaces
later, in a different task, as a failure with no obvious cause.

**Design.** The task declares the paths it may touch — the planner produces that
set, which is another reason decomposition quality matters (ADR 0010). After the
agent finishes, diff the workspace. Any modification outside the declared set
fails the task, and the diff goes in the event log so a human at the gate can
see exactly what was touched.

**Distinguish edits from additions.** The bake-off originally counted both, and
penalised four models for adding `tests/test_helpers.py` after writing
`helpers.py` — which is the behaviour we want. Editing a file the task never
mentioned is the problem. A *new* file outside the declared set is worth
surfacing at the gate, not failing on.

---

## Open question, carried

**Whether the planner should move to `deepseek/deepseek-v4-flash`.** Declined
for now — see `docs/notes/observations.md`. The bake-off measured tool-loop
discipline on coding tasks; the planner does almost no tool use. P8 produces
real specs, which is the right evidence, and it produces them anyway.
