# Nexus Mods — Edit a mod page and replace its main file

`https://www.nexusmods.com/games/{game}/mods/{id}/edit/general` (details and description) and `.../edit/files` (files). The author must be logged in. Check the result on the public page `https://www.nexusmods.com/{game}/mods/{id}?tab=description` or `?tab=files`.

## Cloudflare blocks the editor scripts

When the edit page shows only a large image and no form, Cloudflare blocked its scripts. The scripts under `/_next/static/` answer 403 with `cf-mitigated: challenge`. Open one blocked script URL as a page once. The challenge passes in a real Chrome. Then load the edit page again.

```python
bad = js("""performance.getEntriesByType('resource')
  .filter(r => r.responseStatus === 403 && r.name.includes('/_next/static/'))
  .map(r => r.name)""")
if bad:
    goto_url(bad[0]); wait(5)
    goto_url(f"https://www.nexusmods.com/games/{game}/mods/{mod_id}/edit/general"); wait_for_load()
```

## Description and summary

- The summary is `textarea#short-description`. Set it with the prototype value setter and an `input` event, because React owns the field.
- The full description is an SCEditor (WYSIWYG mode). A change to its textarea is lost. Use the editor instance, which takes BBCode:

```python
js("""(() => {
  const t = [...document.querySelectorAll('textarea')].find(x => window.sceditor.instance(x));
  const ed = window.sceditor.instance(t);
  ed.val(window.__bbcode);   // set window.__bbcode with json.dumps first
  ed.updateOriginal();
})()""")
```

Then click the first `Save` button and check the public page.

## Replace the main file with a new version

1. On `.../edit/files`, the `Update` button of the file row is `button.nxm-button`. A tab of the category filter has the same text, so do not select by text only.
2. The button opens a native file chooser. Answer it with the chooser recipe in `interaction-skills/uploads.md`.
3. A form opens. `Update existing file` is selected and names the old file. Set these fields:
   - the checkbox `Archive existing file`: the old file moves to the File archive section, not to Old files
   - the checkbox `Update mod version to match this file's version`
   - `input#display-name`, `input#file-version`
   - `textarea#file-changelog-text`, one entry per line
4. `Save file` stays disabled for 10 to 20 seconds while the server processes the upload. Wait until it is enabled.
5. The public files tab shows "Virus scanning is in progress" for some minutes. Users cannot download the file before the scan ends.

## Traps

- When you leave the files page with an unsaved upload, Chrome shows a "Leave site?" prompt. See `interaction-skills/dialogs.md`.
- The checkboxes are headless UI elements (`role=checkbox`, `aria-checked`). Find them through their `label`, click them, and read `aria-checked` to verify.
