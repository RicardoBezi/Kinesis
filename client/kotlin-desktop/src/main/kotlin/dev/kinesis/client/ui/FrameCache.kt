package dev.kinesis.client.ui

import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.toComposeImageBitmap
import dev.kinesis.client.api.KinesisApi
import dev.kinesis.client.generated.model.ArtifactReference
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.jetbrains.skia.Image

/** Bounded LRU of decoded preview frames, keyed by (artifact, frame index). */
class FrameCache(private val api: KinesisApi, private val capacity: Int = 600) {
    private val lock = Mutex()
    private val entries = object : LinkedHashMap<Pair<String, Int>, ImageBitmap>(64, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<Pair<String, Int>, ImageBitmap>?) = size > capacity
    }

    suspend fun load(ref: ArtifactReference, index: Int): ImageBitmap {
        val key = ref.artifactId to index
        lock.withLock { entries[key] }?.let { return it }
        val bytes = api.artifact(ref.uri, index)
        val image = Image.makeFromEncoded(bytes).toComposeImageBitmap()
        lock.withLock { entries[key] = image }
        return image
    }
}
