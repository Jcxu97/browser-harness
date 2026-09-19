# Google Flights — deep-link any search via `tfs` (incl. multi-city)

Do **not** drive the Google Flights UI (autocomplete dropdowns, date pickers, the
"Multi-city" mode switch). Everything is expressible as a URL:

```
https://www.google.com/travel/flights?tfs=<base64 protobuf>&hl=en&curr=USD
```

`tfs` is a base64 (standard alphabet, `=` padding stripped) serialized protobuf.

## Schema

```
Query {
  repeated Leg legs   = 3;
  repeated int32 pax  = 8;   // one entry per traveler: 1=adult 2=child 3=infant-in-seat 4=infant-on-lap
  int32 seat          = 9;   // 1=economy 2=premium economy 3=business 4=first
  int32 trip          = 19;  // 1=round trip 2=one way 3=multi-city
}
Leg {
  string date  = 2;          // "YYYY-MM-DD"
  Airport from = 13;
  Airport to   = 14;
}
Airport { string code = 2; }  // IATA airport OR city code
```

Multi-city = `trip=3` plus one `Leg` per segment, in order. Open-jaw is just a
2-leg multi-city where leg2's origin != leg1's destination.

Minimal encoder (no protobuf dep):

```python
import base64
def vint(n):
    o=b""
    while True:
        b_=n&0x7F; n>>=7; o+=bytes([b_|(0x80 if n else 0)])
        if not n: return o
def s(f,v): v=v.encode() if isinstance(v,str) else v; return vint(f<<3|2)+vint(len(v))+v
def v(f,n): return vint(f<<3)+vint(n)
def leg(d,a,b): return s(2,d)+s(13,s(2,a))+s(14,s(2,b))
def tfs(legs,seat=1,trip=3,adults=1):
    body=b"".join(s(3,leg(*l)) for l in legs)+v(8,1)*adults+v(9,seat)+v(19,trip)
    return base64.b64encode(body).decode().rstrip("=")

# JFK->LHR 2026-03-10, CDG->JFK 2026-03-24, 1 adult, economy, multi-city
tfs([("2026-03-10","JFK","LHR"),("2026-03-24","CDG","JFK")])
```

Always append `&hl=en&curr=USD` — otherwise currency/locale follow the browser
profile and prices come back in EUR etc.

Verify the URL parsed correctly by reading `page_info()["title"]` — it echoes
`"<Origin> to <Destination> | Google Flights"`. A malformed `tfs` silently falls
back to a blank search form.

## Reading results

No login needed. Results render ~5-8s after `wait_for_load()`; poll rather than
assuming. Every row is an `<li>`; the whole itinerary is in `li.innerText`:

```python
js("""(()=>{const o=[];document.querySelectorAll('li').forEach(l=>{
  const t=l.innerText; if(t&&/\\$\\d/.test(t)&&t.length<400) o.push(t.replace(/\\n+/g,' | '));
});return JSON.stringify(o)})()""")
```

Trailing rows in that list can be duplicated detail panes (much longer text) —
the `length<400` guard drops them. Prefer this over `aria-label` scraping; the
`jsname`/class attributes are obfuscated and rotate.

## Multi-city flow quirks

- The first results page lists **leg 1 only**. The price on each row is labelled
  `entire trip` and is Google's *estimate* of the cheapest complete itinerary
  containing that leg.
- Clicking a leg-1 row navigates (title flips to `"<Leg2 origin> to <Leg2 dest>"`)
  and shows the leg-2 options **compatible with that leg 1** — usually restricted
  to the same alliance, so the set is small (3-6 rows).
- **The leg-1 `entire trip` estimate can be below any actually selectable total.**
  Observed a leg-1 badge of `$1,073` whose cheapest real leg-2 pairing was
  `$1,107`. Always click through and read the leg-2 page for a bookable number.
- After clicking through, the `<li>` list needs another ~6s; an immediate query
  returns `COUNT 0`. Re-query rather than concluding there are no flights.
- `history.back()` returns to the leg-1 list with state intact.

## Sorting / expanding

There is no `Show more flights` button on multi-city pages — the list is complete
as rendered (`Top flights` + `Other flights`). To sort:

```python
js("[...document.querySelectorAll('button,div[role=button]')].find(x=>/Sorted by/i.test(x.innerText)).click()")
# then
js("[...document.querySelectorAll('[role=menuitem],li')].find(x=>x.innerText.trim()==='Price').click()")
```

Menu options: Top flights, Price, Departure time, Arrival time, Duration, Emissions.

## Cabin comparison

Re-request the same legs with `seat=2/3/4` rather than touching the cabin
dropdown. Mixed-cabin itineraries are labelled in-row
(`Economy + Premium Economy`, `Business Class + Economy`) and are frequently the
cheapest hit in a premium search — read that suffix before quoting a fare.

## Multi-city vs. two one-ways

Worth running both; they differ a lot. Same dates/route sampled 2026-07-25:
multi-city open-jaw `$1,107` vs. two separate one-ways `$640 + $844 = $1,484`.
Multi-city won by ~25%. Encode the one-ways with `trip=2` and a single leg.

## Traps

- Chrome's CDP consent dialog can block the harness against the user's main
  profile. Google Flights needs no session state, so launching a throwaway Chrome
  with `--user-data-dir=<tmp> --remote-debugging-port=<port>` and pointing
  `BU_CDP_WS` at it is a clean workaround.
- Prices are per the *whole* itinerary for 1 adult and exclude bag fees.
