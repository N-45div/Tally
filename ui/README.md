# The stylesheet

Tally's pages are rendered on the server by Flask and Jinja (`tally/web/templates/`) and run no JavaScript, by design:
every page is served under `default-src 'none'`, with styles and images from Tally itself only (rule 8 in
`AGENTS.md`). This folder builds the one stylesheet those pages use, with Vite and Tailwind CSS. Nothing here is
loaded by the pages except its output.

```bash
cd ui
npm ci            # the exact versions in package-lock.json
npm run build     # writes ../tally/web/static/tally.css
```

Commit the built file with the change that needs it: the host installs Tally from the repository and has no Node.

- `src/tally.css` is the entry. Tailwind scans `tally/web/templates/` for the utility classes the templates use
  (`@source`), so a class added to a template appears in the stylesheet after the next build.
- No `url()` may point anywhere but Tally, and nothing may be inlined as a `data:` URI: the content policy refuses
  both. No template may carry a `<script>`, a `style="..."` attribute or an event handler.
