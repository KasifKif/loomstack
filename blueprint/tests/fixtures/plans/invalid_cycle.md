# Bad

## Task: MC-001 A depends on B
role: code_worker
depends_on: [MC-002]
acceptance:
  ci: passes

## Task: MC-002 B depends on A
role: code_worker
depends_on: [MC-001]
acceptance:
  ci: passes
