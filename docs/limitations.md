# Limitations

1. **Scope is registration and master data.** The project checks identity, LEI status, structure, manager and authorisation dates. It does not check PRIIPs KID, SFDR or EET content.
2. **Point-in-time snapshots that do not share a date.** The CBI registers were run on different dates, from 2026-09-02 to 2026-10-27. GLEIF is as at 2026-11-08. A fund authorised or revoked between those dates can show up as an exception.
3. **Fuzzy matching has measured error.**
   - Accepted fuzzy matches scored 86.7% strict precision on 30 reviewed pairs.
   - Exact methods showed 60 correct out of 60 sampled. That bounds their error rate at about 5.0% (95%, rule of three), not at zero.
   - Labels were assigned from names and register context only. They were not checked against prospectuses.
4. **Unmatched records are not proof that an LEI is missing.** 1,211 funds have no accepted match. Many are sub-funds with names too generic to match safely (for example "Balanced Fund"), or funds whose GLEIF name changed after a merger. E14 means "investigate", not "no LEI".
5. **Survivorship.** The registers list only funds that are currently authorised. "Authorisations by year" therefore counts the authorisation dates of today's funds, not historical authorisation flow.
6. **PDF extraction.** 11 of 12 control totals reconcile exactly, and the remaining variance is 1 row. Some fields are read from the first line of a cell only: a manager name that wraps onto a second line may be cut short, which can lower the manager-match score. The register's flag symbols are captured, but other footnotes, such as redomiciliation notes, are kept only as free text.
7. **Manager comparison.** GLEIF's fund-manager relationship sometimes names the delegate investment manager rather than the ManCo or AIFM. These pairs are reported as E11 and are not dropped. Names that share a brand but differ in translation or after a rename are reported as E18.
8. **GLEIF-side findings (E16)** cover Irish fund LEIs that match no current register entry. Some will belong to vehicles that are not CBI-authorised funds at all.
9. **No raw data in the repository.** The source PDFs and GLEIF extracts must be downloaded again to rerun. `data/manifest.json` lets you check that a rerun uses the same files.
