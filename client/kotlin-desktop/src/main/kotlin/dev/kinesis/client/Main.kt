package dev.kinesis.client

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.VerticalDivider
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState
import dev.kinesis.client.api.KinesisApi
import dev.kinesis.client.generated.model.RepairJob
import dev.kinesis.client.generated.model.SceneRef
import dev.kinesis.client.state.ReviewController
import dev.kinesis.client.ui.FrameCache
import dev.kinesis.client.ui.NewRepairDialog
import dev.kinesis.client.ui.ReviewScreen
import dev.kinesis.client.ui.StatusChip
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.awt.FileDialog
import java.awt.Frame
import java.io.File

/**
 * Kinesis review client: job list, synchronized Original/A/B previews, metrics, the model's
 * view, and the decision. The server URL comes from KINESIS_API_URL (default localhost:8000).
 */
fun main() = application {
    val baseUrl = System.getenv("KINESIS_API_URL") ?: "http://127.0.0.1:8000"
    Window(
        onCloseRequest = ::exitApplication,
        title = "Kinesis Review",
        state = rememberWindowState(width = 1400.dp, height = 900.dp),
    ) {
        App(KinesisApi.create(baseUrl))
    }
}

@Composable
fun App(api: KinesisApi) {
    val scope = rememberCoroutineScope()
    val controller = remember(api) { ReviewController(api, scope) }
    val frames = remember(api) { FrameCache(api) }
    val state by controller.state.collectAsState()
    val jobs by controller.jobs.collectAsState()
    val health by controller.health.collectAsState()
    var pendingScene by remember { mutableStateOf<SceneRef?>(null) }
    var uploading by remember { mutableStateOf(false) }

    LaunchedEffect(controller) { controller.refreshJobs() }

    MaterialTheme {
        Surface(Modifier.fillMaxSize()) {
            Row(Modifier.fillMaxSize()) {
                Column(Modifier.width(320.dp).fillMaxHeight().padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Kinesis Review", style = MaterialTheme.typography.headlineSmall)
                    Text(
                        "${api.baseUrl} · " + (health?.let { h -> "${h.status}: " + h.components.orEmpty().joinToString { "${it.name} ${if (it.ok) "ok" else "down"}" } } ?: "unreachable"),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(enabled = !uploading, onClick = {
                            val file = chooseBlend() ?: return@Button
                            uploading = true
                            scope.launch {
                                val bytes = withContext(Dispatchers.IO) { file.readBytes() }
                                controller.upload(bytes, file.name).onSuccess { pendingScene = it }
                                uploading = false
                            }
                        }) { Text(if (uploading) "Uploading…" else "New repair…") }
                        TextButton(onClick = { controller.refreshJobs() }) { Text("Refresh") }
                    }
                    HorizontalDivider()
                    LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        items(jobs, key = { it.jobId }) { job ->
                            JobRow(job, selected = job.jobId == state.job?.jobId) { controller.open(job.jobId) }
                        }
                    }
                }
                VerticalDivider()
                ReviewScreen(state, frames, controller::dispatch, onDecide = { controller.decide(it) }, onCancel = controller::cancel)
            }
        }
        pendingScene?.let { scene ->
            NewRepairDialog(
                scene,
                onSubmit = { draft ->
                    pendingScene = null
                    scope.launch { controller.create(scene, draft) }
                },
                onDismiss = { pendingScene = null },
            )
        }
    }
}

@Composable
private fun JobRow(job: RepairJob, selected: Boolean, onClick: () -> Unit) {
    Column(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(job.jobId.takeLast(10), fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal)
            Spacer(Modifier.weight(1f))
            StatusChip(job.status.value)
        }
        val t = job.selection.temporal
        Text("${job.selection.targetBones.joinToString()} · ${t.frameStart}–${t.frameEnd} · ${job.createdAt.take(16).replace('T', ' ')}", style = MaterialTheme.typography.bodySmall)
    }
}

private fun chooseBlend(): File? {
    val dialog = FileDialog(null as Frame?, "Choose a .blend file", FileDialog.LOAD)
    dialog.setFilenameFilter { _, name -> name.endsWith(".blend") }
    dialog.file = "*.blend"
    dialog.isVisible = true
    return dialog.file?.let { File(dialog.directory, it) }
}
