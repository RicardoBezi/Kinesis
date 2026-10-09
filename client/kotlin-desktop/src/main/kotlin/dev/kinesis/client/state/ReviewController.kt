package dev.kinesis.client.state

import dev.kinesis.client.api.KinesisApi
import dev.kinesis.client.generated.model.AnimationSelection
import dev.kinesis.client.generated.model.CreateRepairJobRequest
import dev.kinesis.client.generated.model.DecisionChoice
import dev.kinesis.client.generated.model.DecisionRequest
import dev.kinesis.client.generated.model.HealthReport
import dev.kinesis.client.generated.model.PlanMode
import dev.kinesis.client.generated.model.RepairJob
import dev.kinesis.client.generated.model.SceneRef
import dev.kinesis.client.generated.model.TemporalScope
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import java.util.UUID
import kotlin.time.TimeSource

/** Wires the API, the poller and the reducer together for the UI. */
class ReviewController(
    val api: KinesisApi,
    private val scope: CoroutineScope,
    private val poller: JobPoller = JobPoller(api),
) {
    private val _state = MutableStateFlow(ReviewState())
    val state: StateFlow<ReviewState> = _state.asStateFlow()

    private val _jobs = MutableStateFlow<List<RepairJob>>(emptyList())
    val jobs: StateFlow<List<RepairJob>> = _jobs.asStateFlow()

    private val _health = MutableStateFlow<HealthReport?>(null)
    val health: StateFlow<HealthReport?> = _health.asStateFlow()

    private var polling: Job? = null
    private var reviewStarted: TimeSource.Monotonic.ValueTimeMark? = null

    fun dispatch(action: Action) {
        _state.update { reduce(it, action) }
        if (action is Action.JobLoaded && action.job.status.name == "AWAITING_DECISION" && reviewStarted == null) {
            reviewStarted = TimeSource.Monotonic.markNow()
        }
    }

    fun refreshJobs() = scope.launch {
        runCatching { api.listJobs() }
            .onSuccess { _jobs.value = it }
            .onFailure { dispatch(Action.Failed(UiError.from(it))) }
        _health.value = runCatching { api.health() }.getOrNull()
    }

    fun open(jobId: String) {
        polling?.cancel()
        reviewStarted = null
        dispatch(Action.Loading)
        polling = scope.launch { poller.poll(jobId).collect { dispatch(it) } }
    }

    fun decide(choice: DecisionChoice, note: String? = null) {
        val job = state.value.job ?: return
        if (!state.value.canDecide) return
        dispatch(Action.DecisionSubmitting)
        val seconds = reviewStarted?.elapsedNow()?.inWholeMilliseconds?.div(1000.0)
        scope.launch {
            runCatching { api.decide(job.jobId, DecisionRequest(choice = choice, note = note, timeToDecisionS = seconds)) }
                .onSuccess {
                    dispatch(Action.JobLoaded(it))
                    if (polling?.isActive != true) open(job.jobId)
                    refreshJobs()
                }
                .onFailure { dispatch(Action.Failed(UiError.from(it))) }
        }
    }

    fun cancel() {
        val job = state.value.job ?: return
        scope.launch {
            runCatching { api.cancel(job.jobId) }
                .onSuccess { dispatch(Action.JobLoaded(it)); refreshJobs() }
                .onFailure { dispatch(Action.Failed(UiError.from(it))) }
        }
    }

    /** Step 1 of a new repair: upload a .blend copy; the server inspects it. */
    suspend fun upload(blend: ByteArray, fileName: String): Result<SceneRef> =
        runCatching { api.uploadScene(blend, fileName) }
            .onFailure { dispatch(Action.Failed(UiError.from(it))) }

    /** Step 2: create the job for the animator's selection and open it. */
    suspend fun create(scene: SceneRef, draft: AnimationSelectionDraft): Result<RepairJob> = runCatching {
        val request = CreateRepairJobRequest(
            selection = AnimationSelection(
                armature = draft.armature,
                sceneId = scene.sceneId,
                targetBones = listOf(draft.targetBone),
                temporal = TemporalScope(frameEnd = draft.frameEnd, frameStart = draft.frameStart),
                instruction = draft.instruction?.takeIf { it.isNotBlank() },
            ),
            planMode = draft.planMode,
        )
        api.createJob(request, "client-${UUID.randomUUID()}")
    }.onSuccess {
        open(it.jobId)
        refreshJobs()
    }.onFailure { dispatch(Action.Failed(UiError.from(it))) }
}

data class AnimationSelectionDraft(
    val armature: String,
    val targetBone: String,
    val frameStart: Int,
    val frameEnd: Int,
    val planMode: PlanMode = PlanMode.AUTO,
    val instruction: String? = null,
)
