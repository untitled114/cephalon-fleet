# Captured source responses

Real responses captured on 2026-10-24 21:21 EDT, while Baro Ki'Teer was present (he left 2026-10-25 13:00 UTC).
Never hand-edit these. Derive edge-case fixtures (malformed, truncated, missing fields) from copies.

| File | Source URL | HTTP |
|---|---|---|
| `forum_pc_update_notes.rss.xml` | https://forums.warframe.com/forum/3-pc-update-notes.xml/ | 200 |
| `official_worldstate_baro_present.json` | https://api.warframe.com/cdn/worldState.php | 200 |
| `wfcd_pc_baro_present.json` | https://api.warframestat.us/pc | 200 |
| `steam_news_230410.rss.xml` | https://store.steampowered.com/feeds/news/app/230410/ | 200 |
| `steam_getnewsforapp_announcements.json` | https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=230410&count=20&maxlength=300&feeds=steam_community_announcements | 200 |
| `wfcd_items_names.json` | https://api.warframestat.us/items/?only=uniqueName,name (captured 2026-10-24 ~21:40 EDT) | 200 |

Public copy: some characters in these captures are written as escapes (JSON's \uXXXX, XML's &#N;); every file decodes to the recorded text.
