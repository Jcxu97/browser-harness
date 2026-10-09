# Airbnb — monthly (28+ night) stay search via URL params

Search URL (no login needed):

```
https://www.airbnb.com/s/San-Francisco--CA--United-States/homes
  ?refinement_paths[]=/homes&checkin=2026-10-11&checkout=2026-11-11&adults=2
  &min_bedrooms=2&room_types[]=Entire home/apt
  &ne_lat=37.812&ne_lng=-122.405&sw_lat=37.778&sw_lng=-122.460&search_by_map=true&zoom=14
  &search_type=filter_change
  &price_max=7000&price_filter_num_nights=31          # optional
```

- With a 28+ night range, cards show a **monthly** figure ("$5,860 → $5,679 monthly") that
  already includes fees; `price_max` is applied to that monthly total, not per night.
- `ne_*/sw_*` + `search_by_map=true` pins the search to the box; the results header reads
  "N homes within map area".
- Cards: `a[href*="/rooms/"]`; the card text carries "Guest favorite", "Superhost",
  "New place to stay" (the newly-listed badge), beds/baths, and the struck/discounted price.
- When the exact dates return few homes, Airbnb appends flexible-date suggestions; their
  cards start with a date range ("Oct 17 to Dec 16") — treat those as *not available* for
  the requested dates.

## Pagination

The observed page contained 18 cards. The "Next" button click failed intermittently.
Use the cursor parameter with the original `search_url`, which contains filters but no pagination parameters.
Increase `page_number` for each page and check for repeated listing IDs:

```python
import base64, json
from urllib.parse import urlencode

page_number = 2
offset = (page_number - 1) * 18
cur = base64.b64encode(json.dumps({"section_offset": 0, "items_offset": offset, "version": 1}).encode()).decode()
page_url = search_url + "&" + urlencode({"cursor": cur, "items_offset": offset})
```

## Listing page

`https://www.airbnb.com/rooms/<id>?check_in=..&check_out=..&adults=2` — `document.body.innerText`
gives: "Entire rental unit in …", "N guests · N bedrooms · N beds · N baths", the location
blurb ("In Nob Hill, near Huntington Park", "Right in Chinatown, steps from…"), rating +
review count, Superhost/years hosting, "This home is in the bottom 10% of eligible listings"
warning (worth surfacing), cancellation text, and check-in window. Airbnb's own neighborhood
label on the card ("Condo in Nob Hill") can differ from the page blurb ("Right in Chinatown");
trust the page.
