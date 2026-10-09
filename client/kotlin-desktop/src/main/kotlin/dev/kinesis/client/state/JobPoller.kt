package dev.kinesis.client.state

import dev.kinesis.client.api.KinesisApi
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlin.time.Duration
import kotlin.time.Duration.Companion.seconds
import kotlin.time.TimeSource

/**
 * ADR 0006 polling: events after the last seen `seq`, then the job.
 *
 * - every [fast] while events keep arriving; [slow] after [idleAfter] without new events;
 * - a failed request emits [Action.Failed] and the next poll resumes from the same `seq`,
 *   so a disconnect loses nothing (and the reducer drops duplicates);
 * - the flow completes once the job is terminal.
 */
class JobPoller(
    private val api: KinesisApi,
    private val fast: Duration = 1.seconds,
    private val slow: Duration = 5.seconds,
    private val idleAfter: Duration = 30.seconds,
    private val time: TimeSource = TimeSource.Monotonic,
) {
    fun poll(jobId: String, afterSeq: Int = 0): Flow<Action> = flow {
        var after = afterSeq
        var lastEvent = time.markNow()
        while (true) {
            try {
                val page = api.events(jobId, after)
                if (page.events.isNotEmpty()) {
                    emit(Action.EventsReceived(page))
                    after = page.nextSeq
                    lastEvent = time.markNow()
                }
                val job = api.job(jobId)
                emit(Action.JobLoaded(job))
                if (job.status.isTerminal) return@flow
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                emit(Action.Failed(UiError.from(e)))
            }
            delay(if (lastEvent.elapsedNow() > idleAfter) slow else fast)
        }
    }
}
