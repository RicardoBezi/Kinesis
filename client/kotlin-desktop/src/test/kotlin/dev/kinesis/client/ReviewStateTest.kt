package dev.kinesis.client

import dev.kinesis.client.api.ApiException
import dev.kinesis.client.generated.model.CandidateLabel
import dev.kinesis.client.generated.model.CandidateStatus
import dev.kinesis.client.generated.model.ErrorCode
import dev.kinesis.client.generated.model.EventPage
import dev.kinesis.client.generated.model.JobEvent
import dev.kinesis.client.generated.model.JobEventType
import dev.kinesis.client.generated.model.JobStatus
import dev.kinesis.client.generated.model.Problem
import dev.kinesis.client.generated.model.RepairJob
import dev.kinesis.client.state.Action
import dev.kinesis.client.state.ReviewState
import dev.kinesis.client.state.UiError
import dev.kinesis.client.state.ViewPhase
import dev.kinesis.client.state.isTerminal
import dev.kinesis.client.state.phaseOf
import dev.kinesis.client.state.reduce
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

fun sampleJob(): RepairJob = ApiJson.decodeFromString(
    RepairJob.serializer(),
    checkNotNull(ReviewStateTest::class.java.getResource("/samples/repair_job.json")).readText(),
)

fun RepairJob.withStatus(status: JobStatus) = copy(status = status, decision = null)

fun event(seq: Int) = JobEvent(jobId = "job_01j9zjob0001", seq = seq, ts = "2026-10-08T00:00:00Z", type = JobEventType.NODE_STARTED)

class ReviewStateTest {
    private val review = sampleJob().withStatus(JobStatus.AWAITING_DECISION)

    private fun loaded(job: RepairJob = review) = reduce(ReviewState(), Action.JobLoaded(job))

    @Test
    fun `every job status maps to a view phase`() {
        val expected = mapOf(
            JobStatus.PENDING to ViewPhase.WAITING,
            JobStatus.RUNNING to ViewPhase.WAITING,
            JobStatus.AWAITING_DECISION to ViewPhase.REVIEW,
            JobStatus.APPLYING to ViewPhase.APPLYING,
            JobStatus.COMPLETED to ViewPhase.DONE,
            JobStatus.REJECTED to ViewPhase.REJECTED,
            JobStatus.FAILED to ViewPhase.FAILED,
            JobStatus.CANCELLED to ViewPhase.CANCELLED,
        )
        assertEquals(JobStatus.entries.toSet(), expected.keys) // a new status must be mapped here
        for ((status, phase) in expected) {
            val state = loaded(review.withStatus(status))
            assertEquals(phase, state.phase, "$status")
            assertEquals(status == JobStatus.AWAITING_DECISION, state.canDecide, "$status")
            assertEquals(status in setOf(JobStatus.COMPLETED, JobStatus.REJECTED, JobStatus.FAILED, JobStatus.CANCELLED), status.isTerminal)
        }
    }

    @Test
    fun `review preselects the recommendation and allows switching`() {
        val state = loaded()
        assertEquals(CandidateLabel.B, state.selected) // the sample recommends B
        assertEquals(CandidateLabel.A, reduce(state, Action.Select(CandidateLabel.A)).selected)
    }

    @Test
    fun `a failed candidate cannot be selected`() {
        val failedA = review.copy(
            candidates = review.candidates!!.map { if (it.label == CandidateLabel.A) it.copy(status = CandidateStatus.FAILED) else it },
        )
        val state = loaded(failedA)
        assertFalse(state.isSelectable(CandidateLabel.A))
        assertEquals(CandidateLabel.B, reduce(state, Action.Select(CandidateLabel.A)).selected)
    }

    @Test
    fun `selection is not possible outside review and shows the decision afterwards`() {
        val running = loaded(review.withStatus(JobStatus.RUNNING))
        assertNull(running.selected)
        assertNull(reduce(running, Action.Select(CandidateLabel.A)).selected)
        val done = loaded(sampleJob().copy(status = JobStatus.COMPLETED)) // sample decision: B
        assertEquals(CandidateLabel.B, done.selected)
    }

    @Test
    fun `refresh keeps the user's valid choice`() {
        val chose = reduce(loaded(), Action.Select(CandidateLabel.A))
        assertEquals(CandidateLabel.A, reduce(chose, Action.JobLoaded(review)).selected)
    }

    @Test
    fun `switching jobs resets state`() {
        val state = reduce(reduce(loaded(), Action.SetFrame(10)), Action.EventsReceived(EventPage(listOf(event(1)), 1)))
        val other = reduce(state, Action.JobLoaded(review.copy(jobId = "job_01j9zother01")))
        assertEquals(0, other.frame)
        assertTrue(other.events.isEmpty())
    }

    @Test
    fun `events are de-duplicated and ordered`() {
        var state = loaded()
        state = reduce(state, Action.EventsReceived(EventPage(listOf(event(1), event(2)), 2)))
        state = reduce(state, Action.EventsReceived(EventPage(listOf(event(2), event(3)), 3)))
        assertEquals(listOf(1, 2, 3), state.events.map { it.seq })
        assertEquals(3, state.nextSeq)
    }

    @Test
    fun `the shared frame index is clamped and playback wraps`() {
        var state = loaded()
        assertEquals(67, state.frameCount)
        assertEquals(66, reduce(state, Action.SetFrame(500)).frame)
        assertEquals(0, reduce(state, Action.SetFrame(-3)).frame)
        assertEquals(35 + 10, reduce(state, Action.SetFrame(10)).sceneFrame)
        state = reduce(reduce(state, Action.SetFrame(66)), Action.TogglePlay)
        assertTrue(state.playing)
        assertEquals(0, reduce(state, Action.Tick).frame)
        assertEquals(66, reduce(reduce(state, Action.TogglePlay), Action.Tick).frame) // paused: no tick
    }

    @Test
    fun `decision submitting blocks a second decision`() {
        val state = reduce(loaded(), Action.DecisionSubmitting)
        assertTrue(state.submitting)
        assertFalse(state.canDecide)
        assertFalse(reduce(state, Action.JobLoaded(review)).submitting)
    }

    @Test
    fun `errors are rendered from a Problem body`() {
        val problem = Problem(code = ErrorCode.INVALID_STATE, status = 409, title = "Job is not awaiting a decision", detail = "status COMPLETED")
        val error = UiError.from(ApiException(409, problem, "x"))
        assertEquals(UiError("Job is not awaiting a decision", "INVALID_STATE", "status COMPLETED"), error)
        val transport = UiError.from(ApiException(0, null, "Cannot reach the Kinesis server"))
        assertEquals("Cannot reach the Kinesis server", transport.title)
        val state = reduce(reduce(loaded(), Action.DecisionSubmitting), Action.Failed(error))
        assertEquals(error, state.error)
        assertFalse(state.submitting)
        assertNull(reduce(state, Action.ClearError).error)
    }
}
