package dev.kinesis.client

import dev.kinesis.client.api.ApiException
import dev.kinesis.client.api.KinesisApi
import dev.kinesis.client.generated.model.CreateRepairJobRequest
import dev.kinesis.client.generated.model.EventPage
import dev.kinesis.client.generated.model.JobStatus
import dev.kinesis.client.generated.model.RepairJob
import dev.kinesis.client.state.Action
import dev.kinesis.client.state.JobPoller
import dev.kinesis.client.state.ReviewState
import dev.kinesis.client.state.reduce
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.OutgoingContent
import io.ktor.http.headersOf
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertTrue
import kotlin.time.Duration.Companion.seconds

private val JSON = headersOf(HttpHeaders.ContentType, "application/json")

private fun MockRequestHandleScope.json(body: String, status: HttpStatusCode = HttpStatusCode.OK): HttpResponseData =
    respond(body, status, JSON)

class JobPollerTest {
    private val job = sampleJob()

    private fun jobJson(status: JobStatus) = ApiJson.encodeToString(RepairJob.serializer(), job.withStatus(status))

    private fun page(vararg seqs: Int) = ApiJson.encodeToString(EventPage.serializer(), EventPage(seqs.map(::event), seqs.lastOrNull() ?: 0))

    @Test
    fun `poller resumes from next_seq after a disconnect and stops when terminal`() = runTest {
        val seen = mutableListOf<String>()
        var eventsCall = 0
        val engine = MockEngine { request ->
            seen += request.url.encodedPathAndQuery
            when {
                request.url.encodedPath.endsWith("/events") -> when (eventsCall++) {
                    0 -> json(page(1, 2))
                    1 -> throw IOException("connection reset") // the disconnect
                    2 -> json(page(3))
                    else -> json(page())
                }
                eventsCall <= 1 -> json(jobJson(JobStatus.RUNNING))
                else -> json(jobJson(JobStatus.AWAITING_DECISION)).also { if (eventsCall > 3) return@MockEngine json(jobJson(JobStatus.COMPLETED)) }
            }
        }
        val api = KinesisApi(KinesisApi.client(engine), "http://test")
        val poller = JobPoller(api, fast = 1.seconds, slow = 5.seconds, time = testScheduler.timeSource)
        val actions = poller.poll(job.jobId).toList()

        val afterSeqs = seen.filter { "/events" in it }.map { it.substringAfter("after_seq=").substringBefore('&').toInt() }
        assertEquals(listOf(0, 2, 2, 3), afterSeqs) // the failed call is retried from seq 2
        assertTrue(actions.any { it is Action.Failed }, "the disconnect is surfaced")
        assertIs<Action.JobLoaded>(actions.last())
        assertEquals(JobStatus.COMPLETED, (actions.last() as Action.JobLoaded).job.status)

        val state = actions.fold(ReviewState(), ::reduce)
        assertEquals(listOf(1, 2, 3), state.events.map { it.seq })
        assertEquals(null, state.error) // cleared by the next successful page
    }

    @Test
    fun `idle jobs are polled slowly`() = runTest {
        var calls = 0
        val engine = MockEngine { request ->
            if (request.url.encodedPath.endsWith("/events")) json(page())
            else { calls++; json(jobJson(if (calls >= 8) JobStatus.CANCELLED else JobStatus.AWAITING_DECISION)) }
        }
        val api = KinesisApi(KinesisApi.client(engine), "http://test")
        val poller = JobPoller(api, fast = 1.seconds, slow = 5.seconds, idleAfter = 3.seconds, time = testScheduler.timeSource)
        poller.poll(job.jobId).toList()
        // Fast polls at 0-4 s until 3 s pass without events, then every 5 s: 9, 14, 19.
        assertEquals(19_000, testScheduler.currentTime)
    }

    @Test
    fun `problem bodies become ApiExceptions and requests carry the idempotency key`() = runTest {
        var captured: HttpRequestData? = null
        val engine = MockEngine { request ->
            captured = request
            json(
                """{"type":"about:blank","title":"Invalid selection","status":422,"code":"BONE_NOT_FOUND","detail":"bone 'foot.X'"}""",
                HttpStatusCode.UnprocessableEntity,
            )
        }
        val api = KinesisApi(KinesisApi.client(engine), "http://test/")
        val error = assertFailsWith<ApiException> {
            api.createJob(CreateRepairJobRequest(selection = job.selection), "client-key-0001")
        }
        assertEquals(422, error.status)
        assertEquals("BONE_NOT_FOUND", error.problem?.code?.value)
        assertEquals("client-key-0001", captured!!.headers["Idempotency-Key"])
        assertEquals("http://test/v1/jobs", captured!!.url.toString())
        assertIs<OutgoingContent>(captured!!.body)
    }

    @Test
    fun `transport failures carry no problem`() = runTest {
        val api = KinesisApi(KinesisApi.client(MockEngine { throw IOException("refused") }), "http://test")
        val error = assertFailsWith<ApiException> { api.job("job_01j9zjob0001") }
        assertEquals(0, error.status)
        assertTrue("Cannot reach" in error.message!!)
    }
}
