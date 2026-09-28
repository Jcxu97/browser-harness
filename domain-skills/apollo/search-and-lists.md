# Apollo: saved searches and lists

## Routes and state

- Company search: `https://app.apollo.io/#/companies`; people search: `#/people`.
- Lists overview: `#/lists`; individual list: `#/lists/<list-id>`.
- Search filters are serialized in the hash query. Observed keys include `organizationNumEmployeesRanges[]` (comma-separated minimum and maximum), `organizationIndustryTagIds[]`, `organizationNotIndustryTagIds[]`, `qOrganizationKeywordTags[]`, `qNotOrganizationKeywordTags[]`, `marketSegments[]`, `intentIds[]`, and `page`.
- People searches can require a company list through `accountLabelIds[]` and current titles through repeated `personTitles[]`.
- Obtain industry IDs and list IDs from the current UI or an observed search URL; do not invent IDs.

## Saving searches without overwriting another one

Opening an AI-generated draft or navigating to a filtered URL can retain the previously selected saved-search name. The main **Save search** button updates that selected search. To create a separate search, open **Select view** and choose **Create saved search**.

To rename an existing search, open the view selector, hover its row, open the ellipsis, and choose **Edit**. The name field may already contain text; select the entire value before replacing it. Verify the name after the update completes.

## Selection and list creation

- Header selection input: `input[aria-label="View more options to select rows"]`.
- Row selection inputs: `input[aria-label="Select current row"]`.
- Selecting the **Select this page** radio collapses the numeric controls above it, moving **Apply** upward. Re-locate Apply after changing the radio.
- Bulk limits depend on the account. If the UI limits selection to one page, work page by page within that limit rather than changing the plan.
- For selected companies, **Add to list → Create new list** opens List Settings and can create the list with the selected records in one operation.
- People and company lists are distinct objects. A company list will not appear in the contact list picker.
- Saving previously unsaved contacts can open an email-enrichment dialog with a credit estimate. Review the estimate and options against the authorized scope. List creation and contact saving are not equivalent to starting outreach.

## Asynchronous and cached UI traps

Apollo is a React application. `wait_for_load()` does not guarantee that a search, list, or AI response has finished updating. Check the visible result and completion toast after each mutation.

After bulk company saves, the current search can temporarily show duplicate saved/unsaved rows and inflated counts. The lists overview can show zero records while the individual list already has records. Verify the individual list and, if needed, reload the page with `Page.reload` before interpreting counts. Navigating to the same hash URL may not reload the application.

Lists created in another tab may not immediately appear in an already-open list picker, even after closing and reopening the dialog. A full reload refreshes this cache, but loses unsaved selection. Finish or record the selection before reloading.

## Apollo AI search drafts

AI Assistant can generate clickable **Companies View (Draft)** and **People View (Draft)** cards. Its prose is not proof of the applied filters: open the card and inspect the filter chips and hash query. Streaming responses can move the card while an action is being prepared; re-check its position after generation finishes.

Keyword searches can include businesses merely serving an industry. An industry filter can improve precision but does not prove a business model, ownership of a platform, or an internal engineering team. Department headcount is only a sourcing signal. Treat such requirements as manual qualification, and do not label the resulting list fully qualified from filters alone.
