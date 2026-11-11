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

## Create a new mod page

A full script set (create the draft, fill each step, publish, check) is in the private repo `Jcxu97/bili-game-pipeline`, folder `tools/nexus`. The steps below are the parts of the form that cause problems.

1. Open `https://www.nexusmods.com/?uploadMod=true`. The "Upload mod" dialog on a game page can show "Something went wrong" and load no categories.
2. Fill `#mod-name` and `#short-description` in the dialog. Select the game and the category in the two comboboxes, then click `Create draft`. The page goes to `.../mods/<id>/edit/general`.
3. Wait until `#mod-version` and `window.sceditor` exist before you fill the General step.
4. "Generative AI usage" is required. Select the boxes that are true for the mod.
5. Media needs at least one gallery image. The page has two image inputs with the same `accept`. The first input is the header, the second input is the gallery.
6. Files: set the zip on `input[type=file][accept*=".zip"]`, wait for `#display-name`, set `#display-name` and `#file-version`, then click `Save file` when it is enabled.
7. Requirements: select "Mod requirements (legacy)" to add External resources (`#external-req-name`, `-url`, `-note`).
8. Click `Publish`, then `Publish mod` in the dialog. The URL ends with `?published=1`.

Headless UI checkboxes and radio buttons change on a plain `click()`. A full PointerEvent chain changes them two times, so the state does not change. Comboboxes are the opposite: click their options with the PointerEvent chain. To find a radio button, select `[id^=headlessui-radio-]`. The radiogroup element contains the same text and matches first.
