# Flat finder

One feed of rental flats from several UK property sites, scored against a personal brief
(1-2 bed in Islington, £1,700-£2,100 pcm, motorbike parking, double glazing, wood or laminate floors).

## How it works

- `scrape.py` reads `config.json`, pulls search results from Rightmove, OnTheMarket and OpenRent,
  opens each listing to read its full description, and detects parking (off-street, mentioned,
  on-street permit, none), double glazing and wood/laminate floors. Listings that appear on more
  than one site are merged into one entry. Output goes to `web/listings.json`, with small photos in
  `web/img/`.
- `web/index.html` is the page. `scrape.py` also copies the feed into it, so it opens without a
  second download; `listings.json` stays as a fallback. It lets you filter by parking, beds, price and
  postcode area, and keeps a shortlist, hidden flats and hand-added listings (for Facebook
  Marketplace and the like) in the published page's database.

Zoopla is not searched because it blocks automated access. Facebook Marketplace needs a login, so
those finds go in through the page's "Add a listing by hand" form.

## Refresh

A scheduled task ("Flat feed daily refresh") runs every morning at 07:37 London time in a fresh cloud
session: it runs `scrape.py`, publishes the page plus whatever `publish_files.py` lists, and commits
the result to this branch. The detail cache and photos are committed so each run only opens new
listings.

To refresh by hand:

```
cd flat-finder
python3 scrape.py
```

Listing details are cached in `.cache/` so a refresh only opens new listings. Pillow is optional; with
it installed the photos are shrunk to small JPEGs. After a refresh, republish `web/` (page,
`listings.json` and `img/`) to the same artifact.

Change the search by editing `config.json`.
