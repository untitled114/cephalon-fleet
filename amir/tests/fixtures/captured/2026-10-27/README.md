# Captured source response

The real response of the official forum's "PC Update Notes" RSS feed,
https://forums.warframe.com/forum/3-pc-update-notes.xml/ (the same source as the 2026-10-24
capture), recorded on 2026-10-27 between 15:05 EDT, when its newest item (Hotfix 44.1.4) was
posted, and 15:37 EDT, when it was committed. Its full patch-notes text is what
`tests/publishing/test_structured_excerpt.py` tests the structured excerpts on.
Never hand-edit it.

Public copy: some characters in these captures are written as escapes (JSON's \uXXXX, XML's &#N;); every file decodes to the recorded text.
