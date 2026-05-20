# ui-capture tests

`test_dryrun.py` covers everything that doesn't need a running emulator:

- static discovery against the real input project
- normalize() against a canned uiautomator XML fixture
- HTML rendering
- class-name → source file resolution
- feature attachment using real feature.json

Run from the repo root:

```bash
IOS2CJ_WORKFLOW_CONFIG=$(pwd)/workflow.config.json \
  python .claude/skills/ui-capture/tests/test_dryrun.py
```

For full end-to-end capture (requires emulator + app installed), edit
`output/workflow_output/ui/nav_script/nav_script_android.sh` and run:

```bash
IOS2CJ_WORKFLOW_CONFIG=$(pwd)/workflow.config.json \
  python .claude/skills/ui-capture/scripts/run.py --mode standalone
```
