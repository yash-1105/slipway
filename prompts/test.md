Test the application that was just built against its specification.

## Specification

{{spec}}

## Build output

{{build_log}}

## What to produce

1. **Coverage of acceptance criteria.** Each numbered criterion from the
   specification, and the test that exercises it. A criterion with no test is
   reported as untested, not quietly dropped.
2. **The tests themselves**, as files to write into the workspace.
3. **Findings.** Anything the build got wrong, with the criterion it violates.
4. **Verdict.** `pass` only if every acceptance criterion has a test and no
   finding contradicts one. Otherwise `fail`, with the reason.

Do not weaken or skip a test to reach `pass`. A `fail` here costs one loop; a
false `pass` reaches the deploy gate and then a client.
