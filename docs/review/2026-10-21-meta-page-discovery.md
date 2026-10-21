# Meta Page discovery — 21 October 2026

The reported Facebook/Instagram callback failed with Graph error #100, "Tried accessing
nonexisting field (account_type)" while listing `/me/accounts`. The Page field expansion
incorrectly requested `account_type` on the Facebook Login IGUser schema, rejecting the
whole list rather than just one Instagram profile.

Removed that unsupported field from Page discovery, including cursor continuations. Keep
Facebook Page tasks, tokens and linked professional-profile identity fields. Permission
checks, app-secret proof, authorization headers and bounded pagination remain unchanged;
other Graph failures are still errors rather than silently empty successful connections.

The direct Instagram Login adapter still requests its own account type through
`graph.instagram.com`. Missing type information from Facebook Login remains unknown and
replaces stale stored type metadata when reconnecting. No Business status is inferred;
Story eligibility stays conservative when the type is unknown. Existing direct Instagram
connections retain their distinct provider and are not replaced by Facebook Login.

Six synthetic regressions reproduce #100 when an unsupported field is requested, cover
all three Meta OAuth purposes and pagination, the full web callback in Arabic/English,
and clearing stale Business metadata without enabling Stories. Tests deny unmocked HTTP,
allow only mocked GETs, and retain the global publication lock. No real account is linked
by acceptance, and no real publication, media container or draft upload is allowed.

Local acceptance passed 460 focused tests, including the six new regressions. Linux CI,
exact runtime hashes and preserved production locks require separate release evidence;
the customer must retry their own authorization to establish a live successful connection.

Primary source checked 21 October 2026: Meta's generated
[IGUser field schema](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/iguser.py).
The developer-reference fetch was rate-limited; no third-party schema is used as authority.
