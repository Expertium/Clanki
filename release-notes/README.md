# Unreleased release notes, one file per change

Every user-visible change adds **one new file** here instead of editing
[`RELEASE.md`](../RELEASE.md): `release-notes/<short-topic>.md`, named after
the branch or the feature (for example `global-algorithm.md`). The file holds
the change's bullet points, written as they will appear in `RELEASE.md`:

```markdown
- The scheduling algorithm is one setting for the whole collection. ...
```

Why: when every pull request added its line at the top of the **Unreleased**
section, any two open pull requests conflicted in `RELEASE.md`. Separate files
never conflict.

A later change to the same feature edits that feature's file. A change that
undoes an unreleased one deletes or edits its file.

## At release time

Move the bullets of every file here, in the order the changes were merged,
into `RELEASE.md` (under **Unreleased**, before it is renamed to the version),
then delete the files. This README stays.
