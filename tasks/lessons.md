# Lessons

- **Web UI: show everything, don't fold it away** (2026-10-05). Fold-out sections and
  "More columns"/"More filters" switches were removed again after a try: a longer page that
  shows all sections, columns and filters is easier to use. To lighten a page, reorder it or cut
  what is not needed; ask before adding any show/hide control.
- **Splicing templates: anchor on unique text.** `s.index("{% endblock %}")` found the title
  block's `endblock`, not the content one, and duplicated the page body. Match on a string that
  occurs once (and assert its count) before cutting a file.
