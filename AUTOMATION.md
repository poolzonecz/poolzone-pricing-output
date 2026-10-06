# Daily encrypted pricing publication

Private `pricing.yml`: `0 22 * * *`, timezone `Europe/Prague` (DST handled by GitHub).
Public `pull.yml`: polling every five minutes. GitHub schedules may be delayed.
App token: Actions read-only for `poolzonecz/poolzone-pricing` only; no Contents permission.
Public GITHUB_TOKEN: writes only this repository. No PAT or private-to-public write token.

Only newest relevant pricing.yml/main run is considered. Running, failed, cancelled or unknown states do not publish and do not fall back to older runs. Successful run ID, SHA, exact artifact ID/name/digest are pinned. Six exact root Office Agile encrypted files must pass verification before one non-force main ref update. Last published run ID is stored in the public commit message, never in a main metadata file. Same/older runs skip before download. Unknown publication history blocks. Source is rechecked immediately before publication. Concurrent updates reject rather than overwrite.

`main` contains only six XLSX. Code/tests/workflow remain on `automation`.
No scanner/pricing dispatch is performed by the public workflow.

Offline tests: install requirements-test.txt plus production requests/olefile. Run `python -m unittest test_automation.Automation test_pull_regression.Tests`. Set PRIVATE_WORKFLOW_PATH to the private pricing.yml to include its schedule check.

Operational limitation: GitHub may disable scheduled workflows in public repositories after 60 days without repository activity. Normal daily publications keep activity fresh; prolonged failures require monitoring and re-enabling if necessary.
