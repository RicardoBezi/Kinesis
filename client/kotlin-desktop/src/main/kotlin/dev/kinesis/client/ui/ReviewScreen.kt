package dev.kinesis.client.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.kinesis.client.generated.model.CandidateLabel
import dev.kinesis.client.generated.model.CandidateMetrics
import dev.kinesis.client.generated.model.CandidateStatus
import dev.kinesis.client.generated.model.DecisionChoice
import dev.kinesis.client.generated.model.EvaluatorStatus
import dev.kinesis.client.state.Action
import dev.kinesis.client.state.Pane
import dev.kinesis.client.state.ReviewState
import dev.kinesis.client.state.ViewPhase
import kotlinx.coroutines.delay

private const val PLAYBACK_FPS = 12

@Composable
fun ReviewScreen(
    state: ReviewState,
    frames: FrameCache,
    dispatch: (Action) -> Unit,
    onDecide: (DecisionChoice) -> Unit,
    onCancel: () -> Unit,
) {
    val job = state.job
    if (job == null) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            if (state.loading) CircularProgressIndicator() else Text("Open a job, or start a new repair.")
        }
        return
    }
    LaunchedEffect(state.playing) {
        while (state.playing) {
            delay(1000L / PLAYBACK_FPS)
            dispatch(Action.Tick)
        }
    }
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Header(state, onCancel)
        state.error?.let { ErrorBanner(it.title, it.code, it.detail) { dispatch(Action.ClearError) } }
        job.defect?.let { Text(it.summary, style = MaterialTheme.typography.bodyLarge) }
        job.error?.let { ErrorBanner("Job failed", it.code.value, it.message, null) }
        if (state.phase == ViewPhase.WAITING) ProgressLog(state)
        if (state.panes.any { it.frames != null }) {
            Previews(state, frames, dispatch)
            Playback(state, dispatch)
        }
        if (job.candidates.orEmpty().isNotEmpty()) MetricsTable(state, dispatch)
        Evaluation(state)
        Decision(state, onDecide)
    }
}

@Composable
private fun Header(state: ReviewState, onCancel: () -> Unit) {
    val job = state.job ?: return
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(job.jobId, style = MaterialTheme.typography.titleLarge)
        StatusChip(job.status.value)
        val t = job.selection.temporal
        Text("${job.selection.targetBones.joinToString()} · frames ${t.frameStart}–${t.frameEnd}")
        Spacer(Modifier.weight(1f))
        if (state.phase == ViewPhase.WAITING || state.phase == ViewPhase.REVIEW) {
            TextButton(onClick = onCancel) { Text("Cancel job") }
        }
    }
}

@Composable
fun StatusChip(status: String) {
    val color = when (status) {
        "AWAITING_DECISION" -> Color(0xFF2E7D32)
        "FAILED", "CANCELLED" -> Color(0xFFC62828)
        "COMPLETED" -> Color(0xFF1565C0)
        else -> Color(0xFF6D6D6D)
    }
    Text(
        status.replace('_', ' '),
        color = Color.White,
        modifier = Modifier.background(color, RoundedCornerShape(6.dp)).padding(horizontal = 8.dp, vertical = 2.dp),
        style = MaterialTheme.typography.labelMedium,
    )
}

@Composable
private fun ErrorBanner(title: String, code: String?, detail: String?, onDismiss: (() -> Unit)?) {
    Card(Modifier.fillMaxWidth()) {
        Row(Modifier.background(Color(0xFFFFEBEE)).padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(title + (code?.let { " · $it" } ?: ""), fontWeight = FontWeight.Bold, color = Color(0xFFB71C1C))
                detail?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
            }
            if (onDismiss != null) TextButton(onClick = onDismiss) { Text("Dismiss") }
        }
    }
}

@Composable
private fun ProgressLog(state: ReviewState) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                CircularProgressIndicator(Modifier.width(18.dp).height(18.dp), strokeWidth = 2.dp)
                Text("Repairing…", fontWeight = FontWeight.Bold)
            }
            state.events.takeLast(8).forEach { e ->
                val what = listOfNotNull(e.node, e.message?.takeIf { it.isNotBlank() }).joinToString(": ")
                Text("#${e.seq} ${e.type.value.lowercase().replace('_', ' ')} $what", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun Previews(state: ReviewState, frames: FrameCache, dispatch: (Action) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
        state.panes.forEach { pane ->
            val label = pane.candidate?.label
            val selected = label != null && state.selected == label
            Column(
                Modifier.weight(1f)
                    .border(BorderStroke(if (selected) 3.dp else 1.dp, if (selected) Color(0xFF2E7D32) else Color.LightGray), RoundedCornerShape(8.dp))
                    .clickable(enabled = label != null) { label?.let { dispatch(Action.Select(it)) } }
                    .padding(8.dp),
            ) {
                Text(pane.title + paneSuffix(pane), fontWeight = FontWeight.Bold)
                FrameImage(pane, state.frame, frames)
            }
        }
    }
}

private fun paneSuffix(pane: Pane): String {
    val c = pane.candidate ?: return ""
    return when {
        c.status == CandidateStatus.FAILED -> " · failed"
        c.metrics?.gated == true -> " · gated"
        else -> ""
    }
}

@Composable
private fun FrameImage(pane: Pane, frame: Int, frames: FrameCache) {
    val ref = pane.frames
    val image by produceState<ImageBitmap?>(null, ref?.artifactId, frame) {
        value = ref?.let { runCatching { frames.load(it, frame) }.getOrNull() }
    }
    Box(Modifier.fillMaxWidth().aspectRatio(1f).background(Color(0xFF202020)), contentAlignment = Alignment.Center) {
        val bitmap = image
        when {
            ref == null -> Text(pane.candidate?.error?.message ?: "no preview", color = Color.LightGray, modifier = Modifier.padding(8.dp))
            bitmap == null -> CircularProgressIndicator()
            else -> Image(bitmap, contentDescription = "${pane.title} frame $frame", modifier = Modifier.fillMaxSize())
        }
    }
}

@Composable
private fun Playback(state: ReviewState, dispatch: (Action) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedButton(onClick = { dispatch(Action.TogglePlay) }) { Text(if (state.playing) "Pause" else "Play") }
        Slider(
            value = state.frame.toFloat(),
            onValueChange = { dispatch(Action.SetFrame(it.toInt())) },
            valueRange = 0f..maxOf(1, state.frameCount - 1).toFloat(),
            modifier = Modifier.weight(1f),
        )
        Text("frame ${state.sceneFrame ?: "-"}")
    }
}

private data class MetricRow(val name: String, val format: (CandidateMetrics) -> String)

private val METRIC_ROWS = listOf(
    MetricRow("Slip reduction") { "%.1f %%".format(it.slipReductionPct) },
    MetricRow("Planted drift after") { "%.2f cm".format(it.plantedDisplacementCmAfter) },
    MetricRow("Jerk ratio") { "%.2f".format(it.jerkRmsRatio) },
    MetricRow("Joint deviation (RMS)") { "%.2f cm".format(it.targetDeviationRmsCm) },
    MetricRow("Collateral change") { "%.4f cm".format(it.collateralMaxCm) },
    MetricRow("Outside window") { "%.4f cm".format(it.outsideWindowMaxCm) },
    MetricRow("Penetration") { "%.2f cm".format(it.penetrationMaxCm) },
    MetricRow("Objective score") { it.objectiveScore?.let { s -> "%.2f".format(s) } ?: "gated" },
)

@Composable
private fun MetricsTable(state: ReviewState, dispatch: (Action) -> Unit) {
    val candidates = state.job?.candidates.orEmpty().sortedBy { it.label.value }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text("Measured", style = MaterialTheme.typography.titleMedium)
            Row { Text("", Modifier.weight(1.5f)); candidates.forEach { Text("Candidate ${it.label.value}", Modifier.weight(1f), fontWeight = FontWeight.Bold) } }
            HorizontalDivider()
            METRIC_ROWS.forEach { row ->
                Row {
                    Text(row.name, Modifier.weight(1.5f))
                    candidates.forEach { c ->
                        Text(c.metrics?.let(row.format) ?: c.status?.value?.lowercase() ?: "", Modifier.weight(1f))
                    }
                }
            }
            candidates.flatMap { c -> c.metrics?.gateReasons.orEmpty().map { "${c.label.value}: $it" } }.forEach {
                Text("gated — $it", color = Color(0xFFC62828), style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

@Composable
private fun Evaluation(state: ReviewState) {
    val evaluation = state.job?.evaluation ?: return
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("Recommendation", style = MaterialTheme.typography.titleMedium)
                evaluation.recommended?.let { AssistChip(onClick = {}, label = { Text("Candidate ${it.value}") }) }
                if (evaluation.evaluatorStatus != EvaluatorStatus.OK) Text("visual review: ${evaluation.evaluatorStatus.value.lowercase()}")
            }
            evaluation.recommendationReason?.let { Text(it) }
            evaluation.visual.orEmpty().forEach { v ->
                val label = state.job?.candidates?.firstOrNull { it.candidateId == v.candidateId }?.label?.value ?: "?"
                Text(
                    "$label · contact ${v.contactStability}/5 · natural ${v.naturalness}/5 · artifacts ${v.artifactsVisible}/5 · " +
                        "performance ${v.performancePreservation}/5 — ${v.notes.orEmpty()} (${v.modelId})",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

@Composable
private fun Decision(state: ReviewState, onDecide: (DecisionChoice) -> Unit) {
    val job = state.job ?: return
    when (state.phase) {
        ViewPhase.REVIEW -> Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
            val chosen = state.selected
            Button(onClick = { chosen?.let { onDecide(if (it == CandidateLabel.A) DecisionChoice.A else DecisionChoice.B) } }, enabled = state.canDecide && chosen != null) {
                Text(chosen?.let { "Apply candidate ${it.value}" } ?: "Select a candidate")
            }
            OutlinedButton(onClick = { onDecide(DecisionChoice.REJECT_ALL) }, enabled = state.canDecide) { Text("Reject all") }
            if (state.submitting) CircularProgressIndicator(Modifier.width(20.dp).height(20.dp), strokeWidth = 2.dp)
        }
        ViewPhase.APPLYING -> Text("Applying the chosen candidate as an NLA layer…")
        ViewPhase.DONE -> job.output?.let {
            Text("Done: ${job.decision?.choice?.value ?: ""} applied non-destructively. Output: ${it.uri} (${it.sizeBytes / 1024} KiB)")
        } ?: Text("Completed: no defect detected; nothing to repair.")
        ViewPhase.REJECTED -> Text("All candidates rejected. The original animation is unchanged.")
        else -> Unit
    }
}
