# Use browser clearance for browserless data fetches

The local API uses visible Camoufox only to acquire the Cloudflare clearance cookies and matching user agent, then performs every sample-data request through a serialized curl_cffi session with the compatible fingerprint and network identity. When challenged, it may refresh clearance once and retry once, with no browser fallback for data retrieval; this accepts a slower first request so the project can prove that one browser-acquired session supports lower-overhead browserless lookups.
