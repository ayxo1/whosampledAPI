---
status: proposed
---

# Enrich Source Recordings separately

This decision depends on [ADR 0003](./0003-expose-sample-use-details.md), whose Sample Use detail supplies an opaque reference for music Source Recordings. Optional enrichment belongs in `GET /recordings/{recording_ref}` rather than the Sample Use endpoint so callers can fetch each unique Source Recording once and omit the extra upstream work when genres are unnecessary. Accept this proposal only after a live probe confirms that current Sample Use pages expose canonical Source Recording links and that the linked Recording pages expose usable genre data; Sampling Recording enrichment is outside the initial scope.
