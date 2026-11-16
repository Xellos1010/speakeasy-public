# Contributing to SpeakEasy

SpeakEasy is source-available under the Functional Source License 1.1 with Apache 2.0 Future License (FSL-1.1-ALv2). See
[LICENSE](LICENSE). Contributions are welcome. Every contributor must sign the SpeakEasy Contributor License Agreement (CLA)
before a pull request can be merged.

## Why there is a CLA

The project is released under FSL, and the licensor (Xellos1010) also offers commercial licenses and may relicense the code
in the future (for example when a version converts to Apache-2.0, or under a commercial license). To do that, the licensor needs
the right to license every contribution under any license. The CLA grants that right, while you keep the copyright in your
contribution. It also gives a patent license and records that you have the right to contribute the code.

The agreements are the Harmony Contributor Agreements, Version 1.0 (copyright license, outbound option "any license"):

- [CLA/INDIVIDUAL.md](CLA/INDIVIDUAL.md) (HA-CLA-I-ANY) for you as a person.
- [CLA/ENTITY.md](CLA/ENTITY.md) (HA-CLA-E-ANY) for a company or other legal entity.

Sources and checksums of the templates are in [CLA/SOURCES.md](CLA/SOURCES.md). Signed agreements are listed in
[CLA/SIGNATURES.md](CLA/SIGNATURES.md).

## How to sign

1. Open a pull request.
2. The CLA Assistant check posts a comment with a link. Follow it, sign in with GitHub, and accept the Individual CLA.
3. If you contribute on behalf of an employer or other entity, the entity must sign the Entity CLA instead (or first). Ask in the
   pull request or an issue and the maintainer will arrange it. If you are an employee, have your employer approve the
   Individual CLA or sign the Entity CLA.
4. Tick the checkbox in the pull request template. The check turns green once a signature is on record.

You sign once; the signature covers your later contributions to SpeakEasy. If the CLA text changes, you are asked to sign again.

**A DCO-style `Signed-off-by:` line is not a substitute for the CLA.** The DCO certifies origin; it does not grant the
licensor the right to relicense. Pull requests without a CLA signature are not merged, whether or not they carry a sign-off.

## Contributions you do not own

Only submit work that you wrote and have the right to license. Do not submit code copied from another project, from your
employer's code, or generated output you cannot license, unless the CLA covers it. If part of a change is not yours, say so in the pull request,
name its source and license, and leave it out unless the maintainer agrees to include it. Mark any communication that is not
meant as a contribution "Not a Contribution".

## Development

See [README.md](README.md) for setup. Run the unit tests with `npx nx run speakeasy:test` (or
`PYTHONPATH=. .venv/bin/python -m pytest -q`) before opening a pull request. Do not commit hostnames, addresses, personal paths or
secrets: deployment details belong in a deployment profile outside the repository (see [docs/PROFILE.md](docs/PROFILE.md)).
Commit messages follow Conventional Commits.

## Maintainer section: enabling CLA enforcement (CLA Assistant)

Enforcement uses the CLA Assistant GitHub App (https://cla-assistant.io, source https://github.com/cla-assistant/cla-assistant).
It is a GitHub App plus webhook, not GitHub Actions; this repository runs no workflows. Installing it needs the repository
owner's GitHub sign-in, so it is an owner step:

1. Create a public GitHub gist containing the CLA text. File 1: the contents of `CLA/INDIVIDUAL.md`. Keep the gist URL.
   (Optional, for entity signers: add a second gist for `CLA/ENTITY.md`, or add a `metadata` file to the gist with custom fields
   such as `company` and `title`, per https://github.com/cla-assistant/cla-assistant#custom-fields.)
2. Install the GitHub App: https://github.com/apps/cla-assistant -> Install -> choose the `Xellos1010` account ->
   "Only select repositories" -> `speakeasy-public`.
3. Go to https://cla-assistant.io, click "Configure CLA", sign in with GitHub, select `Xellos1010/speakeasy-public`, paste
   the gist URL, and Link. Add `Xellos1010` to the allowlist so the owner is not asked to sign.
4. In the repository settings, add "license/cla" as a required status check on `main` (branch protection).
5. Open a test pull request from a second account to confirm the check appears, then record the first real signature in
   `CLA/SIGNATURES.md`.
6. Do not enable GitHub Actions for this. If the CLA text changes, edit the gist; signers are asked to re-sign.
