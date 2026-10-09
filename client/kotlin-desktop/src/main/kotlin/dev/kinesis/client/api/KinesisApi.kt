package dev.kinesis.client.api

import dev.kinesis.client.ApiJson
import dev.kinesis.client.generated.model.CreateRepairJobRequest
import dev.kinesis.client.generated.model.DecisionRequest
import dev.kinesis.client.generated.model.EventPage
import dev.kinesis.client.generated.model.HealthReport
import dev.kinesis.client.generated.model.Problem
import dev.kinesis.client.generated.model.ProductStats
import dev.kinesis.client.generated.model.RepairJob
import dev.kinesis.client.generated.model.SceneRef
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.HttpRequestBuilder
import io.ktor.client.request.forms.MultiPartFormDataContent
import io.ktor.client.request.forms.formData
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.post
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.Headers
import io.ktor.http.HttpHeaders
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import io.ktor.serialization.kotlinx.json.json

/**
 * An API failure. [problem] is the server's RFC 7807 body when there was one; a transport
 * failure (server down, timeout) has none and [status] 0.
 */
class ApiException(val status: Int, val problem: Problem?, message: String) : Exception(message)

/** Thin, typed wrapper over the Kinesis HTTP API (docs/API.md). */
class KinesisApi(private val http: HttpClient, baseUrl: String) {
    val baseUrl: String = baseUrl.trimEnd('/')

    companion object {
        fun client(engine: HttpClientEngine? = null): HttpClient {
            val config: io.ktor.client.HttpClientConfig<*>.() -> Unit = {
                expectSuccess = false
                install(ContentNegotiation) { json(ApiJson) }
                install(HttpTimeout) {
                    requestTimeoutMillis = 120_000
                    connectTimeoutMillis = 5_000
                }
            }
            return if (engine != null) HttpClient(engine, config) else HttpClient(CIO, config)
        }

        fun create(baseUrl: String): KinesisApi = KinesisApi(client(), baseUrl)
    }

    private fun url(path: String) = if (path.startsWith("http")) path else "$baseUrl$path"

    private suspend fun check(response: HttpResponse): HttpResponse {
        if (response.status.isSuccess()) return response
        val text = runCatching { response.bodyAsText() }.getOrDefault("")
        val problem = runCatching { ApiJson.decodeFromString(Problem.serializer(), text) }.getOrNull()
        val message = problem?.let { "${it.title} (${it.code.value})" + (it.detail?.let { d -> ": $d" } ?: "") }
            ?: "HTTP ${response.status.value}"
        throw ApiException(response.status.value, problem, message)
    }

    private suspend fun get(path: String, block: HttpRequestBuilder.() -> Unit = {}): HttpResponse =
        transport { check(http.get(url(path), block)) }

    private suspend fun post(path: String, block: HttpRequestBuilder.() -> Unit = {}): HttpResponse =
        transport { check(http.post(url(path), block)) }

    private suspend fun transport(call: suspend () -> HttpResponse): HttpResponse =
        try {
            call()
        } catch (e: ApiException) {
            throw e
        } catch (e: Exception) {
            if (e is kotlinx.coroutines.CancellationException) throw e
            throw ApiException(0, null, "Cannot reach the Kinesis server at $baseUrl (${e.message ?: e::class.simpleName})")
        }

    suspend fun health(): HealthReport = get("/v1/health/providers").body()

    suspend fun listJobs(limit: Int = 50): List<RepairJob> = get("/v1/jobs") { parameter("limit", limit) }.body()

    suspend fun job(jobId: String): RepairJob = get("/v1/jobs/$jobId").body()

    suspend fun events(jobId: String, afterSeq: Int, limit: Int = 200): EventPage =
        get("/v1/jobs/$jobId/events") {
            parameter("after_seq", afterSeq)
            parameter("limit", limit)
        }.body()

    suspend fun uploadScene(bytes: ByteArray, fileName: String = "scene.blend"): SceneRef =
        post("/v1/scenes") {
            setBody(
                MultiPartFormDataContent(
                    formData {
                        append(
                            "file",
                            bytes,
                            Headers.build {
                                append(HttpHeaders.ContentType, "application/octet-stream")
                                append(HttpHeaders.ContentDisposition, "filename=\"$fileName\"")
                            },
                        )
                    },
                ),
            )
        }.body()

    suspend fun createJob(request: CreateRepairJobRequest, idempotencyKey: String): RepairJob =
        post("/v1/jobs") {
            header("Idempotency-Key", idempotencyKey)
            contentType(ContentType.Application.Json)
            setBody(request)
        }.body()

    suspend fun decide(jobId: String, request: DecisionRequest): RepairJob =
        post("/v1/jobs/$jobId/decision") {
            contentType(ContentType.Application.Json)
            setBody(request)
        }.body()

    suspend fun cancel(jobId: String): RepairJob = post("/v1/jobs/$jobId/cancel").body()

    suspend fun stats(): ProductStats = get("/v1/stats/product").body()

    /** One frame of a frame-sequence artifact, or a whole file when [index] is null. */
    suspend fun artifact(uri: String, index: Int? = null): ByteArray =
        get(uri) { if (index != null) parameter("frame", index) }.body()

    fun close() = http.close()
}
