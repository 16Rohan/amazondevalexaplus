# Alexa Appliance Support MCP Data

This package contains data for the Alexa appliance-support MCP server under `mcp/data/`.

## Files
- `sources.json`: official source URLs, selected facts used, extraction status, and provenance policy.
- `appliances.json`: 117 synthetic appliance ownership records across 10 categories with household isolation.
- `troubleshooting.json`: paraphrased category and model guidance with source references and safety limits across 11 appliance types.
- `warranty.json`: 117 synthetic warranty records matching each appliance.
- `service_slots.json`: synthetic estimated service slots; lookup does not create a booking.
- `cases.json`: synthetic support cases.
- `scenarios.json`: curated scenarios and expected MCP tool calls/outcomes.

## Defensible provenance statement
"Alexa's demo troubleshooting fixtures were prepared from selected guidance in official manufacturer manuals and support pages. Appliance, warranty, service-slot, and case-history records are synthetic. Each troubleshooting record carries source provenance and avoids model-specific claims when the source does not support them."

Do not claim the full manuals were scraped unless a verified ingestion job actually downloaded and processed them. This package records reviewed sources and selected extracted facts; it does not represent a complete manual corpus.

## Integration notes
The MCP handlers load these files from `mcp/data/`, preserve canonical tool schemas, and return `is_simulated: true` for synthetic domain records. A successful MCP call is genuine MCP execution even when the returned domain data is simulated.

E21 is intentionally undocumented for an unidentified washer. Refrigerator guidance is general, not an exact-model diagnosis. Service slots are estimated demo values, not real provider availability. No booking is made by lookup.
