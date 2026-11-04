# PartsLoc public site

Static site for GitHub Pages, no scripts, no web fonts, no external requests (Bryan pays per
GB; a page that calls out would break that rule). One exception: a plain link to the Ko-fi tip page (`store/tests/test_site_external_refs.py` allows exactly that one URL).

- `index.html` product page (hero, problems, walkthrough, features, privacy, FAQ). Hand-written.
- `privacy.html` GENERATED from `docs/privacy-policy.md` by `store/build-pages.py`
  (CONTACT_EMAIL is replaced there). Edit the Markdown, rerun the script, commit both.
- `index.html` is now GENERATED too: `store/index.template.html` + `store/site_content.py`; edit those, run `python3 store/build-pages.py`.
- `index.html.md`, `privacy.html.md`, `llms.txt`, `llms-full.txt` for AI assistants (llmstxt.org convention), generated from the same sources so they cannot drift.
- `style.css` shared styling (icon colours, light/dark, reduced motion).
- `img/` screenshots as WebP (480x960, from `store/listing-drafts/screenshots/`, demo data only)
  and the icon SVG (icon A). Re-make a screenshot with `cwebp -q 72` after resizing to 480 wide.
- `.nojekyll` tells Pages to serve files as they are.

Every claim must be true of the STORE edition (no server sync, no login). The "two phones" FAQ
describes the merge-file flow only. Before publishing run the identity scrub over this folder.
