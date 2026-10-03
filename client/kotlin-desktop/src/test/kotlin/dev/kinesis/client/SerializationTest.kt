package dev.kinesis.client

import dev.kinesis.client.generated.model.CandidateLabel
import dev.kinesis.client.generated.model.DecisionChoice
import dev.kinesis.client.generated.model.ErrorCode
import dev.kinesis.client.generated.model.EventPage
import dev.kinesis.client.generated.model.JobEventType
import dev.kinesis.client.generated.model.JobStatus
import dev.kinesis.client.generated.model.Problem
import dev.kinesis.client.generated.model.RepairJob
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull

/**
 * The samples are produced by scripts/export_samples.py from the backend's Pydantic models.
 * Decoding them with the generated models proves the Kotlin types match what the backend
 * actually sends.
 */
class SerializationTest {
    private fun sample(name: String): String =
        checkNotNull(javaClass.getResource("/samples/$name")) { "missing sample $name" }.readText()

    @Test
    fun `repair job decodes and round-trips`() {
        val job = ApiJson.decodeFromString<RepairJob>(sample("repair_job.json"))
        assertEquals(JobStatus.AWAITING_DECISION, job.status)
        assertEquals(listOf("foot.L"), job.selection.targetBones)
        assertEquals(2, job.candidates?.size)
        val a = job.candidates!!.first { it.label == CandidateLabel.A }
        assertEquals(1.0, a.parameters.lockStrength)
        assertEquals(98.0, a.metrics!!.slipReductionPct, 1e-9)
        assertEquals(listOf(0.1, -0.3, 0.0), job.defect!!.intervals.single().anchor)
        assertEquals(DecisionChoice.B, job.decision!!.choice)
        assertEquals(true, job.decision!!.agreed)

        val again = ApiJson.decodeFromString<RepairJob>(ApiJson.encodeToString(RepairJob.serializer(), job))
        assertEquals(job, again)
    }

    @Test
    fun `event page decodes free-form data`() {
        val page = ApiJson.decodeFromString<EventPage>(sample("event_page.json"))
        assertEquals(2, page.nextSeq)
        val status = page.events.last()
        assertEquals(JobEventType.STATUS_CHANGED, status.type)
        val data: Map<String, JsonElement> = assertNotNull(status.`data`)
        assertEquals("AWAITING_DECISION", data.getValue("to").jsonPrimitive.content)
    }

    @Test
    fun `problem decodes with machine readable code`() {
        val problem = ApiJson.decodeFromString<Problem>(sample("problem.json"))
        assertEquals(ErrorCode.BONE_NOT_FOUND, problem.code)
        assertEquals(422, problem.status)
    }
}
