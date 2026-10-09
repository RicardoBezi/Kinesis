package dev.kinesis.client

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.ImageComposeScene
import androidx.compose.ui.unit.Density
import dev.kinesis.client.api.KinesisApi
import dev.kinesis.client.state.ReviewController
import dev.kinesis.client.state.ViewPhase
import dev.kinesis.client.ui.FrameCache
import dev.kinesis.client.ui.ReviewScreen
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.jetbrains.skia.EncodedImageFormat
import java.io.File

/**
 * Renders the review screen for a live job to a PNG, off-screen (no window), using the real
 * controller and API client. Used to check the UI against a running server:
 *
 *     gradlew screenshot -Pjob=<job_id> -Pout=<file.png> [-Papi=http://127.0.0.1:8000] [-Pframe=30]
 */
fun main(args: Array<String>): Unit = runBlocking {
    val (jobId, out) = args[0] to File(args[1])
    val api = KinesisApi.create(args.getOrElse(2) { "http://127.0.0.1:8000" })
    val frame = args.getOrElse(3) { "30" }.toInt()
    val controller = ReviewController(api, this)
    val frames = FrameCache(api)
    controller.open(jobId)
    withTimeout(60_000) {
        while (controller.state.value.phase.let { it == null || it == ViewPhase.WAITING }) delay(200)
    }
    controller.dispatch(dev.kinesis.client.state.Action.SetFrame(frame))
    val state = controller.state.value
    state.panes.forEach { pane -> pane.frames?.let { frames.load(it, state.frame) } } // warm the cache
    val scene = ImageComposeScene(width = 1500, height = 1500, density = Density(1f)) {
        MaterialTheme { Surface { ReviewScreen(controller.state.value, frames, controller::dispatch, {}, {}) } }
    }
    repeat(5) { i ->
        scene.render(i * 100_000_000L)
        delay(100)
    }
    val image = scene.render(1_000_000_000L)
    out.parentFile?.mkdirs()
    out.writeBytes(checkNotNull(image.encodeToData(EncodedImageFormat.PNG)).bytes)
    scene.close()
    api.close()
    println("wrote $out (${state.job?.status}, selected ${state.selected}, frame ${state.sceneFrame})")
    Runtime.getRuntime().halt(0) // the controller's poller keeps the scope alive
}
