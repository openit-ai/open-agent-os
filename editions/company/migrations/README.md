<!-- SPDX-License-Identifier: BUSL-1.1 -->
<!-- Company edition: see LICENSE-COMPANY at the repository root. -->
# Company migrations

`editions/company/lib/migrate.sh` applies these files. The version is the
three-digit file prefix; the recorded checksum is the SHA-256 of the file
content (the file name is not part of it). An applied file is never edited:
fixes ship as amendments (013+) whose first line is
`-- oaos-applied-with: N`, applied by every phase whose `--through` covers
base version N, through the maintenance account as the owner role.

## License

Every file in this directory is part of the Company edition and licensed
under the Business Source License 1.1 (`SPDX-License-Identifier: BUSL-1.1`,
see `LICENSE-COMPANY` at the repository root). Migrations 001-018 carry no
license header because adding one would change their recorded checksums on
installed E hosts; this README states their license. Later files (019+) and
`db-roles.sql` (applied on every run, not checksummed) carry the header.

## Renamed file

`011_console_g10_setup.sql` was `011_company_console.sql`. Its content and
checksum are unchanged, so an E that applied it under the old name keeps
verifying.
