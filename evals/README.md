# evals

Cases that pin down what the agents are expected to produce, so a prompt change
is a measurable change rather than a vibe.

    make eval

Each case is a directory under `evals/cases/` containing:

    case.yaml       the brief, the stage, and what must hold
    expected.md     optional: a reference output, for eyeballing a diff

A case is not a unit test. It asserts properties of a generated artefact --
that the spec has numbered acceptance criteria, that it names its open
questions, that the test stage refuses to pass an untested criterion -- not
that a particular sentence appears.

Cases cost real tokens. `make eval` reports the spend per case.
