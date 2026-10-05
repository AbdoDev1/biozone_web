# Contributing — Biozone Web frontend (Tailwind)

> Rule source of truth for every new HTML page. `AGENTS.md` (repo root,
> git-ignored) is a local helper only — this file travels with the code.

## Tailwind and new pages

Tailwind CSS is built locally from the central `tailwind.config.js`.
Do not add `cdn.tailwindcss.com`, inline Tailwind configuration, or
page-specific Tailwind configuration.

Every new HTML page must live under a configured `content` path:

- `biozone_web/www/**/*.html`
- `biozone_web/templates/**/*.html`

and must reference the generated local CSS asset
(`/assets/biozone_web/css/app.<hash>.css`). Tailwind classes must remain
statically discoverable in HTML/templates/partials. Do not edit generated
CSS manually. `not-available.html` is intentionally unstyled (allowlisted).

After adding or modifying a page:

```bash
bash scripts/build-css.sh
bash scripts/check-frontend.sh
git diff --check
```

`build-css.sh` rebuilds the CSS, fingerprints it, and repoints every
`<link>` automatically — never update `app.<hash>.css` names by hand.

Verify that:

- the page returns successfully;
- the local fingerprinted CSS is loaded;
- no CDN request or CDN reference exists;
- the browser console has no errors;
- the page is visually checked against the shared layout;
- the generated CSS and asset reference are included in the same change.

If a class must be assembled dynamically, add an explicit static
representation or an approved safelist entry and document why.
Dynamic assembly that Tailwind cannot statically see fails
`check-frontend.sh` by design.
