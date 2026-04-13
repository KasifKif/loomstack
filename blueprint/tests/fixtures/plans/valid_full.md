# MeshCord

Some prose here that should be ignored by the parser.

## Task: MC-001 Design CRDT schema for channel state
role: architect
acceptance:
  pr_opens_against: main
  diff_size_max: 800
  docs_updated: true
  human_pr_approval: true
tags: [design, breaking_change]
human_review: true
notes: >
  Propose Automerge document schema. Must address concurrent edits,
  message ordering, offline reconciliation.

## Task: MC-002 Bootstrap Cargo workspace
role: code_worker
depends_on: [MC-001]
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 300
  tests_added: true
tags: [infra]
max_retries: 3
timeout_s: 900

## Task: MC-003 Implement libp2p transport layer
role: code_worker
depends_on: [MC-002]
context_files:
  - CONTEXT/ARCH.md
acceptance:
  pr_opens_against: main
  ci: passes
  diff_size_max: 400
  tests_added: true
  spec_compliance: true
escalate_if:
  - retries > 2
  - tag: security
tags: [security, cross_crate]
human_review: true
