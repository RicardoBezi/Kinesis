package dev.kinesis.client.state

import dev.kinesis.client.api.ApiException
import dev.kinesis.client.generated.model.ArtifactKind
import dev.kinesis.client.generated.model.ArtifactReference
import dev.kinesis.client.generated.model.CandidateLabel
import dev.kinesis.client.generated.model.CandidateStatus
import dev.kinesis.client.generated.model.EventPage
import dev.kinesis.client.generated.model.JobEvent
import dev.kinesis.client.generated.model.JobStatus
import dev.kinesis.client.generated.model.RepairCandidate
import dev.kinesis.client.generated.model.RepairJob

/** What the review screen shows, derived from the job's status. */
enum class ViewPhase { WAITING, REVIEW, APPLYING, DONE, REJECTED, FAILED, CANCELLED }

fun phaseOf(status: JobStatus): ViewPhase = when (status) {
    JobStatus.PENDING, JobStatus.RUNNING -> ViewPhase.WAITING
    JobStatus.AWAITING_DECISION -> ViewPhase.REVIEW
    JobStatus.APPLYING -> ViewPhase.APPLYING
    JobStatus.COMPLETED -> ViewPhase.DONE
    JobStatus.REJECTED -> ViewPhase.REJECTED
    JobStatus.FAILED -> ViewPhase.FAILED
    JobStatus.CANCELLED -> ViewPhase.CANCELLED
}

val JobStatus.isTerminal: Boolean
    get() = this in setOf(JobStatus.COMPLETED, JobStatus.REJECTED, JobStatus.FAILED, JobStatus.CANCELLED)

/** A user-facing error, rendered from a Problem body when the server sent one. */
data class UiError(val title: String, val code: String?, val detail: String?) {
    companion object {
        fun from(e: Throwable): UiError = when (e) {
            is ApiException -> e.problem?.let { UiError(it.title, it.code.value, it.detail) }
                ?: UiError(e.message ?: "Request failed", null, null)
            else -> UiError(e.message ?: e::class.simpleName ?: "Error", null, null)
        }
    }
}

/** One preview pane: Original, A or B. */
data class Pane(val title: String, val frames: ArtifactReference?, val candidate: RepairCandidate?)

data class ReviewState(
    val job: RepairJob? = null,
    val events: List<JobEvent> = emptyList(),
    val nextSeq: Int = 0,
    val selected: CandidateLabel? = null,
    val frame: Int = 0,
    val playing: Boolean = false,
    val loading: Boolean = false,
    val submitting: Boolean = false,
    val error: UiError? = null,
) {
    val phase: ViewPhase? get() = job?.let { phaseOf(it.status) }

    fun candidate(label: CandidateLabel): RepairCandidate? = job?.candidates?.firstOrNull { it.label == label }

    fun isSelectable(label: CandidateLabel): Boolean =
        phase == ViewPhase.REVIEW && candidate(label)?.status == CandidateStatus.SUCCEEDED

    val canDecide: Boolean get() = phase == ViewPhase.REVIEW && !submitting

    val panes: List<Pane>
        get() {
            val j = job ?: return emptyList()
            val original = Pane("Original", j.originalArtifacts.preview(), null)
            val candidates = CandidateLabel.entries.mapNotNull { label ->
                candidate(label)?.let { Pane("Candidate ${label.value}", it.artifacts.preview(), it) }
            }
            return listOf(original) + candidates
        }

    /** All panes share one frame index, so the shortest sequence bounds it. */
    val frameCount: Int get() = panes.mapNotNull { it.frames?.frameCount }.minOrNull() ?: 0

    /** Scene frame number shown for the shared index. */
    val sceneFrame: Int?
        get() = panes.firstNotNullOfOrNull { it.frames }?.let { (it.firstFrame ?: 0) + frame * (it.frameStep ?: 1) }
}

private fun List<ArtifactReference>?.preview(): ArtifactReference? =
    this?.firstOrNull { it.kind == ArtifactKind.PREVIEW_FRAMES }

sealed interface Action {
    data object Loading : Action
    data class JobLoaded(val job: RepairJob) : Action
    data class EventsReceived(val page: EventPage) : Action
    data class Select(val label: CandidateLabel) : Action
    data class SetFrame(val frame: Int) : Action
    data object TogglePlay : Action
    data object Tick : Action
    data object DecisionSubmitting : Action
    data class Failed(val error: UiError) : Action
    data object ClearError : Action
}

/** Pure state transitions; every UI change goes through here (and is unit tested). */
fun reduce(state: ReviewState, action: Action): ReviewState = when (action) {
    Action.Loading -> state.copy(loading = true)
    is Action.JobLoaded -> {
        // Only a *different* job resets the view; the first load keeps events already received.
        val switched = state.job != null && state.job.jobId != action.job.jobId
        val base = if (switched) ReviewState() else state
        val next = base.copy(job = action.job, loading = false, submitting = false)
        val decided = action.job.decision?.choice?.let { c -> CandidateLabel.entries.firstOrNull { it.value == c.value } }
        val selected = when {
            // In review: keep a still-valid choice, otherwise preselect the recommendation.
            next.phase == ViewPhase.REVIEW ->
                next.selected?.takeIf { next.isSelectable(it) }
                    ?: action.job.evaluation?.recommended?.takeIf { next.isSelectable(it) }
            // After the decision: show what was chosen.
            else -> decided ?: next.selected
        }
        next.copy(
            selected = selected,
            frame = next.frame.coerceIn(0, maxOf(0, next.frameCount - 1)),
            playing = next.playing && next.frameCount > 1,
        )
    }
    is Action.EventsReceived -> {
        val known = state.events.map { it.seq }.toHashSet()
        val fresh = action.page.events.filter { it.seq !in known }
        state.copy(
            events = (state.events + fresh).sortedBy { it.seq },
            nextSeq = maxOf(state.nextSeq, action.page.nextSeq),
            error = null,
        )
    }
    is Action.Select -> if (state.isSelectable(action.label)) state.copy(selected = action.label) else state
    is Action.SetFrame -> state.copy(frame = action.frame.coerceIn(0, maxOf(0, state.frameCount - 1)))
    Action.TogglePlay -> state.copy(playing = !state.playing && state.frameCount > 1)
    Action.Tick -> if (!state.playing || state.frameCount == 0) state else state.copy(frame = (state.frame + 1) % state.frameCount)
    Action.DecisionSubmitting -> state.copy(submitting = true, playing = false)
    is Action.Failed -> state.copy(error = action.error, loading = false, submitting = false)
    Action.ClearError -> state.copy(error = null)
}
