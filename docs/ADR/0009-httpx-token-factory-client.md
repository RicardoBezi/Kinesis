# ADR 0009: httpx client for Nebius Token Factory

**Status:** Accepted, pending spike S4

## Context
Token Factory exposes an OpenAI-compatible API. Kinesis needs three calls from it:
- chat completions with text;
- chat completions with images;
- listing the available models.

## Decision
Build `TokenFactoryClient` on `httpx.AsyncClient`:
- **Explicit shapes:** the request and response formats are spelled out in code.
- **Timeout:** every request has one.
- **Error classification:** timeouts, 429 and 5xx are retryable. Other 4xx errors are fatal.
- **Usage tracking:** the client parses the `usage` block and records the token counts.
- **Testing:** tests inject `httpx.MockTransport` instead of calling the real service.

## Assumptions to verify (S4)
- the base URL and the authentication header;
- that `/v1/models` lists the available models;
- the exact Nemotron 3 Super and Nano Omni model IDs;
- support for `response_format: {type: json_schema}`. If it is missing, fall back to JSON mode and validate afterwards;
- the image input format (`image_url` as a data URL) and the maximum number of images per request;
- that responses include `usage`.

## Consequences
- **No vendor SDK dependency.** Kinesis does not depend on the release schedule of a vendor SDK.
- **Slightly more code to maintain.** That is acceptable for three endpoints.
