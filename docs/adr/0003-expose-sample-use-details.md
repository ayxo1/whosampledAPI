---
status: accepted
---

# Expose Sample Uses as individual detail resources

The artist Samples collection discovers direct Sample Uses and exposes each relationship's numeric WhoSampled ID and URL. Clients retrieve one relationship at a time through `GET /sample-uses/{sample_use_id}` because independently resumable detail requests limit failure scope and let callers skip duplicate IDs. The endpoint reports the Sampling Recording, Source Material, timing positions, sampled elements, evidence, completeness, and source Observation, while collection progress, persistence, notability, deduplication, and dataset construction remain outside this API.
