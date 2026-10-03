package dev.kinesis.client

import kotlinx.serialization.json.Json

/**
 * JSON configuration shared by the HTTP client and the tests.
 *
 * Unknown keys are ignored because API v1 may add response fields (docs/API.md). Defaults
 * are not encoded, so request bodies carry only what the user actually set.
 */
val ApiJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = false
}
