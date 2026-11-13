# The stylesheet

Tally's pages are rendered on the server by Flask and Jinja (`tally/web/templates/`) and run no JavaScript, by design:
every page is served under `default-src 'none'`, with styles, images and fonts from Tally itself only (rule 8 in
`AGENTS.md`). This folder builds the one stylesheet those pages use, with Vite and Tailwind CSS. Nothing here is
loaded by the pages except its output.

```bash
cd ui
npm ci            # the exact versions in package-lock.json
npm run build     # writes ../tally/web/static/tally.css, fonts/ and favicon.svg
```

Commit the built files with the change that needs them: the host installs Tally from the repository and has no Node.

- `src/tally.css` is the entry. `src/tokens.css` holds every colour as a CSS variable, once for light and once for
  dark (`prefers-color-scheme`); the entry maps them to Tailwind colours, so a template can say `text-muted` or
  `bg-surface`. `src/base.css` styles bare elements, `src/components.css` the shared pieces (the shell, cards,
  buttons, status pills, tables, callouts, forms).
- Tailwind scans `tally/web/templates/` for the utility classes the templates use (`@source`), so a class added to a
  template appears in the stylesheet after the next build.
- Icons are inline SVG from `templates/_icons.html`, drawn by the `.icon` rules: nothing is fetched for them.
- The font is Inter (SIL Open Font License, `fonts/Inter-OFL.txt`), from `@fontsource-variable/inter`, latin and
  latin-ext only, served by Tally itself (`font-src 'self'`).
- No `url()` may point anywhere but Tally, and nothing may be inlined as a `data:` URI: the content policy refuses
  both. No template may carry a `<script>`, a `style="..."` attribute or an event handler.
