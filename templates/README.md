# Human-review templates (DRAFT wording)

Linear comments that a person reads. Each file has a short front matter block (between
`---` lines) that is never posted, then the comment body.

* `author: runner` templates are filled from saved state. `{name}` fields are replaced;
  a paragraph whose fields are all empty is left out. `headline.<variant>`, and other
  `<group>.<variant>` entries, hold the wording the runner chooses between.
* `author: worker` and `author: reviewer` templates (`draft-*.md`) show the model the
  shape of the draft it writes into its outbox. `<...>` lines are guidance to replace.
  `required:` names the sections a draft must have; any other heading must be one of
  the template's headings.

Every comment opens with one plain sentence saying what happened and what, if anything,
the owner needs to do. Details live in artifacts; a comment has at most one
`Evidence:` line with host paths. Agent-review contracts (the structured result and
review JSON schemas) are separate and are not rendered here.

`python3 render_samples.py` writes one sample per template to `docs/template-samples.md`.
