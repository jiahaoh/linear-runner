`status-board.json` is what `runner.py status --board` prints for the batch that
`tests/reporting/test_board.py` runs with fake sessions and a fake Linear: DEV-1 was accepted after a
blocked review and one repair, DEV-2 is active in a repair whose session has no finish (it is left
out of the time and tokens), and DEV-3 is queued. Times, launch identifiers and commits are replaced
by fixed ones; the times keep their order. The test fails when the record no longer equals the file.
After an intended change of the record, rewrite the file with
`LINEAR_RUNNER_UPDATE_BOARD_EXAMPLE=1 python3 -m unittest tests.reporting.test_board` and commit it.
The progress board reads this same file in its own tests, so a change of the record shows there too.
